import asyncio
import json

import pytest
from mcp.client import Client

from kb.db import connect
from mcp_server.server import TOOLS, build_server, store_factory

EXPECTED_TOOLS = {"kb_search", "kb_get", "kb_categories", "kb_add_category", "kb_add", "kb_update",
                  "kb_archive", "kb_restore", "kb_rename_category", "kb_delete_category",
                  "kb_link", "kb_evidence", "kb_recheck", "kb_dict", "kb_add_many", "kb_harvest",
                  "kb_cleanup_list", "kb_cleanup_resolve"}
WEB = "http://127.0.0.1:9999"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "kb.sqlite3"
    connect(path).close()
    return path


@pytest.fixture
def server(db):
    return build_server(store_factory(db, None), web_url=WEB)


def call(server, name, args=None):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(name, args or {})
    r = asyncio.run(go())
    text = r.content[0].text
    return r.is_error, (text if r.is_error else json.loads(text))


def test_tool_list(server):
    async def go():
        async with Client(server) as c:
            return {t.name for t in (await c.list_tools()).tools}
    assert asyncio.run(go()) == EXPECTED_TOOLS == set(TOOLS)


def test_add_then_search_as_claude(server):
    assert not call(server, "kb_add_category", {"name": "이관"})[0]
    err, res = call(server, "kb_add", {"category": "이관", "question": ".part 를 쓰는 이유",
                                       "answer": "다 쓴 뒤 rename 으로 확정한다."})
    assert not err and res["status"] == "created"
    assert res["item"]["created_by"] == "claude" and "reviewed" not in res["item"]

    err, found = call(server, "kb_search", {"query": "rename"})
    assert not err
    assert [h["id"] for h in found["hits"]] == [res["item"]["id"]]
    assert found["hits"][0]["tier"] == "exact"
    assert "reviewed" not in found["hits"][0]
    assert "spans" not in found["hits"][0]


def test_results_carry_web_address(server):
    call(server, "kb_add_category", {"name": "이관"})
    item = call(server, "kb_add", {"category": "이관", "question": ".part 를 쓰는 이유",
                                   "answer": "다 쓴 뒤 rename 으로 확정한다."})[1]["item"]
    url = f"{WEB}/#qa/{item['id']}"
    assert item["url"] == url
    assert call(server, "kb_get", {"id": item["id"]})[1]["url"] == url
    assert call(server, "kb_search", {"query": "rename"})[1]["hits"][0]["url"] == url
    err, updated = call(server, "kb_update", {"id": item["id"], "version": item["version"],
                                              "answer": "다 쓴 뒤 rename 으로 이름을 확정한다."})
    assert not err and updated["url"] == url

    err, res = call(server, "kb_add", {"category": "이관", "question": ".part 를 쓰는 까닭",
                                       "answer": "rename 으로 확정한다."})
    assert res["status"] == "similar_found" and res["similar"][0]["url"] == url

    many = call(server, "kb_add_many", {"items": [
        {"category": "이관", "question": "PETRA 를 제거하는 이유", "answer": "대상 DB 에서 쓸 수 없다."}]})[1]
    created = many["results"][0]
    assert created["url"] == f"{WEB}/#qa/{created['id']}"


def test_similar_question_reported(server):
    call(server, "kb_add_category", {"name": "이관"})
    call(server, "kb_add", {"category": "이관", "question": ".nas-tmp 가 NAS 안에 있는 이유", "answer": "같은 볼륨"})
    err, res = call(server, "kb_add", {"category": "이관", "question": ".nas-tmp 가 NAS 안에 있는 까닭",
                                       "answer": "같은 볼륨이어야 한다."})
    assert not err and res["status"] == "similar_found"


def test_validation_error_is_tool_error(server):
    call(server, "kb_add_category", {"name": "이관"})
    err, text = call(server, "kb_add", {"category": "이관", "question": "질문", "answer": "1\n- 2\n- 3\n- 4\n- 5\n- 6\n- 7"})
    assert err and "6줄까지" in text


