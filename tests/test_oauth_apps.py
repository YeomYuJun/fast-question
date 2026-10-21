"""연결 패널의 "연결된 앱": 계정의 OAuth 연결 목록과 끊기(kb/oauth_store.py list_connections·disconnect,
web/auth.py /api/account/apps). 브라우저 세션으로만 받는다."""
import json

import pytest

from kb import control, oauth_store, tenant
from tests.test_auth import (URL, analyzer, app, app_for, cfg, client, idp, login, make_cfg,  # noqa: F401 - 픽스처
                             secrets_env, start)
from tests.test_oauth import PARAMS, control_db, mcp_status, tokens
from tests.test_remote_mcp import new_token


@pytest.fixture
def conn(tmp_path):
    c = control.connect_control(tmp_path / "control.sqlite3")
    yield c
    c.close()


def connect_client(conn, client_id, account, t, name="Claude Code"):
    oauth_store.put_client(conn, client_id, json.dumps({"client_name": name}), now=t)
    code = oauth_store.create_code(conn, client_id, account, PARAMS, now=t)
    return oauth_store.exchange_code(conn, code, now=t)


def test_list_connections_names_dates_and_last_use(conn):
    me, other = control.create_account(conn), control.create_account(conn)
    first = connect_client(conn, "c1", me, 1000)
    connect_client(conn, "c2", me, 5000)  # 같은 이름으로 다시 연결(새 클라이언트 등록)
    connect_client(conn, "c3", me, 2000, name="")
    connect_client(conn, "o", other, 1000)
    oauth_store.rotate_refresh(conn, first["refresh_token"], ["kb"], now=9000)  # c1 을 최근에 썼다
    rows = oauth_store.list_connections(conn, me, now=9001)
    assert [r["client_id"] for r in rows] == ["c1", "c2", "c3"]  # 마지막 사용 순
    assert [r["name"] for r in rows] == ["Claude Code", "Claude Code", ""]
    c1 = rows[0]
    assert c1["connected_at"] == "1970-01-01T00:16:40Z"  # 클라이언트를 등록한 시각(갱신으로 바뀌지 않는다)
    assert c1["last_used_at"] == "1970-01-01T02:30:00Z"  # 그 계정·클라이언트 토큰을 마지막으로 낸 시각


def test_list_skips_connections_without_live_refresh(conn):
    me = control.create_account(conn)
    connect_client(conn, "old", me, 0)
    connect_client(conn, "new", me, oauth_store.REFRESH_TTL)
    rows = oauth_store.list_connections(conn, me, now=oauth_store.REFRESH_TTL + 1)
    assert [r["client_id"] for r in rows] == ["new"]
    oauth_store.put_client(conn, "registered", "{}", now=0)  # 등록만 하고 토큰을 받지 않음
    assert [r["client_id"] for r in oauth_store.list_connections(conn, me, now=oauth_store.REFRESH_TTL + 1)] == ["new"]


def test_disconnect_removes_only_that_account_and_client(conn):
    me, other = control.create_account(conn), control.create_account(conn)
    mine = connect_client(conn, "shared", me, 1000)
    theirs = oauth_store.exchange_code(conn, oauth_store.create_code(conn, "shared", other, PARAMS, now=1000), now=1000)
    keep = connect_client(conn, "keep", me, 1000)
    unused = oauth_store.create_code(conn, "shared", me, PARAMS, now=1000)
    other_code = oauth_store.create_code(conn, "shared", other, PARAMS, now=1000)

    assert oauth_store.disconnect(conn, me, "shared") is True
    assert oauth_store.load_token(conn, mine["access_token"], "access", now=1001) is None
    assert oauth_store.load_token(conn, mine["refresh_token"], "refresh", now=1001) is None
    assert oauth_store.exchange_code(conn, unused, now=1001) is None  # 승인했지만 아직 교환하지 않은 코드
    assert oauth_store.load_token(conn, theirs["access_token"], "access", now=1001) is not None
    assert oauth_store.load_token(conn, keep["access_token"], "access", now=1001) is not None
    assert oauth_store.exchange_code(conn, other_code, now=1001) is not None
    assert conn.execute("SELECT count(*) FROM oauth_client WHERE client_id = 'shared'").fetchone()[0] == 1
    assert oauth_store.disconnect(conn, me, "shared") is False  # 이미 끊었다
    assert oauth_store.disconnect(conn, me, "nothing") is False


# ---------- HTTP ----------

def test_apps_list_and_disconnect_over_browser_session(cfg, app):
    with client(app) as c:
        client_id, tok = tokens(c)
        r = c.get("/api/account/apps")
        assert r.status_code == 200, r.text
        apps = r.json()["apps"]
        assert [(a["client_id"], a["name"]) for a in apps] == [(client_id, "Claude Code")]
        assert apps[0]["connected_at"].endswith("Z") and apps[0]["last_used_at"].endswith("Z")

        r = c.delete(f"/api/account/apps/{client_id}")
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert c.get("/api/account/apps").json()["apps"] == []
        assert mcp_status(c, tok["access_token"]) == 401
        assert c.delete(f"/api/account/apps/{client_id}").status_code == 404


def test_disconnect_with_other_accounts_client_id_changes_nothing(cfg, app, idp):
    idp.sub = "google-a"
    with client(app) as c:
        a_client, a_tok = tokens(c)
    idp.sub = "google-b"
    with client(app) as c:
        login(c)
        r = c.delete(f"/api/account/apps/{a_client}")
        assert r.status_code == 404
        assert c.get("/api/account/apps").json()["apps"] == []
        assert mcp_status(c, a_tok["access_token"]) != 401
    conn = control_db(cfg)
    try:
        assert conn.execute("SELECT count(*) FROM oauth_token WHERE client_id = ?", (a_client,)).fetchone()[0] == 2
    finally:
        conn.close()


def test_apps_routes_need_browser_session(cfg, app):
    _, token = new_token(cfg)
    with client(app) as c:
        bearer = {"Authorization": f"Bearer {token}"}
        assert c.get("/api/account/apps", headers=bearer).status_code == 401
        assert c.delete("/api/account/apps/x", headers=bearer).status_code == 401
        assert c.get("/api/account/apps").status_code == 401
