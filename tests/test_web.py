import pytest
from starlette.testclient import TestClient

from kb.db import connect
from kb.morph import Analyzer
from kb.store import Store
from web.app import create_app


@pytest.fixture(scope="module")
def analyzer():
    return Analyzer()


@pytest.fixture
def db(tmp_path):
    return tmp_path / "kb.sqlite3"


@pytest.fixture
def client(db, analyzer):
    app = create_app(db_path=db, run_jobs=False, analyzer=analyzer)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        yield c


def add_basics(client):
    assert client.post("/api/categories", json={"name": "이관"}).status_code == 200
    assert client.post("/api/categories", json={"name": "NAS", "parent": "이관"}).status_code == 200
    r = client.post("/api/qa", json={"category": "이관 > NAS", "question": ".part 를 쓰는 이유",
                                     "answer": "다 쓴 뒤 rename 으로 확정한다."})
    assert r.status_code == 200
    return r.json()["item"]


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "fast-question" in r.text
    assert client.get("/static/app.js").status_code == 200


def test_create_and_search_grouped(client):
    item = add_basics(client)
    assert item["created_by"] == "user" and "reviewed" not in item
    res = client.get("/api/search", params={"q": "rename"}).json()
    assert [h["id"] for h in res["hits"]] == [item["id"]]
    assert res["groups"][0]["category_path"] == "이관 > NAS"
    assert res["hits"][0]["spans"]["answer"]


def test_validation_is_400(client):
    add_basics(client)
    r = client.post("/api/qa", json={"category": "이관", "question": "질문", "answer": "1\n2\n3"})
    assert r.status_code == 400 and "2줄" in r.json()["error"]


def test_conflict_is_409_with_current(client):
    item = add_basics(client)
    ok = client.patch(f"/api/qa/{item['id']}", json={"version": item["version"], "answer": "고친 답변"})
    assert ok.status_code == 200
    r = client.patch(f"/api/qa/{item['id']}", json={"version": item["version"], "answer": "또 고친 답변"})
    assert r.status_code == 409 and r.json()["current"]["answer"] == "고친 답변"


def test_not_found_is_404(client):
    assert client.get("/api/qa/999").status_code == 404


def test_similar_then_force(client):
    add_basics(client)
    body = {"category": "이관", "question": ".part 를 쓰는 이유는", "answer": "확정 전 파일을 숨긴다."}
    assert client.post("/api/qa", json=body).json()["status"] == "similar_found"
    assert client.post("/api/qa", json=dict(body, force=True)).json()["status"] == "created"


def test_archive_restore_and_links(client):
    a = add_basics(client)
    b = client.post("/api/qa", json={"category": "이관", "question": "PETRA 제거 이유",
                                     "answer": "대상 DB 에서 쓸 수 없다."}).json()["item"]
    assert client.post(f"/api/qa/{a['id']}/links", json={"other": b["id"]}).status_code == 200
    assert [l["id"] for l in client.get(f"/api/qa/{a['id']}").json()["links"]] == [b["id"]]
    archived = client.post(f"/api/qa/{a['id']}/archive", json={"version": a["version"]}).json()
    assert archived["archived"] is True
    restored = client.post(f"/api/qa/{a['id']}/restore", json={"version": archived["version"]}).json()
    assert restored["archived"] is False


def test_evidence_and_recheck(client, tmp_path):
    item = add_basics(client)
    f = tmp_path / "근거 파일.txt"
    f.write_text("v1", encoding="utf-8")
    after = client.post(f"/api/qa/{item['id']}/evidence", json={"kind": "auto", "path": str(f)}).json()
    assert after["evidence_state"] == "trusted"
    f.write_text("v2", encoding="utf-8")
    assert client.post("/api/recheck", json={}).status_code == 200
    detail = client.get(f"/api/qa/{item['id']}").json()
    assert detail["evidence_state"] == "stale"
    ev = detail["evidence"][0]["id"]
    assert client.post(f"/api/evidence/{ev}/recapture").json()["evidence_state"] == "trusted"


def test_dictionary_endpoints(client):
    add_basics(client)
    r = client.post("/api/dict", json={"action": "synonym_add", "terms": ["임시파일", ".part"]})
    assert r.status_code == 200 and r.json()["snapshot"]["synonyms"]
    hits = client.get("/api/search", params={"q": "임시파일"}).json()["hits"]
    assert hits and hits[0]["tier"] == "synonym"
    assert client.get("/api/dict").json()["stopwords"]


def test_review_and_oversight_endpoints_are_gone(client):
    item = add_basics(client)
    assert client.get("/api/oversight").status_code == 404
    assert client.post(f"/api/qa/{item['id']}/review").status_code in (404, 405)


def test_app_start_warms_up_analyzer(db):
    import threading

    loaded = threading.Event()

    class FakeKiwi:
        def add_user_word(self, *_):
            return True

        def tokenize(self, _):
            loaded.set()
            return []

    create_app(db_path=db, run_jobs=False, analyzer=Analyzer(loader=FakeKiwi))
    assert loaded.wait(5), "앱을 만들면 형태소 분석기를 백그라운드에서 미리 띄워야 한다"


def test_status(client):
    s = client.get("/api/status").json()
    assert {"morph", "pending_reindex", "last_recheck", "last_backup", "counts"} <= set(s)


def test_search_options_and_filters(client):
    add_basics(client)
    r = client.get("/api/search", params={"q": r"\.part", "regex": "1"}).json()
    assert r["total"] == 1


@pytest.mark.parametrize("headers", [
    {"origin": "https://evil.example", "content-type": "text/plain"},
    {"host": "attacker.example:8765"},
    {"origin": "null"},
])
def test_rejects_cross_site_requests(client, headers):
    r = client.post("/api/categories", content='{"name": "심은 분류"}', headers=headers)
    assert r.status_code == 403
    assert client.get("/api/categories").json()["categories"] == []


def test_rejects_foreign_host_on_read(client):
    assert client.get("/api/search", params={"q": "x"}, headers={"host": "attacker.example:8765"}).status_code == 403


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8765", "http://localhost:8765"])
def test_accepts_same_origin(client, origin):
    r = client.post("/api/categories", json={"name": "정상 분류"}, headers={"origin": origin})
    assert r.status_code == 200
