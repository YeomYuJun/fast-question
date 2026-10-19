"""PC 클라이언트 로그인: 브라우저 승인(web/cli_login.py) → 127.0.0.1 콜백 → 토큰 교환 → credentials.json(fq_client/login.py)."""
import io
import json
import threading
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import pytest
from starlette.testclient import TestClient

from fq_client import login as cli
from kb import control, tenant
from tests.test_auth import URL, analyzer, app, cfg, client, idp, login, secrets_env  # noqa: F401 - 픽스처
from web.cli_login import TOKEN_NAME_PREFIX


def params(port=50123, state="s" * 20, challenge=None, device="내 PC"):
    challenge = challenge or cli.pkce()[1]
    return {"port": str(port), "state": state, "code_challenge": challenge, "device": device}


def approve(c, p: dict, decision="approve"):
    r = c.post("/cli/authorize", data={**p, "decision": decision}, headers={"Origin": URL}, follow_redirects=False)
    assert r.status_code == 303, r.text
    loc = urlsplit(r.headers["location"])
    assert (loc.scheme, loc.hostname, loc.port, loc.path) == ("http", "127.0.0.1", int(p["port"]), "/callback")
    return {k: v[0] for k, v in parse_qs(loc.query).items()}


def exchange(app, code, verifier, replaces=""):
    with TestClient(app, base_url=URL) as anon:  # PC 명령은 브라우저 쿠키가 없다
        return anon.post("/api/cli/token", json={"code": code, "code_verifier": verifier, "replaces": replaces})


def granted_token(app, c, device="내 PC", replaces="") -> str:
    verifier, challenge = cli.pkce()
    q = approve(c, params(challenge=challenge, device=device))
    r = exchange(app, q["code"], verifier, replaces)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def me_status(app, token) -> int:
    with TestClient(app, base_url=URL, headers={"Authorization": f"Bearer {token}"}) as t:
        return t.get("/api/me").status_code


def test_without_session_shows_login_that_returns_here(app):
    p = params()
    with client(app) as c:
        r = c.get("/cli/authorize", params=p)
        assert r.status_code == 200 and "로그인" in r.text
        nxt = "/cli/authorize?" + urlencode(p)
        assert f"/auth/login/google?{urlencode({'next': nxt})}".replace("&", "&amp;") in r.text
        login(c, next_=nxt)
        page = c.get("/cli/authorize", params={**p, "device": "<b>PC</b>"})
    assert page.status_code == 200 and "이 PC 를 연결할까요?" in page.text
    assert "&lt;b&gt;PC&lt;/b&gt;" in page.text and "<b>PC</b>" not in page.text
    assert page.headers["x-frame-options"] == "DENY" and "default-src 'self'" in page.headers["content-security-policy"]


@pytest.mark.parametrize("bad", [{"port": "80"}, {"port": "x"}, {"state": "짧"}, {"code_challenge": "abc"}])
def test_bad_parameters_are_refused(app, bad):
    with client(app) as c:
        login(c)
        assert c.get("/cli/authorize", params={**params(), **bad}).status_code == 400


def test_approve_then_exchange_gives_a_working_personal_token(cfg, app):
    verifier, challenge = cli.pkce()
    with client(app) as c:
        login(c)
        p = params(challenge=challenge, state="state-1234567890ab")
        q = approve(c, p)
        assert q["state"] == p["state"] and q["code"]
        r = exchange(app, q["code"], verifier)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        body = r.json()
        assert body["token"].startswith(control.TOKEN_PREFIX) and body["name"] == TOKEN_NAME_PREFIX + "내 PC"
        assert body["account"]["providers"] == ["google"]
        assert me_status(app, body["token"]) == 200
        again = exchange(app, q["code"], verifier)  # 코드는 한 번만 쓴다
        assert again.status_code == 400 and again.json()["code"] == "invalid_grant"
        names = [t["name"] for t in c.get("/api/account/tokens").json()["tokens"]]
    assert names == [TOKEN_NAME_PREFIX + "내 PC"]


def test_wrong_verifier_is_refused(app):
    verifier, challenge = cli.pkce()
    with client(app) as c:
        login(c)
        q = approve(c, params(challenge=challenge))
    r = exchange(app, q["code"], verifier + "x")
    assert r.status_code == 400 and r.json()["code"] == "invalid_grant"


def test_deny_sends_access_denied_without_code(app):
    with client(app) as c:
        login(c)
        q = approve(c, params(), decision="deny")
    assert q["error"] == "access_denied" and "code" not in q


def test_approve_needs_session_and_same_origin(app):
    p = params()
    with client(app) as c:
        r = c.post("/cli/authorize", data={**p, "decision": "approve"}, headers={"Origin": URL}, follow_redirects=False)
        assert r.status_code == 401 and "로그인" in r.text
        login(c)
        r = c.post("/cli/authorize", data={**p, "decision": "approve"}, headers={"Origin": "https://evil.example"},
                   follow_redirects=False)
    assert r.status_code == 403


def test_replaces_revokes_only_own_old_token(app):
    with client(app) as c, client(app) as other:
        login(c)
        login(other, provider="github")
        mine = granted_token(app, c)
        theirs = granted_token(app, other)
        renewed = granted_token(app, c, replaces=mine)
        assert me_status(app, mine) == 401 and me_status(app, renewed) == 200
        granted_token(app, c, replaces=theirs)  # 다른 계정의 토큰은 폐기하지 않는다
        assert me_status(app, theirs) == 200


