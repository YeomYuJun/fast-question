import pytest
from starlette.testclient import TestClient

from kb.morph import Analyzer
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
    assert r.text.index("/static/tokens.css") < r.text.index("/static/app.css")
    for path in ("/static/app.js", "/static/app.css", "/static/tokens.css",
                 "/static/fonts/Pretendard-Regular.woff2", "/static/fonts/JetBrainsMono-Regular.woff2"):
        assert client.get(path).status_code == 200, path


def test_app_css_uses_only_tokens():
    """색은 design 토큰 변수로만 쓴다(CLAUDE.md 화면 디자인 규칙)."""
    import re
    from web.app import STATIC
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert re.findall(r"#[0-9a-fA-F]{3,8}", css) == []


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


def test_review_and_oversight_endpoints_are_gone(client):
    item = add_basics(client)
    assert client.get("/api/oversight").status_code == 404
    assert client.post(f"/api/qa/{item['id']}/review").status_code in (404, 405)


@pytest.mark.parametrize("method, path", [
    ("POST", "/api/qa/1/links"), ("POST", "/api/qa/1/evidence"), ("POST", "/api/evidence/1/recapture"),
    ("POST", "/api/evidence/1/archive"), ("POST", "/api/recheck"), ("GET", "/api/dict"), ("POST", "/api/dict"),
    ("POST", "/api/reindex"), ("GET", "/api/status"),
])
def test_unused_endpoints_are_gone(client, method, path):
    """화면이 쓰지 않는 관리 기능은 MCP 도구에만 남는다."""
    add_basics(client)
    assert client.request(method, path, json={}).status_code in (404, 405)


def test_archive_then_restore(client):
    item = add_basics(client)
    archived = client.post(f"/api/qa/{item['id']}/archive", json={"version": item["version"]}).json()
    assert archived["archived"] is True
    assert client.get("/api/search", params={"q": "rename"}).json()["hits"] == []
    restored = client.post(f"/api/qa/{item['id']}/restore", json={"version": archived["version"]}).json()
    assert restored["archived"] is False


def test_evidence_line_on_create_and_edit(client, tmp_path):
    client.post("/api/categories", json={"name": "이관"})
    f = (tmp_path / "근거 폴더" / "a b.txt")
    f.parent.mkdir()
    f.write_text("v1", encoding="utf-8")
    f = f.resolve()
    r = client.post("/api/qa", json={"category": "이관", "question": "근거 있는 질문?", "answer": "답변이다.",
                                     "evidence_line": f"{f} #v1"})
    item = r.json()["item"]
    assert item["evidence_state"] == "trusted" and item["evidence_line"] == f"{f} #v1"

    edited = client.patch(f"/api/qa/{item['id']}", json={"version": item["version"],
                                                          "evidence_line": "메모: 사용자가 확인"}).json()
    assert edited["evidence_state"] == "external" and edited["evidence_line"] == "메모: 사용자가 확인"
    assert client.get(f"/api/qa/{item['id']}").json()["evidence_line"] == "메모: 사용자가 확인"

    cleared = client.patch(f"/api/qa/{item['id']}", json={"version": edited["version"], "evidence_line": ""}).json()
    assert cleared["evidence_state"] == "none" and cleared["evidence_line"] == ""

    bad = client.patch(f"/api/qa/{item['id']}", json={"version": cleared["version"],
                                                      "evidence_line": str(tmp_path / "없는 파일")})
    assert bad.status_code == 400 and "근거 경로가 없다" in bad.json()["error"]


def test_search_by_category(client):
    add_basics(client)
    client.post("/api/categories", json={"name": "일반"})
    client.post("/api/qa", json={"category": "일반", "question": "rename 은 무엇인가?", "answer": "이름 바꾸기다."})
    tops = {c["name"]: c for c in client.get("/api/categories").json()["categories"]}
    nas = tops["이관"]["children"][0]["id"]
    hits = client.get("/api/search", params={"q": "rename", "category": nas}).json()["hits"]
    assert [h["category_path"] for h in hits] == ["이관 > NAS"]


def test_locked_db_is_503(client, monkeypatch):
    import sqlite3
    from kb.store import Store as S

    def locked(*_, **__):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(S, "add_category", locked)
    r = client.post("/api/categories", json={"name": "잠김"})
    assert r.status_code == 503 and "잠겨" in r.json()["error"]


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


def test_search_box_syntax_without_regex(client):
    item = add_basics(client)
    assert [h["id"] for h in client.get("/api/search", params={"q": "rename | 없는말"}).json()["hits"]] == [item["id"]]
    assert client.get("/api/search", params={"q": "rename -확정"}).json()["hits"] == []
    assert client.get("/api/search", params={"q": "q:rename"}).json()["hits"] == []
    # 정규식은 MCP 에서만 쓴다. 화면 요청의 regex 인자는 무시되어 글자 그대로 찾는다
    assert client.get("/api/search", params={"q": "ren.*", "regex": "1"}).json()["hits"] == []


def test_rename_category(client):
    add_basics(client)
    top = client.get("/api/categories").json()["categories"][0]
    r = client.patch(f"/api/categories/{top['id']}", json={"version": top["version"], "name": "이관 사업"})
    assert r.status_code == 200 and r.json()["path"] == "이관 사업"
    assert client.get("/api/categories").json()["categories"][0]["children"][0]["path"] == "이관 사업 > NAS"
    stale = client.patch(f"/api/categories/{top['id']}", json={"version": top["version"], "name": "x"})
    assert stale.status_code == 409


def test_delete_category_with_impact(client):
    item = add_basics(client)
    top = client.get("/api/categories").json()["categories"][0]
    impact = client.get(f"/api/categories/{top['id']}/impact").json()
    assert (impact["children"], impact["qa"]) == (1, 1)

    r = client.post(f"/api/categories/{top['id']}/archive", json={"version": top["version"]})
    assert r.status_code == 200
    assert (r.json()["archived_children"], r.json()["archived_qa"]) == (1, 1)
    assert client.get("/api/categories").json()["categories"] == []
    assert client.get(f"/api/qa/{item['id']}").json()["archived"] is True
    assert client.get(f"/api/categories/{top['id']}/impact").status_code == 404


def test_page_and_static_files_are_revalidated(client):
    """화면 파일을 고친 뒤 브라우저가 캐시의 옛 app.js 를 쓰지 않게 매번 확인하게 한다."""
    for path in ("/", "/static/app.js", "/static/app.css", "/static/tokens.css"):
        assert client.get(path).headers.get("cache-control") == "no-cache", path
