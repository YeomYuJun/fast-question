"""원격 모드 기본 가이드: 근거 없이, 화면 주소는 public_url 로 바꿔 사용자 DB 를 처음 만들 때 1회 넣는다."""
from kb import guide, tenant
from kb.db import connect
from kb.text import check_answer
from tests.test_remote_web import URL, analyzer, app, cfg, client_for  # noqa: F401 - 픽스처

LOCAL_URL = "http://127.0.0.1:8765"


def test_load_remote_drops_evidence_and_uses_public_url():
    local = guide.load()
    remote = guide.load_remote(URL)
    assert [i["question"] for i in remote["items"]] == [i["question"] for i in local["items"]]
    assert all(i["evidence"] == [] for i in remote["items"])
    assert not any(LOCAL_URL in i["answer"] for i in remote["items"])
    assert sum(URL in i["answer"] for i in remote["items"]) == sum(LOCAL_URL in i["answer"] for i in local["items"]) > 0


def test_load_remote_keeps_answers_within_limit_for_long_url():
    long_url = "https://" + "a" * 200 + ".example.com"
    for item in guide.load_remote(long_url)["items"]:
        assert check_answer(item["answer"]) == item["answer"], item["question"]


def test_new_user_db_gets_remote_guide_once(cfg, app):
    n = len(guide.load()["items"])
    with client_for(app, 1) as c:
        cats = c.get("/api/categories").json()["categories"]
        assert [x["name"] for x in cats] == ["fast-question"] and cats[0]["count"] == n
        hit = c.get("/api/search", params={"q": "주소 형식"}).json()["hits"][0]
        item = c.get(f"/api/qa/{hit['id']}").json()
        assert URL in item["answer"] and item["evidence"] == [] and item["created_by"] == "system"
        c.patch(f"/api/qa/{item['id']}", json={"version": item["version"], "answer": "사용자가 고친 답이다."})
        assert c.get("/api/categories").json()["categories"][0]["count"] == n  # 두 번째 요청은 다시 넣지 않는다
    conn = connect(tenant.db_path_for(cfg.data_dir, 1))
    assert conn.execute("SELECT count(*) FROM qa").fetchone()[0] == n
    assert conn.execute("SELECT answer FROM qa WHERE id = ?", (item["id"],)).fetchone()[0] == "사용자가 고친 답이다."
    conn.close()


def test_existing_user_db_is_not_seeded(cfg, app):
    connect(tenant.db_path_for(cfg.data_dir, 1)).close()
    with client_for(app, 1) as c:
        assert c.get("/api/categories").json()["categories"] == []