def test_code_expires_after_five_minutes(cfg):
    conn = control.connect_control(tenant.control_path(cfg.data_dir))
    try:
        acc = control.create_account(conn)
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        code = control.put_cli_grant(conn, acc, "c" * 43, "PC", now=t)
        assert control.take_cli_grant(conn, code, now=t + timedelta(minutes=5, seconds=1)) is None
        code = control.put_cli_grant(conn, acc, "c" * 43, "PC", now=t)
        assert control.take_cli_grant(conn, code, now=t + timedelta(minutes=4))["account_id"] == acc
        control.put_cli_grant(conn, acc, "c" * 43, "PC", now=t)
        control.delete_account(conn, acc)
        assert conn.execute("SELECT count(*) FROM cli_grant").fetchone()[0] == 0
    finally:
        conn.close()


def test_token_limit_is_reported(cfg, app):
    with client(app) as c:
        login(c)
        for i in range(control.TOKEN_MAX):
            assert c.post("/api/account/tokens", json={"name": f"t{i}"}, headers={"Origin": URL}).status_code == 200
        verifier, challenge = cli.pkce()
        q = approve(c, params(challenge=challenge))
    r = exchange(app, q["code"], verifier)
    assert r.status_code == 400 and r.json()["code"] == "token_limit" and "폐기" in r.json()["error"]


def test_page_css_uses_only_token_variables(app):
    with client(app) as c:
        login(c)
        page = c.get("/cli/authorize", params=params()).text
        css = c.get("/cli/login.css")
    assert "<style" not in page and 'href="/cli/login.css"' in page and 'href="/static/tokens.css"' in page
    assert css.headers["content-type"].startswith("text/css") and "var(--accent)" in css.text
    assert "#" not in css.text.replace("/*", "").split("*/", 1)[1]  # hex 색을 쓰지 않는다


# ---- PC 쪽 명령(fq_client/login.py) ----

def browser_for(c):
    """브라우저 흉내: 승인 주소를 열어 승인하고, 콜백 주소를 PC 의 임시 서버로 보낸다(로그인 명령이 기다리는 동안)."""
    def open_browser(target: str, decision="approve"):
        u = urlsplit(target)
        assert f"{u.scheme}://{u.netloc}" == URL and u.path == "/cli/authorize"
        p = {k: v[0] for k, v in parse_qs(u.query).items()}
        r = c.post("/cli/authorize", data={**p, "decision": decision}, headers={"Origin": URL}, follow_redirects=False)
        threading.Thread(target=lambda: urlopen(r.headers["location"], timeout=10).read(), daemon=True).start()
    return open_browser


def opener_for(app):
    """PC 명령의 urlopen 을 테스트 앱으로 보낸다(쿠키 없음)."""
    def open_(req, timeout=None):
        with TestClient(app, base_url=URL) as anon:
            r = anon.post(urlsplit(req.full_url).path, content=req.data, headers={"Content-Type": "application/json"})
        if r.status_code >= 400:
            raise HTTPError(req.full_url, r.status_code, "", {}, io.BytesIO(r.content))
        return io.BytesIO(r.content)
    return open_


def test_login_command_saves_token_keeps_device_id_and_replaces_old(app, tmp_path):
    cred = tmp_path / "home" / ".fast-question" / "credentials.json"
    cred.parent.mkdir(parents=True)
    cred.write_text(json.dumps({"url": URL, "token": "fq_pat_old", "device_id": "dev-1"}), encoding="utf-8")
    said = []
    with client(app) as c:
        login(c)
        saved = cli.login(None, path=cred, open_browser=browser_for(c), opener=opener_for(app), wait_s=10,
                          say=said.append)
        first = json.loads(cred.read_text(encoding="utf-8"))
        assert saved == cred and first["url"] == URL and first["device_id"] == "dev-1"
        assert me_status(app, first["token"]) == 200
        cli.login(None, path=cred, open_browser=browser_for(c), opener=opener_for(app), wait_s=10, say=said.append)
        second = json.loads(cred.read_text(encoding="utf-8"))
    assert second["device_id"] == "dev-1" and me_status(app, first["token"]) == 401
    assert me_status(app, second["token"]) == 200
    assert any("/cli/authorize?" in s for s in said) and any("로그인했습니다" in s for s in said)


def test_login_command_reports_deny_and_timeout(app, tmp_path):
    cred = tmp_path / "credentials.json"
    with client(app) as c:
        login(c)
        deny = browser_for(c)
        with pytest.raises(cli.LoginError, match="거부"):
            cli.login(URL, path=cred, open_browser=lambda t: deny(t, "deny"), opener=opener_for(app), wait_s=10,
                      say=lambda s: None)
    with pytest.raises(cli.LoginError, match="승인하지 않아"):
        cli.login(URL, path=cred, open_browser=lambda t: None, wait_s=0.5, say=lambda s: None)
    assert not cred.exists()


def test_login_url_defaults(tmp_path):
    assert cli.resolve_url(None, None) == cli.DEFAULT_URL
    assert cli.resolve_url(None, {"url": "https://other.example"}) == "https://other.example"
    assert cli.resolve_url("https://given.example/", {"url": "https://other.example"}) == "https://given.example"
    with pytest.raises(cli.LoginError):
        cli.resolve_url("fast-question.com", None)
