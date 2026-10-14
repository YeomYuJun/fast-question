"""로컬 프록시(run_mcp.py --remote): 근거는 이 PC 에서 캡처하고 원격 /mcp 에 저장한다."""
import asyncio
import codecs
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx2
from mcp.client import Client

from kb import remote as remote_mod
from kb import tenant
from mcp_server.proxy import Remote, build_proxy
from mcp_server.server import TOOLS
from tests.test_auth import URL, analyzer, app, cfg, idp, secrets_env  # noqa: F401 - 픽스처
from tests.test_remote_mcp import new_token

ROOT = Path(__file__).resolve().parent.parent


def via_proxy(app, token, work, transport=None):
    async def go():
        async with app.router.lifespan_context(app):
            remote = Remote(URL, token, http_client=lambda: httpx2.AsyncClient(
                transport=transport or httpx2.ASGITransport(app=app), base_url=URL,
                headers={"Authorization": f"Bearer {token}"}))
            async with Client(build_proxy(remote)) as c:
                return await work(c)
    return asyncio.run(go())


def call(app, token, name, args=None, transport=None):
    r = via_proxy(app, token, lambda c: c.call_tool(name, args or {}), transport)
    text = r.content[0].text
    return r.is_error, (text if r.is_error else json.loads(text))


def captured_of(cfg, acc, evidence_id) -> dict:
    conn = sqlite3.connect(tenant.db_path_for(cfg.data_dir, acc))
    try:
        return json.loads(conn.execute("SELECT captured_json FROM evidence WHERE id = ?", (evidence_id,)).fetchone()[0])
    finally:
        conn.close()


def local_file(tmp_path, text="값"):
    p = tmp_path / "내 PC 폴더" / "a.txt"
    p.parent.mkdir(exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_proxy_lists_the_remote_tools(cfg, app):
    _, token = new_token(cfg)
    tools = via_proxy(app, token, lambda c: c.list_tools()).tools
    assert {t.name for t in tools} == set(TOOLS)
    add = next(t for t in tools if t.name == "kb_add")
    assert "evidence" in add.input_schema["properties"]


def test_file_evidence_is_captured_on_this_pc(cfg, app, tmp_path):
    acc, token = new_token(cfg)
    src = local_file(tmp_path)
    assert not call(app, token, "kb_add_category", {"name": "이관"})[0]
    err, res = call(app, token, "kb_add", {"category": "이관", "question": "근거 질문", "answer": "근거 답이다.",
                                           "evidence": [{"kind": "file", "path": str(src)}]})
    assert not err, res
    ev = res["item"]["evidence"][0]
    assert ev["kind"] == "file" and res["item"]["evidence_state"] == "trusted"
    assert "sha256" in captured_of(cfg, acc, ev["id"])

    err, msg = call(app, token, "kb_add", {"category": "이관", "question": "없는 근거", "answer": "답이다.",
                                           "evidence": [{"kind": "file", "path": str(tmp_path / "없음.txt")}]})
    assert err and "근거 경로가 없다" in msg


def test_add_many_reads_local_file_and_reports_per_item(cfg, app, tmp_path):
    _, token = new_token(cfg)
    src = local_file(tmp_path)
    call(app, token, "kb_add_category", {"name": "이관"})
    items = [
        {"category": "이관", "question": "첫 질문", "answer": "첫 답이다.", "evidence": [{"kind": "file", "path": str(src)}]},
        {"category": "이관", "question": "둘째 질문", "answer": "둘째 답이다.",
         "evidence": [{"kind": "file", "path": str(tmp_path / "없음.txt")}]},
        {"category": "없는 분류", "question": "셋째 질문", "answer": "셋째 답이다."},
        {"category": "이관", "question": "넷째 질문", "answer": "넷째 답이다."},
    ]
    draft = tmp_path / "초안.json"
    draft.write_bytes(codecs.BOM_UTF8 + json.dumps(items, ensure_ascii=False).encode("utf-8"))

    err, res = call(app, token, "kb_add_many", {"file": str(draft)})

    assert not err, res
    assert [(r["index"], r["status"]) for r in res["results"]] == [(0, "created"), (1, "error"), (2, "error"), (3, "created")]
    assert "근거 경로가 없다" in res["results"][1]["error"]
    assert res["created"] == 2


def test_recapture_uses_this_pc(cfg, app, tmp_path):
    acc, token = new_token(cfg)
    src = local_file(tmp_path)
    call(app, token, "kb_add_category", {"name": "이관"})
    _, res = call(app, token, "kb_add", {"category": "이관", "question": "근거 질문", "answer": "근거 답이다.",
                                         "evidence": [{"kind": "file", "path": str(src)}]})
    ev = res["item"]["evidence"][0]
    before = captured_of(cfg, acc, ev["id"])
    src.write_text("바뀐 값", encoding="utf-8")

    err, res = call(app, token, "kb_evidence", {"action": "recapture", "evidence_id": ev["id"]})

    assert not err, res
    assert captured_of(cfg, acc, ev["id"]) != before


def test_unreachable_server_is_a_tool_error(cfg, app):
    _, token = new_token(cfg)

    def refuse(request):
        raise httpx2.ConnectError("연결 거부")

    err, msg = call(app, token, "kb_search", {"query": "x"}, transport=httpx2.MockTransport(refuse))
    assert err and "호출 실패" in msg


def test_credentials_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "한글 폴더" / "credentials.json"
    monkeypatch.setenv("FQ_CREDENTIALS", str(path))
    assert remote_mod.load_credentials() is None
    remote_mod.save_credentials("https://fq.example.com/", "fq_pat_abc")
    assert not path.read_bytes().startswith(codecs.BOM_UTF8)
    cred = remote_mod.load_credentials()
    assert (cred["url"], cred["token"]) == ("https://fq.example.com", "fq_pat_abc") and len(cred["device_id"]) == 36
    path.write_bytes(codecs.BOM_UTF8 + path.read_bytes())  # 메모장이 BOM 을 붙여 저장해도 읽는다
    assert remote_mod.load_credentials()["token"] == "fq_pat_abc"
    path.write_text('{"url": "https://fq.example.com", "token": "garbage"}', encoding="utf-8")
    assert remote_mod.load_credentials() is None


def test_run_mcp_remote_without_credentials_exits(tmp_path):
    env = {**os.environ, "FQ_CREDENTIALS": str(tmp_path / "없음.json")}
    r = subprocess.run([sys.executable, str(ROOT / "run_mcp.py"), "--remote"], capture_output=True, env=env, timeout=60)
    # 표준 오류는 콘솔 코드 페이지(CP949)로 나온다. ASCII 부분으로 확인한다
    assert r.returncode == 1 and b"install.py --remote" in r.stderr
