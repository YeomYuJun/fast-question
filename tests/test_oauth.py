"""원격 MCP OAuth: 동적 등록 → /authorize → 로그인 세션·동의 → 인가 코드 → /token → /mcp.
로그인 공급자 응답은 tests/test_auth.py 의 FakeIdP 로 고정하고, MCP 호출은 tests/test_remote_mcp.py 방식으로 한다.
SDK 의 OAuth 경로(/register, /authorize, /token, /revoke)는 lifespan 안에서만 있으므로 HTTP 단계는 TestClient 블록
안에서, MCP 호출은 그 블록을 나온 뒤에 한다."""
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from kb import tenant
from tests.test_auth import (URL, analyzer, app, app_for, cfg, client, idp, login, make_cfg,  # noqa: F401 - 픽스처
                             secrets_env, start)
from tests.test_remote_mcp import call, new_token
from web import auth

REDIRECT = "http://localhost:33418/callback"
RESOURCE = f"{URL}/mcp"
CONSENT = "Claude Code 가 당신의 KB 를 읽고 씁니다"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}


def control_db(cfg) -> sqlite3.Connection:
    conn = sqlite3.connect(tenant.control_path(cfg.data_dir), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def register(c, redirect_uris=(REDIRECT,), **kw):
    body = {"redirect_uris": list(redirect_uris), "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "client_name": "Claude Code", **kw}
    return c.post("/register", json=body)


def authorize(c, client_id, challenge, resource=RESOURCE, state="st-1"):
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "scope": "kb",
              "code_challenge": challenge, "code_challenge_method": "S256", "state": state}
    if resource is not None:
        params["resource"] = resource
    return c.get("/authorize", params=params, follow_redirects=False)


def pending(c, **kw) -> tuple[str, str, str]:
    """(client_id, verifier, /oauth/continue/<id> 경로). 동의 직전까지 간다."""
    client_id = register(c, **kw).json()["client_id"]
    verifier, challenge = auth.pkce()
    r = authorize(c, client_id, challenge)
    assert r.status_code == 302, r.text
    loc = urlsplit(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}" == URL and loc.path.startswith("/oauth/continue/")
    return client_id, verifier, loc.path


def query(r) -> dict:
    assert r.status_code in (302, 303), r.text
    return {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}


def grant(c, provider="google") -> tuple[str, str, str]:
    """로그인부터 인가 코드까지. (client_id, verifier, code)"""
    client_id, verifier, path = pending(c)
    if c.get("/api/me").status_code != 200:
        assert login(c, provider, next_=path).headers["location"] == path
    assert CONSENT in c.get(path).text
    done = c.post(path, data={"decision": "approve"}, follow_redirects=False)
    assert done.headers["location"].startswith(REDIRECT + "?")
    q = query(done)
    assert q["state"] == "st-1"
    return client_id, verifier, q["code"]


def exchange(c, client_id, code, verifier):
    return c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                  "client_id": client_id, "code_verifier": verifier})


def tokens(c, provider="google") -> tuple[str, dict]:
    client_id, verifier, code = grant(c, provider)
    r = exchange(c, client_id, code, verifier)
    assert r.status_code == 200, r.text
    return client_id, r.json()


def refresh(c, client_id, refresh_token):
    return c.post("/token", data={"grant_type": "refresh_token", "refresh_token": refresh_token,
                                  "client_id": client_id})


def mcp_status(c, token) -> int:
    return c.post("/mcp", json=INIT, headers={"Accept": "application/json, text/event-stream",
                                              "Authorization": f"Bearer {token}"}).status_code


def test_metadata_documents(app):
    with client(app) as c:
        asm = c.get("/.well-known/oauth-authorization-server").json()
        prm = c.get("/.well-known/oauth-protected-resource/mcp").json()
    assert asm["issuer"] == URL
    for key, path in (("authorization_endpoint", "/authorize"), ("token_endpoint", "/token"),
                      ("registration_endpoint", "/register"), ("revocation_endpoint", "/revoke")):
        assert asm[key] == URL + path, key
    assert asm["scopes_supported"] == ["kb"] and asm["code_challenge_methods_supported"] == ["S256"]
    assert {"authorization_code", "refresh_token"} <= set(asm["grant_types_supported"])
    assert prm["resource"] == RESOURCE
    assert [s.rstrip("/") for s in prm["authorization_servers"]] == [URL]
    assert prm["scopes_supported"] == ["kb"]


def test_full_flow_then_kb_search(cfg, app):
    with client(app) as c:
        _, tok = tokens(c)
        account_id = c.get("/api/me").json()["account"]["id"]
    assert tok["token_type"] == "Bearer" and tok["expires_in"] == 3600 and tok["scope"] == "kb"
    assert tok["refresh_token"] and tok["access_token"] != tok["refresh_token"]
    access = tok["access_token"]
    assert not call(app, access, "kb_add_category", {"name": "이관"})[0]
    err, res = call(app, access, "kb_add", {"category": "이관", "question": "OAuth 로 등록한 질문",
                                            "answer": "OAuth 토큰의 계정 DB 에 쓴다."})
    assert not err and res["item"]["created_by"] == "claude"
    err, found = call(app, access, "kb_search", {"query": "OAuth"})
    assert not err and [h["id"] for h in found["hits"]] == [res["item"]["id"]]
    assert tenant.db_path_for(cfg.data_dir, account_id).exists()


