"""원격 모드 브라우저 로그인. GitHub·Google 응답은 httpx.MockTransport 로 고정한다."""
import base64
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from kb import control, tenant
from kb.config import Config
from kb.morph import Analyzer
from web import auth
from web.app import create_app

URL = "https://fq.example.com"
GOOGLE_ID, GITHUB_ID = "g-client", "gh-client"
TOKEN_URL, JWKS_URL = "https://oauth2.example/token", "https://oauth2.example/jwks"


def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeIdP:
    def __init__(self):
        self.key = rsa_key()
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwk = dict(jwk, kid="k1", alg="RS256", use="sig")
        self.sign_key = self.key
        self.claims: dict = {}
        self.sub = "google-sub-1"
        self.gh_id, self.gh_login = 12345, "octo"
        self.forms: list[dict] = []
        self.user_auth: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == auth.GOOGLE_DISCOVERY:
            return httpx.Response(200, json={"issuer": "https://accounts.google.com",
                                             "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                                             "token_endpoint": TOKEN_URL, "jwks_uri": JWKS_URL})
        if url == JWKS_URL:
            return httpx.Response(200, json={"keys": [self.jwk]})
        if url == TOKEN_URL:
            self.forms.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            now = int(time.time())
            claims = {"iss": "https://accounts.google.com", "aud": GOOGLE_ID, "sub": self.sub,
                      "iat": now, "exp": now + 3600, **self.claims}
            token = jwt.encode(claims, self.sign_key, algorithm="RS256", headers={"kid": "k1"})
            return httpx.Response(200, json={"id_token": token, "access_token": "ya29.x"})
        if url == auth.GITHUB_TOKEN:
            self.forms.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            return httpx.Response(200, json={"access_token": "gho_x", "token_type": "bearer"})
        if url == auth.GITHUB_USER:
            self.user_auth.append(request.headers.get("authorization", ""))
            return httpx.Response(200, json={"id": self.gh_id, "login": self.gh_login})
        return httpx.Response(404, json={})


def _no_kiwi():
    raise RuntimeError("이 파일은 형태소 계층을 쓰지 않는다")


@pytest.fixture(scope="module")
def analyzer():
    # Kiwi(약 600 MB)를 띄우지 않는다. 형태소 계층이 꺼지면 E·Y 로 찾는다(test_morph 가 확인한다)
    return Analyzer(loader=_no_kiwi)


@pytest.fixture
def idp():
    return FakeIdP()


def make_cfg(tmp_path, **kw):
    base = dict(db_path=tmp_path / "local.sqlite3", host="0.0.0.0", port=8765, backup_dir=tmp_path / "backup",
                backup_keep=3, log_dir=tmp_path / "logs", log_keep_days=14, recheck_interval_hours=24,
                mode="remote", public_url=URL, data_dir=tmp_path / "data",
                github_client_id=GITHUB_ID, google_client_id=GOOGLE_ID)
    base.update(kw)
    return Config(**base)


@pytest.fixture
def secrets_env(monkeypatch):
    monkeypatch.setenv("FQ_GITHUB_SECRET", "gh-secret")
    monkeypatch.setenv("FQ_GOOGLE_SECRET", "g-secret")


@pytest.fixture
def cfg(tmp_path, secrets_env):
    return make_cfg(tmp_path)


def app_for(cfg, idp, analyzer):
    return create_app(cfg, run_jobs=False, analyzer=analyzer, http_transport=httpx.MockTransport(idp.handler))


@pytest.fixture
def app(cfg, idp, analyzer):
    return app_for(cfg, idp, analyzer)


def client(app):
    return TestClient(app, base_url=URL)


def start(c, provider="google", next_=None):
    r = c.get(f"/auth/login/{provider}", params={"next": next_} if next_ else None, follow_redirects=False)
    assert r.status_code == 302
    return r, parse_qs(urlsplit(r.headers["location"]).query)


def login(c, provider="google", next_=None):
    _, q = start(c, provider, next_)
    return c.get(f"/auth/callback/{provider}", params={"code": "c1", "state": q["state"][0]}, follow_redirects=False)


def accounts(cfg) -> int:
    conn = control.connect_control(tenant.control_path(cfg.data_dir))
    try:
        return conn.execute("SELECT count(*) FROM account").fetchone()[0]
    finally:
        conn.close()


def test_google_login_creates_account_and_session(cfg, app, idp):
    with client(app) as c:
        r, q = start(c)
        assert r.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert q["scope"] == ["openid"] and q["code_challenge_method"] == ["S256"]
        assert q["redirect_uri"] == [f"{URL}/auth/callback/google"] and q["client_id"] == [GOOGLE_ID]
        done = c.get("/auth/callback/google", params={"code": "c1", "state": q["state"][0]}, follow_redirects=False)

        assert done.status_code == 302 and done.headers["location"] == "/"
        cookie = next(h for h in done.headers.get_list("set-cookie") if h.startswith("__Host-fq_session="))
        for attr in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"):
            assert attr.lower() in cookie.lower(), attr
        form = idp.forms[-1]
        assert form["client_secret"] == "g-secret" and form["code"] == "c1"
        digest = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
        assert digest.decode() == q["code_challenge"][0]

        me = c.get("/api/me").json()
        assert me["mode"] == "remote" and me["account"]["providers"] == ["google"]
        assert c.post("/api/categories", json={"name": "이관"}).status_code == 200
    assert tenant.db_path_for(cfg.data_dir, me["account"]["id"]).exists()


def test_same_identity_returns_same_account_and_providers_do_not_merge(cfg, app):
    ids = []
    for provider in ("google", "google", "github"):
        with client(app) as c:
            assert login(c, provider).headers["location"] == "/"
            ids.append(c.get("/api/me").json()["account"]["id"])
    assert ids[0] == ids[1] != ids[2]
    assert accounts(cfg) == 2


def test_github_login_uses_numeric_id_and_login(app, idp):
    with client(app) as c:
        _, q = start(c, "github")
        assert q["scope"] == ["read:user"] and q["code_challenge_method"] == ["S256"]
        assert c.get("/auth/callback/github", params={"code": "c1", "state": q["state"][0]},
                     follow_redirects=False).headers["location"] == "/"
        acc = c.get("/api/me").json()["account"]
    assert acc["display_name"] == "octo" and acc["providers"] == ["github"]
    assert idp.user_auth == ["Bearer gho_x"]
    assert idp.forms[-1]["client_secret"] == "gh-secret"


@pytest.mark.parametrize("bad", [
    {"aud": "someone-else"},
    {"iss": "https://evil.example"},
    {"exp": int(time.time()) - 60},
    "other_key",
])
def test_invalid_id_token_is_refused(cfg, app, idp, bad):
    if bad == "other_key":
        idp.sign_key = rsa_key()
    else:
        idp.claims = bad
    with client(app) as c:
        assert login(c).headers["location"] == "/?login_error=provider"
        assert c.get("/api/me").status_code == 401
    assert accounts(cfg) == 0


def test_state_must_come_from_same_browser_and_is_single_use(app):
    with client(app) as a:
        _, q = start(a)
        state = q["state"][0]
        with client(app) as b:  # 쿠키가 없는 다른 브라우저
            r = b.get("/auth/callback/google", params={"code": "c1", "state": state}, follow_redirects=False)
            assert r.headers["location"] == "/?login_error=state"
        assert a.get("/auth/callback/google", params={"code": "c1", "state": state},
                     follow_redirects=False).headers["location"] == "/"
        a.cookies.set("__Host-fq_login", state, domain="fq.example.com")
        assert a.get("/auth/callback/google", params={"code": "c1", "state": state},
                     follow_redirects=False).headers["location"] == "/?login_error=state"


def test_login_state_expires_after_ten_minutes(tmp_path):
    conn = control.connect_control(tmp_path / "control.sqlite3")
    t0 = datetime(2026, 10, 14, tzinfo=timezone.utc)
    try:
        s1 = control.put_login_state(conn, "google", "v", "/", now=t0)
        s2 = control.put_login_state(conn, "google", "v", "/", now=t0)
        assert control.take_login_state(conn, s1, "google", now=t0 + timedelta(minutes=11)) is None
        assert control.take_login_state(conn, s2, "github", now=t0) is None  # 공급자가 다르다
    finally:
        conn.close()


def test_denied_by_user(app):
    with client(app) as c:
        start(c)
        r = c.get("/auth/callback/google", params={"error": "access_denied"}, follow_redirects=False)
    assert r.headers["location"] == "/?login_error=denied"


def test_allowed_logins_limit_who_can_sign_in(tmp_path, secrets_env, idp, analyzer):
    cfg = make_cfg(tmp_path, allowed_logins=frozenset({"github:12345"}))
    app = app_for(cfg, idp, analyzer)
    with client(app) as c:
        assert login(c, "google").headers["location"] == "/?login_error=closed"
        assert c.get("/api/me").status_code == 401
    assert accounts(cfg) == 0
    with client(app) as c:
        assert login(c, "github").headers["location"] == "/"


def test_session_idle_and_absolute_expiry(tmp_path):
    conn = control.connect_control(tmp_path / "control.sqlite3")
    t0 = datetime(2026, 10, 14, tzinfo=timezone.utc)
    try:
        acc = control.create_account(conn)
        idle = control.create_session(conn, acc, now=t0)
        assert control.session_account(conn, idle, now=t0 + timedelta(days=29)) == acc
        assert control.session_account(conn, idle, now=t0 + timedelta(days=29 + 31)) is None

        busy = control.create_session(conn, acc, now=t0)
        for day in range(20, 90, 20):  # 계속 쓰여도
            assert control.session_account(conn, busy, now=t0 + timedelta(days=day)) == acc
        assert control.session_account(conn, busy, now=t0 + timedelta(days=91)) is None  # 생성 90일에 끝난다
        stored = {r[0] for r in conn.execute("SELECT id_hash FROM session")}
        assert idle not in stored and busy not in stored  # 쿠키 값 그대로는 두지 않는다
    finally:
        conn.close()


def test_logout_ends_session(cfg, app):
    with client(app) as c:
        login(c)
        assert c.get("/api/me").status_code == 200
        assert c.post("/auth/logout", headers={"Origin": URL}).json() == {"ok": True}
        assert c.get("/api/me").status_code == 401
    conn = control.connect_control(tenant.control_path(cfg.data_dir))
    try:
        assert conn.execute("SELECT count(*) FROM session").fetchone()[0] == 0
    finally:
        conn.close()


def test_logout_from_other_site_is_refused(app):
    with client(app) as c:
        login(c)
        assert c.post("/auth/logout", headers={"Origin": "https://evil.example"}).status_code == 403
        assert c.get("/api/me").status_code == 200


def test_account_session_cannot_read_other_account(app):
    with client(app) as a:
        login(a, "google")
        assert a.post("/api/categories", json={"name": "이관"}).status_code == 200
        item = a.post("/api/qa", json={"category": "이관", "question": "A 의 질문", "answer": "A 의 답."}).json()["item"]
    with client(app) as b:
        login(b, "github")
        assert b.get(f"/api/qa/{item['id']}").status_code == 404
        assert b.get("/api/search", params={"q": "질문"}).json()["hits"] == []


def test_unknown_cookie_is_not_a_session(app):
    with client(app) as c:
        c.cookies.set("__Host-fq_session", "garbage", domain="fq.example.com")
        r = c.get("/api/me")
    assert r.status_code == 401 and r.json()["providers"] == ["github", "google"]


def test_deleted_account_loses_session_and_login(cfg, app):
    with client(app) as c:
        login(c)
        acc = c.get("/api/me").json()["account"]["id"]
        conn = control.connect_control(tenant.control_path(cfg.data_dir))
        try:
            conn.execute("UPDATE account SET deleted_at = 'x' WHERE id = ?", (acc,))
        finally:
            conn.close()
        assert c.get("/api/me").status_code == 401
        assert login(c).headers["location"] == "/?login_error=closed"


def test_provider_without_secret_is_off(tmp_path, monkeypatch, idp, analyzer):
    monkeypatch.setenv("FQ_GOOGLE_SECRET", "g-secret")
    monkeypatch.delenv("FQ_GITHUB_SECRET", raising=False)
    app = app_for(make_cfg(tmp_path), idp, analyzer)
    with client(app) as c:
        assert c.get("/auth/login/github", follow_redirects=False).status_code == 404
        assert c.get("/api/me").json()["providers"] == ["google"]


@pytest.mark.parametrize("next_, want", [("//evil.example/x", "/"), ("/\\evil.example", "/"),
                                         ("https://evil.example", "/"), ("/?q=rename", "/?q=rename")])
def test_return_path_stays_on_site(app, next_, want):
    with client(app) as c:
        assert login(c, next_=next_).headers["location"] == want


def test_local_mode_has_no_login(tmp_path, analyzer):
    app = create_app(db_path=tmp_path / "kb.sqlite3", run_jobs=False, analyzer=analyzer)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        assert c.get("/api/me").json() == {"mode": "local"}
        assert c.get("/auth/login/google", follow_redirects=False).status_code == 404


def test_signup_limit_per_ip(tmp_path):
    conn = control.connect_control(tmp_path / "control.sqlite3")
    t0 = datetime(2026, 10, 14, tzinfo=timezone.utc)
    try:
        for n in range(control.SIGNUP_PER_HOUR):
            control.login_identity(conn, "google", f"s{n}", ip="1.2.3.4", now=t0)
        with pytest.raises(control.SignupLimited):
            control.login_identity(conn, "google", "s-more", ip="1.2.3.4", now=t0)
        assert control.login_identity(conn, "google", "s0", ip="1.2.3.4", now=t0)[1] is False  # 기존 계정은 들어온다
        assert control.login_identity(conn, "google", "s-other", ip="5.6.7.8", now=t0)[1] is True
        assert control.login_identity(conn, "google", "s-more", ip="1.2.3.4", now=t0 + timedelta(hours=1, minutes=1))[1] is True
    finally:
        conn.close()


def test_signup_limit_reaches_login_screen(app, idp):
    codes = []
    for n in range(control.SIGNUP_PER_HOUR + 1):
        idp.sub = f"new-{n}"
        with client(app) as c:
            codes.append(login(c).headers["location"])
    assert codes[:-1] == ["/"] * control.SIGNUP_PER_HOUR and codes[-1] == "/?login_error=limit"
