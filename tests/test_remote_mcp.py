"""원격 MCP(<public_url>/mcp): 개인 토큰의 계정 DB 에 claude 주체로 쓴다. 파일 근거는 캡처값으로만 받는다."""
import asyncio
import json
import sqlite3

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient

from kb import control, tenant
from kb.evidence import capture
from mcp_server.server import TOOLS
from tests.test_auth import URL, analyzer, app, cfg, idp, secrets_env  # noqa: F401 - 픽스처


def new_token(cfg) -> tuple[int, str]:
    conn = control.connect_control(tenant.control_path(cfg.data_dir))
    try:
        acc = control.create_account(conn)
        return acc, control.create_personal_token(conn, acc, "테스트 PC")["token"]
    finally:
        conn.close()


def session(app, token, work):
    async def go():
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=URL,
                                          headers={"Authorization": f"Bearer {token}"}) as h:
                async with Client(streamable_http_client(f"{URL}/mcp", http_client=h)) as c:
                    return await work(c)
    return asyncio.run(go())


def call(app, token, name, args=None):
    r = session(app, token, lambda c: c.call_tool(name, args or {}))
    text = r.content[0].text
    return r.is_error, (text if r.is_error else json.loads(text))


def seed(app, token):
    assert not call(app, token, "kb_add_category", {"name": "이관"})[0]


def test_tool_list_is_the_same(cfg, app):
    _, token = new_token(cfg)
    names = session(app, token, lambda c: c.list_tools())
    assert {t.name for t in names.tools} == set(TOOLS)


def test_add_and_search_as_claude_in_own_db(cfg, app):
    acc, token = new_token(cfg)
    seed(app, token)
    err, res = call(app, token, "kb_add", {"category": "이관", "question": ".part 를 쓰는 이유",
                                           "answer": "다 쓴 뒤 rename 으로 확정한다."})
    assert not err and res["item"]["created_by"] == "claude"
    err, found = call(app, token, "kb_search", {"query": "rename"})
    assert [h["id"] for h in found["hits"]] == [res["item"]["id"]]
    assert tenant.db_path_for(cfg.data_dir, acc).exists()


def test_accounts_are_isolated(cfg, app):
    _, a = new_token(cfg)
    _, b = new_token(cfg)
    seed(app, a)
    call(app, a, "kb_add", {"category": "이관", "question": "A 의 질문", "answer": "A 의 답이다."})
    assert call(app, b, "kb_search", {"query": "질문"})[1]["hits"] == []
    assert call(app, b, "kb_categories")[1]["categories"] == []


def test_mcp_without_valid_token_is_401(cfg, app):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(app, base_url=URL) as c:
        for auth in (None, "Bearer fq_pat_garbage", "Bearer garbage"):
            h = dict(headers, **({"Authorization": auth} if auth else {}))
            r = c.post("/mcp", json=body, headers=h)
            assert r.status_code == 401, auth
            assert "resource_metadata" in r.headers.get("www-authenticate", "")
        meta = c.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == f"{URL}/mcp"


def stored_capture(cfg, acc, evidence_id) -> dict:
    conn = sqlite3.connect(tenant.db_path_for(cfg.data_dir, acc))
    try:
        return json.loads(conn.execute("SELECT captured_json FROM evidence WHERE id = ?", (evidence_id,)).fetchone()[0])
    finally:
        conn.close()


def test_path_evidence_needs_captured_value(cfg, app, tmp_path):
    acc, token = new_token(cfg)
    seed(app, token)
    src = tmp_path / "사용자 PC" / "a.txt"
    src.parent.mkdir()
    src.write_text("값", encoding="utf-8")
    q = {"category": "이관", "question": "근거 질문", "answer": "근거가 있는 답이다."}

    err, msg = call(app, token, "kb_add", {**q, "evidence": [{"kind": "file", "path": str(src)}]})
    assert err and "로컬 프록시" in msg

    row = capture({"kind": "file", "path": str(src)})
    err, res = call(app, token, "kb_add", {**q, "evidence": [{"kind": "file", "path": str(src), "captured": row}]})
    assert not err, res
    ev = res["item"]["evidence"][0]
    assert ev["path"] == str(src.resolve()) and ev["verdict"] == "same"
    assert res["item"]["evidence_state"] == "trusted"

    src.write_text("바뀐 값", encoding="utf-8")
    again = capture({"kind": "file", "path": str(src)})
    err, res = call(app, token, "kb_evidence", {"action": "recapture", "evidence_id": ev["id"],
                                                "spec": {"captured": again}})
    assert not err and res["evidence"][0]["version"] == ev["version"] + 1
    assert stored_capture(cfg, acc, ev["id"]) == json.loads(again["captured_json"])
    assert json.loads(again["captured_json"]) != json.loads(row["captured_json"])


@pytest.mark.parametrize("captured, needle", [
    ({"kind": "auto", "path": "x"}, "kind"),
    ({"kind": "file", "path": "x", "captured_json": "{not json"}, "JSON"),
    ({"kind": "file", "path": "x" * 70000}, "KB"),
    ({"kind": "note", "captured_json": "{}"}, "url·note"),
])
def test_bad_captured_value_is_refused(cfg, app, captured, needle):
    _, token = new_token(cfg)
    seed(app, token)
    err, msg = call(app, token, "kb_add", {"category": "이관", "question": "질문", "answer": "답이다.",
                                           "evidence": [{"kind": "file", "captured": captured}]})
    assert err and needle in msg


def test_url_and_note_evidence_still_work(cfg, app):
    _, token = new_token(cfg)
    seed(app, token)
    err, res = call(app, token, "kb_add", {"category": "이관", "question": "질문", "answer": "답이다.",
                                           "evidence": [{"kind": "url", "url": "https://example.com/doc"},
                                                        {"kind": "note", "text": "사용자가 화면에서 확인"}]})
    assert not err and res["item"]["evidence_state"] == "external"


@pytest.mark.parametrize("name, args, needle", [
    ("kb_add_many", {"file": "C:/x.json"}, "로컬 프록시"),
    ("kb_recheck", {}, "로컬 검사기"),
    ("kb_dict", {"action": "user_word_add", "word": "망연계"}, "사용자 단어"),
])
def test_server_side_file_work_is_refused(cfg, app, name, args, needle):
    _, token = new_token(cfg)
    err, msg = call(app, token, name, args)
    assert err and needle in msg


def test_synonyms_still_work_remotely(cfg, app):
    _, token = new_token(cfg)
    err, res = call(app, token, "kb_dict", {"action": "synonym_add", "terms": ["룰", "규칙"]})
    assert not err and res["snapshot"]["synonyms"]
