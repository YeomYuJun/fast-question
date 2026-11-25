"""로컬 프록시 MCP(stdio). Claude Code 가 `run_mcp.py --remote` 로 띄우고, 원격 <url>/mcp 를 개인 토큰으로 부른다.

도구 목록은 원격 서버의 것을 그대로 내보낸다(같은 21개 이름·인자·주석). kb_add_many 설명만 file 을 이 PC 에서 읽으므로
로컬 서버의 설명으로 바꿔 내보낸다. 호출은 그대로 넘기되, 사용자 PC 에서만 할 수 있는 일을 여기서 먼저 한다.
- 근거 spec(kb_add, kb_add_many 항목, kb_evidence add)은 이 PC 에서 capture() 해 captured 로 붙인다(url·note 제외).
- kb_add_many file= 은 이 PC 의 파일을 읽어 items 로 보낸다. 캡처에 실패한 항목은 그 항목만 오류로 돌려준다.
- kb_evidence recapture 는 원격 kb_evidence_spec 으로 근거 spec 을 받아 이 PC 에서 다시 캡처한 값을 보낸다.
- kb_search 결과에서 이 PC 가 학습용으로 둔 대분류의 항목을 study 키로 떼어 낸다(set_apart, settings.json).
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

from kb import uselog
from kb.errors import KbError
from kb.evidence import capture
from kb.remote import judge, study_categories
from mcp_server.instructions import ADD_MANY_DOC, ADD_MANY_FILE, PROXY_INSTRUCTIONS, RECHECK_DOC

SERVER_KINDS = ("url", "note", "github")  # 서버가 직접 만드는 근거(github 는 서버가 GitHub API 로 기록·판정한다)
LOCAL_DOCS = {"kb_add_many": ADD_MANY_DOC + ADD_MANY_FILE, "kb_recheck": RECHECK_DOC}


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
    got = await remote.call("kb_evidence_spec", {"evidence_id": args.get("evidence_id")})
    if got.is_error:
        return got
    spec = json.loads(got.content[0].text)["spec"]
    local = await anyio.to_thread.run_sync(localize, spec, remote.device_id)
    extra = {"spec": {"captured": local["captured"]}} if "captured" in local else {}
    return await remote.call("kb_evidence", {"action": "recapture", "evidence_id": args.get("evidence_id"), **extra})


async def _recheck(remote: Remote, args: dict) -> types.CallToolResult:
    if not remote.device_id:
        return text_result("credentials.json has no device_id. Create it again with fast-question login "
                           "(for a repository install, scripts/install.py --remote).", is_error=True)
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


STUDY_NOTE = ("Learning notes: general knowledge the user keeps for study, set apart by study_categories in "
              "settings.json on this PC. They are not facts about the user's projects. Use them only when the user "
              "asks about that topic. Passing category searches them directly.")


def set_apart(args: dict, result: types.CallToolResult, study: list[str]) -> types.CallToolResult:
    """kb_search 결과에서 학습용 대분류(kb/remote.py study_categories)의 항목을 hits·similar 에서 빼 study 키로 옮긴다.
    Claude 가 프로젝트의 사실을 찾을 때 일반 지식이 섞이지 않게 한다. category 를 준 검색은 그 분류를 직접 찾는 것이라
    그대로 둔다. total·groups 는 서버가 센 값 그대로다. 결과 형식이 다르면 손대지 않는다."""
    try:
        if not study or result.is_error or args.get("category"):
            return result
        data = json.loads(result.content[0].text)
        moved = {k: [h for h in data.get(k, []) if str(h.get("category_path", "")).split(" > ")[0].strip().lower() in study]
                 for k in ("hits", "similar")}
        if not any(moved.values()):
            return result
        for k, gone in moved.items():
            data[k] = [h for h in data.get(k, []) if h not in gone]
        data["study"] = {"note": STUDY_NOTE, **moved}
        return text_result(data)
    except Exception:  # noqa: BLE001 - 떼어 내지 못하면 서버 결과를 그대로 돌려준다
        return result


USE_KINDS = {"kb_search": "search", "kb_get": "get", "kb_add": "add", "kb_update": "update", "kb_archive": "archive",
             "kb_evidence": "evidence"}


def log_use(name: str, args: dict, result: types.CallToolResult) -> None:
    """찾아지거나 읽히거나 등록되거나 고쳐진 항목을 이 PC 의 사용 기록(kb/uselog.py)에 적는다. 실패는 버린다.
    kb_search 가 0건이면 Claude 가 넣은 검색어를 miss 로 적는다. 정규식 검색과 지운 항목까지 보는 검색은 글자 패턴이나
    복원 대상을 찾는 것이라 적지 않는다."""
    kind = USE_KINDS.get(name)
    try:
        if kind is None or result.is_error:
            return
        data = json.loads(result.content[0].text)
        if kind == "search":
            items = [(h["id"], h["question"]) for h in data.get("hits", [])[:uselog.SEARCH_TOP]]
            if not items and not args.get("regex") and not args.get("include_archived"):
                uselog.record("miss", [], queries=args.get("queries") or [args.get("query")])
        elif kind == "add":  # {"status": "created", "item": {...}}. similar_found 는 등록하지 않은 것이다
            item = data.get("item") or {}
            items = [(item["id"], item.get("question", ""))] if data.get("status") == "created" else []
        else:
            items = [(data["id"], data.get("question", ""))]
        uselog.record(kind, items)
    except Exception:  # noqa: BLE001 - 기록 실패가 도구 응답을 막지 않게 한다
        return


def build_proxy(remote: Remote) -> Server:
    cache: list[types.Tool] = []

    async def list_tools(ctx, params) -> types.ListToolsResult:
        if not cache:
            cache.extend(t.model_copy(update={"description": LOCAL_DOCS[t.name]}) if t.name in LOCAL_DOCS else t
                         for t in await remote.tools())
        return types.ListToolsResult(tools=cache)

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            args = dict(params.arguments or {})
            result = await forward(remote, params.name, args)
            if params.name == "kb_search":
                result = set_apart(args, result, study_categories())
            log_use(params.name, args, result)
            return result
        except KbError as e:
            return text_result(e.message, is_error=True)
        except (OSError, ValueError) as e:
            return text_result(f"{type(e).__name__}: {e}", is_error=True)
        except Exception as e:  # noqa: BLE001 - 연결 실패 등은 도구 오류로 돌려 Claude 가 읽게 한다
            return text_result(f"Call to remote server {remote.endpoint} failed: {type(e).__name__}: {e}", is_error=True)

    return Server("fast-question", instructions=PROXY_INSTRUCTIONS, on_list_tools=list_tools, on_call_tool=call_tool)


async def run_stdio(remote: Remote) -> None:
    from mcp.server.stdio import stdio_server
    server = build_proxy(remote)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
