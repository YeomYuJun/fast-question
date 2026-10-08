import asyncio
import json

import pytest
from mcp.client import Client

from kb.db import connect
from mcp_server.server import TOOLS, build_server, store_factory

EXPECTED_TOOLS = {"kb_search", "kb_get", "kb_categories", "kb_add_category", "kb_add", "kb_update",
                  "kb_archive", "kb_link", "kb_evidence", "kb_recheck", "kb_dict"}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "kb.sqlite3"
    connect(path).close()
    return path


@pytest.fixture
def server(db):
    return build_server(store_factory(db, None))


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


def test_similar_question_reported(server):
    call(server, "kb_add_category", {"name": "이관"})
    call(server, "kb_add", {"category": "이관", "question": ".nas-tmp 가 NAS 안에 있는 이유", "answer": "같은 볼륨"})
    err, res = call(server, "kb_add", {"category": "이관", "question": ".nas-tmp 가 NAS 안에 있는 까닭",
                                       "answer": "같은 볼륨이어야 한다."})
    assert not err and res["status"] == "similar_found"


def test_validation_error_is_tool_error(server):
    call(server, "kb_add_category", {"name": "이관"})
    err, text = call(server, "kb_add", {"category": "이관", "question": "질문", "answer": "1\n2\n3"})
    assert err and "2줄까지" in text


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