def test_format_rules_reject_claude_writes(server):
    call(server, "kb_add_category", {"name": "이관"})
    err, text = call(server, "kb_add", {"category": "이관", "question": "세 문장 답의 질문",
                                        "answer": "하나다. 둘이다. 셋이다."})
    assert err and "3문장" in text and "rules.md" in text
    assert call(server, "kb_search", {"query": "세 문장"})[1]["total"] == 0

    item = call(server, "kb_add", {"category": "이관", "question": "운영 DB 위치", "answer": "설정 파일에 있다."})[1]["item"]
    err, text = call(server, "kb_update", {"id": item["id"], "version": item["version"],
                                           "answer": "10.20.30.40 에 있다."})
    assert err and "IP 주소" in text
    err, text = call(server, "kb_evidence", {"action": "add", "id": item["id"],
                                             "spec": {"kind": "note", "text": "hong@example.com 확인"}})
    assert err and "이메일" in text


def test_format_rules_do_not_apply_to_user_writes(db):
    from kb.store import Store
    s = Store(connect(db), "user")
    s.add_category("이관")
    assert s.add_qa("이관", "사람이 쓴 질문", "하나다. 둘이다. 셋이다.")["status"] == "created"


def test_conflict_reports_current_value(server):
    call(server, "kb_add_category", {"name": "이관"})
    item = call(server, "kb_add", {"category": "이관", "question": "질문 하나", "answer": "답변 하나"})[1]["item"]
    call(server, "kb_update", {"id": item["id"], "version": item["version"], "answer": "고친 답변"})
    err, text = call(server, "kb_update", {"id": item["id"], "version": item["version"], "answer": "또 고친 답변"})
    assert err and "현재 값" in text and "고친 답변" in text


def test_dictionary_and_synonym_search(server):
    call(server, "kb_add_category", {"name": "이관"})
    call(server, "kb_add", {"category": "이관", "question": "API Gateway 라우팅 룰 출처", "answer": "NHN 콘솔에만 있다."})
    err, res = call(server, "kb_dict", {"action": "synonym_add", "terms": ["규칙", "룰"]})
    assert not err and res["reindex_needed"] is False
    hits = call(server, "kb_search", {"query": "라우팅 규칙"})[1]["hits"]
    assert hits and hits[0]["tier"] == "synonym"


def test_evidence_add_and_recapture(server, tmp_path):
    f = tmp_path / "근거 파일.txt"
    f.write_text("v1", encoding="utf-8")
    call(server, "kb_add_category", {"name": "이관"})
    item = call(server, "kb_add", {"category": "이관", "question": "근거 질문", "answer": "근거 답변",
                                   "evidence": [{"kind": "auto", "path": str(f)}]})[1]["item"]
    assert item["evidence_state"] == "trusted"

    f.write_text("v2", encoding="utf-8")
    err, res = call(server, "kb_recheck", {"id": item["id"]})
    assert not err and res["items"][0]["evidence_state"] == "stale"

    ev_id = call(server, "kb_get", {"id": item["id"]})[1]["evidence"][0]["id"]
    err, after = call(server, "kb_evidence", {"action": "recapture", "evidence_id": ev_id})
    assert not err and after["evidence_state"] == "trusted"


def test_archive_and_link(server):
    call(server, "kb_add_category", {"name": "이관"})
    a = call(server, "kb_add", {"category": "이관", "question": "첫째 질문", "answer": "첫째 답변"})[1]["item"]
    b = call(server, "kb_add", {"category": "이관", "question": "PETRA 제거 이유", "answer": "둘째 답변"})[1]["item"]
    assert not call(server, "kb_link", {"a": a["id"], "b": b["id"]})[0]
    assert [l["id"] for l in call(server, "kb_get", {"id": a["id"]})[1]["links"]] == [b["id"]]
    err, archived = call(server, "kb_archive", {"id": a["id"], "version": a["version"], "reason": "중복"})
    assert not err and archived["archived"] is True


