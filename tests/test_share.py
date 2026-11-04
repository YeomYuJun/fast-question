"""공개 링크(kb/share.py, web/share.py): 항목·분류 하나를 로그인 없이 읽는 주소."""
from starlette.testclient import TestClient

from kb import control, share, tenant
from tests.test_auth import URL, analyzer, app, cfg, idp, login, secrets_env  # noqa: F401 - 픽스처
from tests.test_remote_mcp import new_token


def seed(c):
    assert c.post("/api/categories", json={"name": "이관", "description": "이관 사업"}).status_code == 200
    c.post("/api/categories", json={"name": "NAS", "parent": "이관"})
    a = c.post("/api/qa", json={"category": "이관 > NAS", "question": ".part 를 쓰는 이유", "answer": "rename 으로 확정한다."}).json()["item"]
    b = c.post("/api/qa", json={"category": "이관", "question": "이관 순서는", "answer": "복호화 뒤 옮긴다."}).json()["item"]
    return a, b


def test_share_qa_is_readable_without_login_until_revoked(app, cfg):
    with TestClient(app, base_url=URL) as c:
        login(c)
        a, _ = seed(c)
        r = c.post("/api/account/shares", json={"kind": "qa", "id": a["id"]})
        assert r.status_code == 200, r.text
        made = r.json()
        assert made["url"].startswith(f"{URL}/s/") and made["title"] == a["question"]
        token = made["url"].rsplit("/", 1)[1]
        # 같은 대상은 두 번 만들지 않는다
        assert c.post("/api/account/shares", json={"kind": "qa", "id": a["id"]}).status_code == 400
        assert [s["id"] for s in c.get("/api/account/shares").json()["shares"]] == [made["id"]]
    with TestClient(app, base_url=URL) as anon:
        assert anon.get(f"/s/{token}").status_code == 200 and "share.js" in anon.get(f"/s/{token}").text
        d = anon.get(f"/api/share/{token}").json()
        assert d["kind"] == "qa" and d["items"] == [{"question": a["question"], "answer": a["answer"], "path": "이관 > NAS",
                                                     "updated_at": d["items"][0]["updated_at"]}]
        assert "id" not in d["items"][0] and "evidence" not in d
        assert anon.get("/api/share/nope").status_code == 404
        assert anon.get("/api/account/shares").status_code == 401
    with TestClient(app, base_url=URL) as c:
        login(c)
        assert c.delete(f"/api/account/shares/{made['id']}").status_code == 200
        assert c.delete(f"/api/account/shares/{made['id']}").status_code == 404
    with TestClient(app, base_url=URL) as anon:
        assert anon.get(f"/api/share/{token}").status_code == 404


def test_share_category_lists_children_and_hides_archived(app, cfg):
    with TestClient(app, base_url=URL) as c:
        login(c)
        a, b = seed(c)
        top = next(t for t in c.get("/api/categories").json()["categories"] if t["name"] == "이관")
        made = c.post("/api/account/shares", json={"kind": "category", "id": top["id"]}).json()
        token = made["url"].rsplit("/", 1)[1]
        c.post(f"/api/qa/{b['id']}/archive", json={"version": b["version"]})
    with TestClient(app, base_url=URL) as anon:
        d = anon.get(f"/api/share/{token}").json()
        assert d["kind"] == "category" and d["title"] == "이관" and d["description"] == "이관 사업"
        assert [i["question"] for i in d["items"]] == [a["question"]]  # 보관한 항목은 빠진다, 하위 분류 항목은 든다


def test_share_needs_session_and_own_target(app, cfg):
    _, token = new_token(cfg)
    with TestClient(app, base_url=URL) as c:
        assert c.post("/api/account/shares", json={"kind": "qa", "id": 1},
                      headers={"Authorization": f"Bearer {token}"}).status_code == 401, "개인 토큰으로는 만들지 못한다"
    with TestClient(app, base_url=URL) as c:
        login(c)
        assert c.post("/api/account/shares", json={"kind": "qa", "id": 9999}).status_code == 404
        assert c.post("/api/account/shares", json={"kind": "evidence", "id": 1}).status_code == 400
        assert c.post("/api/account/shares", json={"kind": "qa"}).status_code == 400


def test_delete_account_revokes_shares(app, cfg):
    with TestClient(app, base_url=URL) as c:
        login(c)
        a, _ = seed(c)
        token = c.post("/api/account/shares", json={"kind": "qa", "id": a["id"]}).json()["url"].rsplit("/", 1)[1]
    conn = control.connect_control(tenant.control_path(cfg.data_dir))
    try:
        assert share.resolve(conn, token) is not None
        control.delete_account(conn, 1)
        assert share.resolve(conn, token) is None
    finally:
        conn.close()


def test_share_limit_per_account(tmp_path):
    conn = control.connect_control(tmp_path / "control.sqlite3")
    acc = control.create_account(conn)
    try:
        share.SHARE_MAX, keep = 2, share.SHARE_MAX
        share.create(conn, acc, "qa", 1)
        share.create(conn, acc, "qa", 2)
        try:
            share.create(conn, acc, "qa", 3)
            assert False, "상한을 넘겼다"
        except share.ShareRefused as e:
            assert "2개" in str(e)
    finally:
        share.SHARE_MAX = keep
        conn.close()
