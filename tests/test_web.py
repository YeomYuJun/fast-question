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
    assert "<title>fast question - Claude 와 같이 쓰는 Q&amp;A 저장소</title>" in r.text
    assert '<meta name="description" content="' in r.text  # 검색 결과 요약이 로그인 화면 견본 문장을 잘라 쓰지 않게
    assert "noindex" not in r.text
    assert r.text.index("/static/tokens.css") < r.text.index("/static/app.css")
    for path in ("/static/app.js", "/static/app.css", "/static/tokens.css",
                 "/static/fonts/Pretendard-Regular.woff2", "/static/fonts/JetBrainsMono-Regular.woff2"):
        assert client.get(path).status_code == 200, path


def test_index_lists_search_syntax(client):
    """검색창 옆 도움말은 kb/query.py 의 문법을 모두 보인다."""
    html = client.get("/").text
    for token in ("<code>a b</code>", "<code>a | b</code>", "<code>-a</code>", '<code>"a b"</code>',
                  "<code>q:a</code>", "<code>a:a</code>", "<code>path:x</code>"):
        assert token in html, token


def test_page_has_csp_and_no_inline_code(client):
    """CSP default-src 'self' 아래에서 화면이 돌게 인라인 스크립트·스타일을 두지 않는다."""
    import re
    r = client.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert "<style" not in r.text and " style=" not in r.text
    assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", r.text) == []
    # 테마는 첫 그리기 전에 정한다: 스타일시트보다 먼저, defer 없이
    assert r.text.index('<script src="/static/theme.js"></script>') < r.text.index("/static/tokens.css")
    assert client.get("/static/theme.js").status_code == 200
    assert "default-src 'self'" in client.get("/api/me").headers["content-security-policy"]


def test_terms_and_privacy_pages(client):
    """약관·처리방침은 정적 페이지다. 본 화면처럼 인라인 코드 없이 테마를 먼저 정하고, 서로 링크한다."""
    import re
    for path, title in (("/terms", "이용약관"), ("/privacy", "개인정보 처리방침")):
        r = client.get(path)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"], path
        assert r.headers["cache-control"] == "no-cache"
        assert f"<h1>{title}</h1>" in r.text
        assert f'href="{path}" aria-current="page"' in r.text
        assert "<style" not in r.text and " style=" not in r.text
        assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", r.text) == []
        assert r.text.index('<script src="/static/theme.js"></script>') < r.text.index("/static/tokens.css")
        assert '<meta name="robots" content="noindex">' in r.text, path  # 검색 결과에는 첫 화면만 나온다
    assert "운영자는 장애 대응·백업 복구를 위해" in client.get("/privacy").text
    for path in ("/static/doc.js", "/static/favicon.svg"):
        assert client.get(path).status_code == 200, path


def test_favicon_ico_at_root(client):
    """검색 엔진은 SVG 대신 /favicon.ico 를 찾기도 한다. 48px 배수 크기를 담는다."""
    r = client.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/x-icon"
    assert r.content[:4] == bytes([0, 0, 1, 0])
    sizes = {r.content[6 + 16 * i] for i in range(int.from_bytes(r.content[4:6], "little"))}
    assert sizes == {16, 32, 48}
    assert 'rel="icon" href="/favicon.ico"' in client.get("/").text


def test_plugin_install_commands_match_marketplace():
    """연결 패널 2단계는 플러그인 설치·로그인 명령이고, 상자마다 명령 한 줄뿐이다(주석·안내는 상자 밖).
    마켓플레이스·플러그인 이름은 .claude-plugin/marketplace.json 과 같다. 저장소 설치 명령(install.py)은 없다."""
    import json
    import re
    from pathlib import Path
    from web.app import STATIC
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    m = re.search(r"const PLUGIN_CMDS = \(url\) => \[(.*?)\n\];", js, re.S)
    assert m, "app.js 에 PLUGIN_CMDS 가 없다"
    block = m.group(1)
    market = json.loads((Path(__file__).resolve().parent.parent / ".claude-plugin" / "marketplace.json")
                        .read_text(encoding="utf-8-sig"))
    plugin = market["plugins"][0]["name"]
    assert re.findall(r'["`]([^"`]+)["`]', block) == [
        f"claude plugin marketplace add {market['owner']['name']}/fast-question",
        f"claude plugin install {plugin}@{market['name']}",
        "uvx fast-question login --url ${url}",
    ]
    assert "#" not in block and "\\n" not in block
    assert "install.py" not in js and "INSTALL_SHELLS" not in js and "fq-shell" not in js


