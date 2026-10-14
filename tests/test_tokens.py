"""개인 토큰: 브라우저 세션으로 발급·폐기하고, 훅·로컬 프록시가 Bearer 로 쓴다."""
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from kb import control, tenant
from tests.test_auth import URL, analyzer, app, cfg, client, idp, login, secrets_env  # noqa: F401 - 픽스처


def make_token(c, name="내 PC"):
    r = c.post("/api/account/tokens", json={"name": name}, headers={"Origin": URL})
    assert r.status_code == 200, r.text
    return r.json()


def bearer_client(app, token):
    return TestClient(app, base_url=URL, headers={"Authorization": f"Bearer {token}"})


def test_token_is_shown_once_and_stored_as_hash(cfg, app):
    with client(app) as c:
        login(c)
        made = make_token(c)
        listed = c.get("/api/account/tokens").json()["tokens"]
    assert made["token"].startswith("fq_pat_") and len(made["token"]) > 40
    assert [t["name"] for t in listed] == ["내 PC"] and "token" not in listed[0] and "hash" not in listed[0]
    conn = control.connect_control(tenant.control_path(cfg.data_dir))
    try:
        assert conn.execute("SELECT hash FROM personal_token").fetchone()[0] != made["token"]
    finally:
        conn.close()


def test_bearer_token_reaches_own_db(cfg, app):
    with client(app) as c:
        login(c)
        account = c.get("/api/me").json()["account"]["id"]
        token = make_token(c)["token"]
    with bearer_client(app, token) as b:
        assert b.post("/api/categories", json={"name": "이관"}).status_code == 200
        assert b.get("/api/me").json()["account"]["id"] == account
    assert tenant.db_path_for(cfg.data_dir, account).exists()


def test_revoked_or_unknown_token_is_401(app):
    with client(app) as c:
        login(c)
        made = make_token(c)
        assert c.delete(f"/api/account/tokens/{made['id']}", headers={"Origin": URL}).json() == {"ok": True}
        assert c.delete(f"/api/account/tokens/{made['id']}", headers={"Origin": URL}).status_code == 404
    for token in (made["token"], "fq_pat_garbage", "garbage"):
        with bearer_client(app, token) as b:
            assert b.get("/api/categories").status_code == 401, token


def test_token_cannot_manage_tokens(app):
    with client(app) as c:
        login(c)
        token = make_token(c)["token"]
    with bearer_client(app, token) as b:
        assert b.get("/api/account/tokens").status_code == 401
        assert b.post("/api/account/tokens", json={"name": "더"}).status_code == 401


def test_other_account_cannot_revoke(app):
    with client(app) as a:
        login(a, "google")
        made = make_token(a)
    with client(app) as b:
        login(b, "github")
        assert b.delete(f"/api/account/tokens/{made['id']}", headers={"Origin": URL}).status_code == 404
    with bearer_client(app, made["token"]) as t:
        assert t.get("/api/categories").status_code == 200


@pytest.mark.parametrize("name", ["", "   ", "가" * 61, "두\n줄"])
def test_token_name_is_checked(app, name):
    with client(app) as c:
        login(c)
        r = c.post("/api/account/tokens", json={"name": name}, headers={"Origin": URL})
    assert r.status_code == 400 and r.json()["code"] == "invalid"


def test_token_limit_expiry_and_last_used(tmp_path):
    conn = control.connect_control(tmp_path / "control.sqlite3")
    t0 = datetime(2026, 10, 14, tzinfo=timezone.utc)
    try:
        acc = control.create_account(conn)
        made = [control.create_personal_token(conn, acc, f"PC {i}", now=t0) for i in range(control.TOKEN_MAX)]
        with pytest.raises(control.TokenRefused):
            control.create_personal_token(conn, acc, "하나 더", now=t0)
        control.revoke_personal_token(conn, acc, made[0]["id"], now=t0)
        control.create_personal_token(conn, acc, "자리 남", now=t0)  # 폐기하면 자리가 난다

        tok = made[1]["token"]
        assert control.token_account(conn, tok, now=t0 + timedelta(days=179))["account_id"] == acc
        assert control.token_account(conn, tok, now=t0 + timedelta(days=180)) is None
        used = {t["id"]: t["last_used_at"] for t in control.list_personal_tokens(conn, acc)}
        assert used[made[1]["id"]] is not None and used[made[2]["id"]] is None
        assert control.list_personal_tokens(conn, acc, now=t0 + timedelta(days=181))[0]["expired"] is True
    finally:
        conn.close()