def test_tokens_are_stored_as_hashes_with_lifetimes(cfg, app):
    with client(app) as c:
        _, tok = tokens(c)
    now = time.time()
    conn = control_db(cfg)
    try:
        rows = {r["kind"]: r for r in conn.execute("SELECT * FROM oauth_token")}
        values = [str(v) for r in conn.execute("SELECT * FROM oauth_token") for v in tuple(r)]
        values += [str(v) for r in conn.execute("SELECT * FROM auth_code") for v in tuple(r)]
    finally:
        conn.close()
    assert tok["access_token"] not in values and tok["refresh_token"] not in values
    assert abs(rows["access"]["expires_at"] - (now + 3600)) < 60
    assert abs(rows["refresh"]["expires_at"] - (now + 30 * 86400)) < 60


def test_login_page_without_session(app):
    with client(app) as c:
        _, _, path = pending(c)
        page = c.get(path)
        assert page.status_code == 200 and CONSENT not in page.text
        for provider in ("google", "github"):
            assert f'href="/auth/login/{provider}?next=%2Foauth%2Fcontinue%2F' in page.text
        assert "/static/tokens.css" in page.text
        assert login(c, "google", next_=path).headers["location"] == path
        assert CONSENT in c.get(path).text


def test_login_page_shows_only_enabled_providers(tmp_path, monkeypatch, idp, analyzer):
    monkeypatch.setenv("FQ_GOOGLE_SECRET", "g-secret")
    monkeypatch.delenv("FQ_GITHUB_SECRET", raising=False)
    app = app_for(make_cfg(tmp_path), idp, analyzer)
    with client(app) as c:
        _, _, path = pending(c)
        page = c.get(path).text
    assert "/auth/login/google?" in page and "/auth/login/github" not in page


def test_consent_shows_escaped_client_name_and_redirect(app):
    with client(app) as c:
        _, _, path = pending(c, client_name='<script>alert("x")</script>')
        login(c, next_=path)
        page = c.get(path)
    assert "<script>alert" not in page.text and "&lt;script&gt;" in page.text
    assert "localhost:33418" in page.text
    assert page.headers["x-frame-options"] == "DENY"


def test_deny_sends_access_denied(app):
    with client(app) as c:
        _, _, path = pending(c)
        login(c, next_=path)
        q = query(c.post(path, data={"decision": "deny"}, follow_redirects=False))
        assert q == {"error": "access_denied", "state": "st-1"}
        assert c.post(path, data={"decision": "approve"}, follow_redirects=False).status_code == 400  # 1회용
        assert c.get(path).status_code == 400


def test_approve_needs_browser_session(app):
    with client(app) as c:
        _, _, path = pending(c)
        r = c.post(path, data={"decision": "approve"}, follow_redirects=False)
        assert r.status_code == 401 and "code=" not in r.headers.get("location", "")
        login(c, next_=path)
        assert "code" in query(c.post(path, data={"decision": "approve"}, follow_redirects=False))


def test_approve_from_other_site_is_refused(app):
    with client(app) as c:
        _, _, path = pending(c)
        login(c, next_=path)
        r = c.post(path, data={"decision": "approve"}, headers={"Origin": "https://evil.example"},
                   follow_redirects=False)
    assert r.status_code == 403


@pytest.mark.parametrize("uri", ["http://example.com/callback", "http://localhost/callback",
                                 "http://localhost:3000/other", "http://127.0.0.1:3000/callback/x",
                                 "custom-app://callback", "ftp://localhost:21/callback"])
def test_register_refuses_other_redirect_uris(app, uri):
    with client(app) as c:
        r = register(c, redirect_uris=(REDIRECT, uri))
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"


@pytest.mark.parametrize("uri", ["http://127.0.0.1:5555/callback", "https://claude.ai/api/mcp/auth_callback"])
def test_register_accepts_loopback_and_https(app, uri):
    with client(app) as c:
        r = register(c, redirect_uris=(uri,))
    assert r.status_code == 201 and r.json()["redirect_uris"] == [uri]


def test_wrong_pkce_is_refused(app):
    with client(app) as c:
        client_id, _, code = grant(c)
        r = exchange(c, client_id, code, auth.pkce()[0])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_code_is_single_use_and_reuse_revokes_tokens(app):
    with client(app) as c:
        client_id, verifier, code = grant(c)
        first = exchange(c, client_id, code, verifier)
        assert first.status_code == 200
        again = exchange(c, client_id, code, verifier)
        assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
        assert mcp_status(c, first.json()["access_token"]) == 401