def test_restore_after_archive(server):
    call(server, "kb_add_category", {"name": "이관"})
    a = call(server, "kb_add", {"category": "이관", "question": "첫째 질문", "answer": "첫째 답변"})[1]["item"]
    archived = call(server, "kb_archive", {"id": a["id"], "version": a["version"]})[1]
    err, restored = call(server, "kb_restore", {"id": a["id"], "version": archived["version"], "reason": "실수"})
    assert not err and restored["archived"] is False
    assert restored["revisions"][0]["op"] == "restore"


def test_rename_category(server):
    top = call(server, "kb_add_category", {"name": "이관"})[1]
    call(server, "kb_add_category", {"name": "NAS", "parent": "이관"})
    err, renamed = call(server, "kb_rename_category", {"category": "이관", "version": top["version"],
                                                       "name": "이관 사업"})
    assert not err and renamed["path"] == "이관 사업"
    tree = call(server, "kb_categories")[1]["categories"]
    assert tree[0]["children"][0]["path"] == "이관 사업 > NAS"
    err, msg = call(server, "kb_rename_category", {"category": "이관 사업", "version": top["version"], "name": "x"})
    assert err and "먼저 고쳤다" in msg


def test_delete_category_needs_confirm(server):
    top = call(server, "kb_add_category", {"name": "이관"})[1]
    call(server, "kb_add_category", {"name": "NAS", "parent": "이관"})
    a = call(server, "kb_add", {"category": "이관 > NAS", "question": "첫째 질문", "answer": "첫째 답변"})[1]["item"]

    err, preview = call(server, "kb_delete_category", {"category": "이관", "version": top["version"]})
    assert not err and preview["deleted"] is False
    assert (preview["children"], preview["qa"]) == (1, 1)
    assert "1건" in preview["warning"] and "confirm" in preview["warning"]
    assert call(server, "kb_categories")[1]["categories"][0]["name"] == "이관"

    err, done = call(server, "kb_delete_category", {"category": "이관", "version": top["version"], "confirm": True})
    assert not err and done["deleted"] is True
    assert (done["archived_children"], done["archived_qa"]) == (1, 1)
    assert call(server, "kb_categories")[1]["categories"] == []
    assert call(server, "kb_get", {"id": a["id"]})[1]["archived"] is True


def test_categories_tree(server):
    call(server, "kb_add_category", {"name": "이관"})
    call(server, "kb_add_category", {"name": "NAS", "parent": "이관"})
    err, tree = call(server, "kb_categories")
    assert not err and tree["categories"][0]["children"][0]["path"] == "이관 > NAS"


def test_parallel_tool_calls_use_separate_connections(server, db):
    async def go():
        async with Client(server) as c:
            await c.call_tool("kb_add_category", {"name": "이관"})
            return await asyncio.gather(*[
                c.call_tool("kb_add", {"category": "이관", "question": f"병렬 등록 확인 {i} 호 {i * 7919}",
                                       "answer": "병렬 답변", "force": True}) for i in range(30)])
    results = asyncio.run(go())
    assert [r.content[0].text[:80] for r in results if r.is_error] == []
    c = connect(db)
    assert c.execute("SELECT count(*) FROM qa").fetchone()[0] == 30
    c.close()