def test_one_line_connect_command():
    """연결 패널 1단계는 설치 없는 원격 MCP 등록 한 줄이다(인증은 등록 뒤 Claude Code 의 /mcp 에서)."""
    from web.app import STATIC
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "const ONE_LINE = (url) => `claude mcp add --transport http -s user fast-question ${url}/mcp`;" in js
    assert "ONE_LINE(location.origin)" in js


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
    r = client.post("/api/qa", json={"category": "이관", "question": "질문", "answer": "1\n2\n3\n4\n5\n6\n7"})
    assert r.status_code == 400 and "6줄" in r.json()["error"]


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
    assert bad.status_code == 400 and "근거 경로가 없습니다" in bad.json()["error"]


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


def test_search_zero_hits_suggests_dropping_a_word(client):
    item = add_basics(client)
    res = client.get("/api/search", params={"q": "rename 없는단어", "relax": "1"}).json()
    assert res["total"] == 0 and res["relaxed"] == [{"dropped": "없는단어", "query": "rename", "total": 1}]
    assert client.get("/api/search", params={"q": "rename"}).json()["hits"][0]["id"] == item["id"]
    # 즉석 질문 훅처럼 relax 를 넘기지 않는 요청은 단어 빼기를 계산하지 않는다
    assert "relaxed" not in client.get("/api/search", params={"q": "rename 없는단어"}).json()


def test_search_since_hours(client):
    add_basics(client)
    assert client.get("/api/search", params={"since": "24"}).json()["total"] == 1
    assert client.get("/api/search", params={"q": "rename", "since": "24"}).json()["total"] == 1
    assert client.get("/api/search", params={"since": "abc"}).status_code == 400


def test_export_category_markdown(client):
    item = add_basics(client)
    client.post("/api/qa", json={"category": "이관", "question": "이관 대상은?", "answer": "NAS 와 DB 다.\n- NAS: 파일\n- DB: 테이블"})
    client.post("/api/categories", json={"name": "DB", "parent": "이관"})
    gone = client.post("/api/qa", json={"category": "이관 > DB", "question": "지운 질문?", "answer": "지운 답이다."}).json()["item"]
    client.post(f"/api/qa/{gone['id']}/archive", json={"version": gone["version"]})
    tops = client.get("/api/categories").json()["categories"]
    top = tops[0]
    nas = next(c for c in top["children"] if c["name"] == "NAS")

    r = client.get("/api/export", params={"category": nas["id"]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "filename*=UTF-8''" in r.headers["content-disposition"]
    assert r.text == f"# 이관 > NAS\n\n### {item['question']}\n\n{item['answer']}\n"

    whole = client.get("/api/export", params={"category": top["id"]}).text
    assert whole.startswith("# 이관\n\n### 이관 대상은?\n\nNAS 와 DB 다.\n- NAS: 파일\n- DB: 테이블\n")
    assert "## 이관 > NAS\n\n### .part 를 쓰는 이유" in whole
    assert "지운 질문" not in whole and "## 이관 > DB" not in whole

    assert client.get("/api/export", params={"category": 999}).status_code == 404
    assert client.get("/api/export").status_code == 400


def test_rename_category(client):
    add_basics(client)
    top = client.get("/api/categories").json()["categories"][0]
    r = client.patch(f"/api/categories/{top['id']}", json={"version": top["version"], "name": "이관 사업"})
    assert r.status_code == 200 and r.json()["path"] == "이관 사업"
    assert client.get("/api/categories").json()["categories"][0]["children"][0]["path"] == "이관 사업 > NAS"
    stale = client.patch(f"/api/categories/{top['id']}", json={"version": top["version"], "name": "x"})
    assert stale.status_code == 409


def test_update_category_description(client):
    add_basics(client)
    top = client.get("/api/categories").json()["categories"][0]
    r = client.patch(f"/api/categories/{top['id']}", json={"version": top["version"], "description": "이관 사업 지식"})
    assert r.status_code == 200 and r.json()["description"] == "이관 사업 지식" and r.json()["name"] == "이관"
    both = client.patch(f"/api/categories/{top['id']}",
                        json={"version": r.json()["version"], "name": "이관 사업", "description": ""})
    assert both.status_code == 200 and (both.json()["name"], both.json()["description"]) == ("이관 사업", "")
    empty = client.patch(f"/api/categories/{top['id']}", json={"version": both.json()["version"]})
    assert empty.status_code == 400


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