def test_code_expires_after_ten_minutes(cfg, app):
    with client(app) as c:
        client_id, verifier, code = grant(c)
        conn = control_db(cfg)
        try:
            (expires_at,) = conn.execute("SELECT expires_at FROM auth_code").fetchone()
            assert abs(expires_at - (time.time() + 600)) < 60
            conn.execute("UPDATE auth_code SET expires_at = ?", (time.time() - 1,))
        finally:
            conn.close()
        r = exchange(c, client_id, code, verifier)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_authorize_refuses_other_resource(app):
    with client(app) as c:
        client_id = register(c).json()["client_id"]
        for resource in ("https://other.example/mcp", None):
            q = query(authorize(c, client_id, auth.pkce()[1], resource=resource))
            assert q["error"] == "invalid_target" and q["state"] == "st-1"


def test_token_for_other_resource_is_401(cfg, app):
    with client(app) as c:
        _, tok = tokens(c)
        assert mcp_status(c, tok["access_token"]) == 200
        conn = control_db(cfg)
        try:
            conn.execute("UPDATE oauth_token SET resource = 'https://other.example/mcp'")
        finally:
            conn.close()
        assert mcp_status(c, tok["access_token"]) == 401


def test_expired_access_token_is_401(cfg, app):
    with client(app) as c:
        _, tok = tokens(c)
        conn = control_db(cfg)
        try:
            conn.execute("UPDATE oauth_token SET expires_at = ? WHERE kind = 'access'", (int(time.time()) - 1,))
        finally:
            conn.close()
        r = c.post("/mcp", json=INIT, headers={"Accept": "application/json, text/event-stream",
                                               "Authorization": f"Bearer {tok['access_token']}"})
    assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]


def test_refresh_rotates_and_old_refresh_is_refused(app):
    with client(app) as c:
        client_id, old = tokens(c)
        r = refresh(c, client_id, old["refresh_token"])
        assert r.status_code == 200, r.text
        new = r.json()
        assert new["refresh_token"] != old["refresh_token"] and new["access_token"] != old["access_token"]
        assert mcp_status(c, new["access_token"]) == 200
        assert mcp_status(c, old["access_token"]) == 401
        again = refresh(c, client_id, old["refresh_token"])
        assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
        assert refresh(c, client_id, new["refresh_token"]).status_code == 200


def test_refresh_of_other_client_is_refused(app):
    with client(app) as c:
        _, tok = tokens(c)
        other = register(c).json()["client_id"]
        r = refresh(c, other, tok["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


@pytest.mark.parametrize("which", ["access_token", "refresh_token"])
def test_revoke_ends_access_and_refresh(app, which):
    with client(app) as c:
        client_id, tok = tokens(c)
        # mcp 2.0.0 의 /revoke 는 공개 클라이언트도 client_secret 필드(빈 값)가 없으면 400 이다
        r = c.post("/revoke", data={"token": tok[which], "client_id": client_id, "client_secret": ""})
        assert r.status_code == 200
        assert mcp_status(c, tok["access_token"]) == 401
        assert refresh(c, client_id, tok["refresh_token"]).status_code == 400


def test_personal_token_and_oauth_token_share_mcp(cfg, app):
    _, pat = new_token(cfg)
    with client(app) as c:
        assert mcp_status(c, pat) == 200
        _, tok = tokens(c)
        assert mcp_status(c, tok["access_token"]) == 200
    assert call(app, pat, "kb_categories")[1]["categories"] == []


def test_oauth_token_of_account_a_cannot_see_account_b(app, idp):
    idp.sub = "google-a"
    with client(app) as c:
        _, a = tokens(c)
    idp.sub = "google-b"
    with client(app) as c:
        _, b = tokens(c)
    assert not call(app, a["access_token"], "kb_add_category", {"name": "이관"})[0]
    assert not call(app, a["access_token"], "kb_add", {"category": "이관", "question": "A 의 질문",
                                                       "answer": "A 의 답이다."})[0]
    assert call(app, b["access_token"], "kb_search", {"query": "질문"})[1]["hits"] == []
    assert call(app, b["access_token"], "kb_categories")[1]["categories"] == []
    assert len(call(app, a["access_token"], "kb_search", {"query": "질문"})[1]["hits"]) == 1


def test_deleted_account_tokens_are_refused(cfg, app):
    with client(app) as c:
        _, tok = tokens(c)
        conn = control_db(cfg)
        try:
            conn.execute("UPDATE account SET deleted_at = 'x'")
        finally:
            conn.close()
        assert mcp_status(c, tok["access_token"]) == 401


def test_idle_clients_are_deleted_after_a_year(cfg, app):
    from kb import control, oauth_store
    with client(app) as c:
        idle = register(c).json()["client_id"]
        used, _ = tokens(c)
    conn = control.connect_control(tenant.control_path(cfg.data_dir))
    try:
        year = 366 * 86400
        assert oauth_store.delete_idle_clients(conn, now=time.time() + 30) == 0
        conn.execute("UPDATE oauth_client SET created_at = created_at - ?", (year,))
        assert oauth_store.delete_idle_clients(conn) == 1  # 토큰을 받은 적이 없는 것만
        assert {r[0] for r in conn.execute("SELECT client_id FROM oauth_client")} == {used}
        assert oauth_store.delete_idle_clients(conn, now=time.time() + year) == 1
        assert conn.execute("SELECT count(*) FROM oauth_token").fetchone()[0] == 0
    finally:
        conn.close()
    assert idle != used
