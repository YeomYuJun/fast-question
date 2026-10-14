"""탈퇴: 로그인·개인 토큰·OAuth 토큰이 모두 끊기고 사용자 폴더는 deleted/ 로 옮긴다."""
from starlette.testclient import TestClient

from kb import control, tenant
from tests.test_auth import URL, analyzer, app, cfg, idp, login, secrets_env  # noqa: F401 - 픽스처


def test_delete_account_cuts_every_access(cfg, app):
    with TestClient(app, base_url=URL) as c:
        login(c)
        acc = c.get("/api/me").json()["account"]["id"]
        assert c.post("/api/categories", json={"name": "이관"}).status_code == 200
        token = c.post("/api/account/tokens", json={"name": "PC"}, headers={"Origin": URL}).json()["token"]
        conn = control.connect_control(tenant.control_path(cfg.data_dir))
        try:  # 이 계정에 OAuth 토큰도 하나 있다고 둔다
            conn.execute("INSERT INTO oauth_client(client_id, info_json, created_at) VALUES ('c', '{}', 0)")
            conn.execute("INSERT INTO oauth_token(hash, kind, grant_id, client_id, account_id, scopes, created_at, expires_at) "
                         "VALUES ('h', 'access', 'g', 'c', ?, 'kb', 0, 9999999999)", (acc,))
        finally:
            conn.close()

        with TestClient(app, base_url=URL, headers={"Authorization": f"Bearer {token}"}) as b:
            assert b.post("/api/account/delete", json={"confirm": "탈퇴"}).status_code == 401  # 토큰으로는 못 한다
        assert c.post("/api/account/delete", json={"confirm": "네"}, headers={"Origin": URL}).status_code == 400
        assert c.post("/api/account/delete", json={"confirm": "탈퇴"}, headers={"Origin": URL}).json() == {"ok": True}

        assert c.get("/api/me").status_code == 401
        with TestClient(app, base_url=URL, headers={"Authorization": f"Bearer {token}"}) as b:
            assert b.get("/api/categories").status_code == 401
        assert login(c).headers["location"] == "/?login_error=closed"

    assert not tenant.user_dir(cfg.data_dir, acc).exists()
    moved = list((cfg.data_dir / "deleted").iterdir())
    assert len(moved) == 1 and moved[0].name.startswith(f"{acc}-") and (moved[0] / "kb.sqlite3").exists()
    conn = control.connect_control(tenant.control_path(cfg.data_dir))
    try:
        assert conn.execute("SELECT count(*) FROM oauth_token WHERE account_id = ?", (acc,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM session WHERE account_id = ?", (acc,)).fetchone()[0] == 0
    finally:
        conn.close()