def test_search_syntax_queries_and_regex(server):
    call(server, "kb_add_category", {"name": "이관"})
    for q, a in [("PETRA 를 제거하는 이유", "대상 DB 에서 쓸 수 없다."), ("PETRA 데이터 이관 순서", "복호화 후 옮긴다."),
                 ("TypeHandler 가 맡는 일", "PLS_ 호출(507)을 대신한다.")]:
        call(server, "kb_add", {"category": "이관", "question": q, "answer": a, "force": True})
    err, res = call(server, "kb_search", {"query": "PETRA -제거"})
    assert not err and [h["question"] for h in res["hits"]] == ["PETRA 데이터 이관 순서"]
    err, res = call(server, "kb_search", {"queries": ["PETRA", "제거"]})
    assert not err and res["hits"][0]["matched"] == ["PETRA", "제거"] and res["total"] == 2
    err, res = call(server, "kb_search", {"query": r"\(\d{3}\)", "regex": True})
    assert not err and [h["question"] for h in res["hits"]] == ["TypeHandler 가 맡는 일"]
    err, text = call(server, "kb_search", {"query": "(a+)+", "regex": True})
    assert err and "중첩" in text


def test_add_many_reports_each_item(server):
    call(server, "kb_add_category", {"name": "이관"})
    call(server, "kb_add", {"category": "이관", "question": ".nas-tmp 가 NAS 안에 있는 이유", "answer": "같은 볼륨"})
    err, res = call(server, "kb_add_many", {"items": [
        {"category": "이관", "question": "PETRA 를 제거하는 이유", "answer": "대상 DB 에서 쓸 수 없다."},
        {"category": "이관", "question": ".nas-tmp 가 NAS 안에 있는 까닭", "answer": "같은 볼륨이어야 한다."},
        {"category": "이관", "question": "세 문장 답", "answer": "하나다. 둘이다. 셋이다."},
        {"category": "없는 분류", "question": "분류 없음", "answer": "실패한다."},
    ]})
    assert not err
    assert [r["status"] for r in res["results"]] == ["created", "similar_found", "error", "error"]
    assert res["created"] == 1 and res["results"][1]["similar"][0]["question"].startswith(".nas-tmp")
    assert "3문장" in res["results"][2]["error"]
    assert call(server, "kb_search", {"query": "PETRA"})[1]["total"] == 1


def test_harvest_record_roundtrip(server):
    err, res = call(server, "kb_harvest", {"action": "get", "root": r"D:\work"})
    assert not err and res["record"] is None
    state = {"repos": {"D:/work/app": "abc123"}, "created": 12}
    err, res = call(server, "kb_harvest", {"action": "set", "root": "D:/work/", "state": state})
    assert not err and res["record"]["repos"] == state["repos"] and res["record"]["at"]
    err, res = call(server, "kb_harvest", {"action": "get", "root": r"d:\work"})
    assert res["record"]["created"] == 12
    err, res = call(server, "kb_harvest", {"action": "list"})
    assert [r["root"] for r in res["records"]] == ["d:/work"]


def test_add_many_reads_draft_file(server, tmp_path):
    call(server, "kb_add_category", {"name": "이관"})
    draft = tmp_path / "draft.json"
    items = [{"category": "이관", "question": "재암호화 대상에서 뺀 컬럼은?", "answer": "길이가 맞지 않는 컬럼은 대상에서 뺐다."}]
    draft.write_bytes(b"\xef\xbb\xbf" + json.dumps(items, ensure_ascii=False).encode("utf-8"))
    err, res = call(server, "kb_add_many", {"file": str(draft)})
    assert not err and res["created"] == 1
    assert call(server, "kb_search", {"query": "뺐다"})[1]["hits"][0]["answer"] == items[0]["answer"]
    err, text = call(server, "kb_add_many", {})
    assert err and "items 나 file" in text


def test_add_many_description_offers_file_only_locally(db):
    async def docs(srv):
        async with Client(srv) as c:
            return {t.name: t.description for t in (await c.list_tools()).tools}
    local = asyncio.run(docs(build_server(store_factory(db, None), web_url=WEB)))["kb_add_many"]
    remote = asyncio.run(docs(build_server(store_factory(db, None), web_url=WEB, remote=True)))["kb_add_many"]
    assert "초안 파일을 그대로" in local
    assert "초안 파일을 그대로" not in remote and "file 을 쓸 수 없다" in remote
