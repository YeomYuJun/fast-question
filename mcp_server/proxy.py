"""로컬 프록시 MCP(stdio). Claude Code 가 `run_mcp.py --remote` 로 띄우고, 원격 <url>/mcp 를 개인 토큰으로 부른다.

도구 목록은 원격 서버의 것을 그대로 내보낸다(같은 18개 이름·인자). 호출은 그대로 넘기되, 사용자 PC 에서만 할 수
있는 일을 여기서 먼저 한다.
- 근거 spec(kb_add, kb_add_many 항목, kb_evidence add)은 이 PC 에서 capture() 해 captured 로 붙인다(url·note 제외).
- kb_add_many file= 은 이 PC 의 파일을 읽어 items 로 보낸다. 캡처에 실패한 항목은 그 항목만 오류로 돌려준다.
- kb_evidence recapture 는 원격에서 근거 spec 을 받아 이 PC 에서 다시 캡처한 값을 보낸다.
- 캡처값에는 이 PC 의 device_id 를 origin 으로 붙인다. kb_recheck 는 원격에서 이 PC 의 근거만 받아
  (/api/evidence/check) 이 PC 에서 판정하고 /api/evidence/verdicts 로 보고한다.
호출마다 MCP 세션을 새로 연다(원격은 세션을 메모리에 두지 않는다).
"""
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

import anyio
import httpx2
import mcp_types as types
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel.server import Server

from kb.errors import KbError
from kb.evidence import capture
from kb.remote import judge
from mcp_server.instructions import INSTRUCTIONS

SERVER_KINDS = ("url", "note")  # 서버가 직접 만드는 근거


def localize(spec, origin: str = ""):
    if not isinstance(spec, dict) or (spec.get("kind") or "auto") in SERVER_KINDS:
        return spec
    row = capture(spec)
    if origin and row["captured_json"] is not None:
        row["captured_json"] = json.dumps({**json.loads(row["captured_json"]), "origin": origin}, ensure_ascii=False)
    return {**spec, "captured": row}


def text_result(data, is_error: bool = False) -> types.CallToolResult:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)


class Remote:
    def __init__(self, url: str, token: str, device_id: str = "",
                 http_client: Callable[[], httpx2.AsyncClient] | None = None):
        self.base = url.rstrip("/")
        self.endpoint = f"{self.base}/mcp"
        self.device_id = device_id
        self._http = http_client or (lambda: httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(30.0, read=300.0)))

    @asynccontextmanager
    async def session(self):
        async with self._http() as h:
            async with Client(streamable_http_client(self.endpoint, http_client=h)) as c:
                yield c

    async def call(self, name: str, args: dict) -> types.CallToolResult:
        async with self.session() as c:
            return await c.call_tool(name, args)

    async def tools(self) -> list[types.Tool]:
        async with self.session() as c:
            return (await c.list_tools()).tools


def _prepare_many(args: dict, origin: str = "") -> tuple[list, list[int], dict[int, str]]:
    """(보낼 항목, 보낼 항목의 원래 번호, 이 PC 에서 실패한 항목 번호 → 오류)."""
    items = args.get("items")
    if args.get("file"):
        # 윈도우에서 만든 JSON 은 BOM 이 붙어 온다
        items = json.loads(Path(args["file"]).read_text(encoding="utf-8-sig"))
    send, index, errors = [], [], {}
    for n, it in enumerate(items or []):
        try:
            if isinstance(it, dict) and it.get("evidence"):
                it = {**it, "evidence": [localize(e, origin) for e in it["evidence"]]}
        except KbError as e:
            errors[n] = e.message
            continue
        send.append(it)
        index.append(n)
    return send, index, errors


async def _add_many(remote: Remote, args: dict) -> types.CallToolResult:
    send, index, errors = await anyio.to_thread.run_sync(_prepare_many, args, remote.device_id)
    results: list[dict] = []
    if send or not errors:  # 항목이 없으면 원격이 오류 문구를 만든다
        res = await remote.call("kb_add_many", {"items": send})
        if res.is_error:
            return res
        results = json.loads(res.content[0].text)["results"]
        for r in results:
            r["index"] = index[r["index"]]
    results += [{"index": n, "status": "error", "error": msg} for n, msg in errors.items()]
    results.sort(key=lambda r: r["index"])
    return text_result({"results": results, "created": sum(r["status"] == "created" for r in results)})


async def _recapture(remote: Remote, args: dict) -> types.CallToolResult:
    got = await remote.call("kb_evidence", {"action": "spec", "evidence_id": args.get("evidence_id")})
    if got.is_error:
        return got
    spec = json.loads(got.content[0].text)["spec"]
    local = await anyio.to_thread.run_sync(localize, spec, remote.device_id)
    extra = {"spec": {"captured": local["captured"]}} if "captured" in local else {}
    return await remote.call("kb_evidence", {"action": "recapture", "evidence_id": args.get("evidence_id"), **extra})


async def _recheck(remote: Remote, args: dict) -> types.CallToolResult:
    if not remote.device_id:
        return text_result("credentials.json 에 device_id 가 없다. fast-question login(저장소 설치는 "
                           "scripts/install.py --remote)으로 다시 만든다.", is_error=True)
    params = {"origin": remote.device_id}
    if args.get("id") is not None:
        params["qa"] = str(args["id"])
    async with remote._http() as h:
        got = await h.get(f"{remote.base}/api/evidence/check", params=params)
        got.raise_for_status()
        results = await anyio.to_thread.run_sync(judge, got.json()["rows"])
        sent = await h.post(f"{remote.base}/api/evidence/verdicts", json={"results": results})
        sent.raise_for_status()
    return text_result(sent.json())


async def forward(remote: Remote, name: str, args: dict) -> types.CallToolResult:
    if name == "kb_recheck":
        return await _recheck(remote, args)
    if name == "kb_add_many":
        return await _add_many(remote, args)
    if name == "kb_evidence" and args.get("action") == "recapture":
        return await _recapture(remote, args)
    if name == "kb_add" and args.get("evidence"):
        args = {**args, "evidence": await anyio.to_thread.run_sync(
            lambda: [localize(e, remote.device_id) for e in args["evidence"]])}
    if name == "kb_evidence" and args.get("action") == "add" and args.get("spec"):
        args = {**args, "spec": await anyio.to_thread.run_sync(localize, args["spec"], remote.device_id)}
    return await remote.call(name, args)


def build_proxy(remote: Remote) -> Server:
    cache: list[types.Tool] = []

    async def list_tools(ctx, params) -> types.ListToolsResult:
        if not cache:
            cache.extend(await remote.tools())
        return types.ListToolsResult(tools=cache)

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            return await forward(remote, params.name, dict(params.arguments or {}))
        except KbError as e:
            return text_result(e.message, is_error=True)
        except (OSError, ValueError) as e:
            return text_result(f"{type(e).__name__}: {e}", is_error=True)
        except Exception as e:  # noqa: BLE001 - 연결 실패 등은 도구 오류로 돌려 Claude 가 읽게 한다
            return text_result(f"원격 서버 {remote.endpoint} 호출 실패: {type(e).__name__}: {e}", is_error=True)

    return Server("fast-question", instructions=INSTRUCTIONS, on_list_tools=list_tools, on_call_tool=call_tool)


async def run_stdio(remote: Remote) -> None:
    from mcp.server.stdio import stdio_server
    server = build_proxy(remote)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
