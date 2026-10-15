"""로그인 수단 추가: 로그인한 계정에 다른 공급자 신원을 붙인다. 그 신원이 다른 계정에 있었으면 그 계정의 DB 는 버린다."""
from urllib.parse import parse_qs, urlsplit

from kb import control, tenant
from tests.test_auth import (URL, accounts, analyzer, app, app_for, cfg, client, idp, login, make_cfg,  # noqa: F401
                             secrets_env)

ORIGIN = {"Origin": URL}


def link(c, provider="google"):
    r = c.post(f"/auth/link/{provider}", headers=ORIGIN, follow_redirects=False)
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    if not loc.startswith("https://"):
        return r
    state = parse_qs(urlsplit(loc).query)["state"][0]
    return c.get(f"/auth/callback/{provider}", params={"code": "c1", "state": state}, follow_redirects=False)


def me(c) -> dict:
    return c.get("/api/me").json()["account"]


def account_row(cfg, account_id):
    conn = control.connect_control(tenant.control_path(cfg.data_dir))
    try:
        return dict(conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone())
    finally:
        conn.close()


def test_link_adds_provider_to_current_account(cfg, app):
    with client(app) as c:
        login(c, "github")
        a = me(c)["id"]
        assert c.get("/api/me").json()["available"] == ["github", "google"]  # 화면이 추가 버튼을 그릴 공급자
        assert link(c, "google").headers["location"] == "/?linked=google"
        assert me(c) == {"id": a, "display_name": "octo", "providers": ["github", "google"]}
    with client(app) as c:
        login(c, "google")
        assert me(c)["id"] == a
    assert accounts(cfg) == 1


def test_link_discards_db_of_account_that_had_the_identity(cfg, app):
    with client(app) as c:
        login(c, "google")
        b = me(c)["id"]
        assert c.post("/api/categories", json={"name": "버릴 분류"}).status_code == 200
    with client(app) as c:
        login(c, "github")
        a = me(c)["id"]
        assert link(c, "google").headers["location"] == "/?linked=google&discarded=1"
        assert me(c)["providers"] == ["github", "google"]
        assert [x["name"] for x in c.get("/api/categories").json()["categories"]] != ["버릴 분류"]
    assert account_row(cfg, b)["deleted_at"] is not None
    assert not tenant.user_dir(cfg.data_dir, b).exists()
    assert any(p.name.startswith(f"{b}-") for p in (cfg.data_dir / "deleted").iterdir())
    with client(app) as c:
        login(c, "google")
        assert me(c)["id"] == a


def test_link_needs_browser_session(app):
    with client(app) as c:
        assert link(c, "google").headers["location"] == "/?link_error=session"


def test_link_fails_if_session_ended_before_callback(cfg, app):
    with client(app) as c:
        login(c, "github")
        r = c.post("/auth/link/google", headers=ORIGIN, follow_redirects=False)
        state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
        assert c.post("/auth/logout", headers=ORIGIN).status_code == 200
        done = c.get("/auth/callback/google", params={"code": "c1", "state": state}, follow_redirects=False)
        assert done.headers["location"] == "/?link_error=state"
        assert c.get("/api/me").status_code == 401
    assert accounts(cfg) == 1  # Google 신원으로 새 계정을 만들지 않았다


def test_link_respects_allowed_logins(tmp_path, monkeypatch, idp, analyzer):
    monkeypatch.setenv("FQ_GITHUB_SECRET", "gh-secret")
    monkeypatch.setenv("FQ_GOOGLE_SECRET", "g-secret")
    cfg = make_cfg(tmp_path, allowed_logins=frozenset({f"github:{idp.gh_id}"}))
    with client(app_for(cfg, idp, analyzer)) as c:
        login(c, "github")
        assert link(c, "google").headers["location"] == "/?link_error=closed"
        assert me(c)["providers"] == ["github"]


def test_second_identity_of_same_provider_is_refused(app, idp):
    with client(app) as c:
        login(c, "github")
        idp.gh_id += 1
        assert link(c, "github").headers["location"] == "/?link_error=exists"
        assert me(c)["providers"] == ["github"]


def test_linking_own_identity_again_changes_nothing(cfg, app):
    with client(app) as c:
        login(c, "github")
        assert link(c, "github").headers["location"] == "/?linked=github"
        assert me(c)["providers"] == ["github"]
    assert accounts(cfg) == 1
