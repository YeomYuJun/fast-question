"""웹 화면 Claude 패널(원격 모드). 사용자가 등록한 Anthropic API 키로 서버가 대화 반복을 돌리고, 진행을 SSE 로 보낸다.

- 키는 제어 DB 의 claude_key 에 Fernet 으로 암호화해 둔다. 암호 키는 data_dir/claude-key.secret 파일이고 처음 쓸 때 만든다.
  이 파일을 잃거나 바꾸면 저장된 키를 풀 수 없어 사용자가 키를 다시 등록해야 한다(S3 의 DB 백업만으로는 키를 풀 수 없다).
- 도구는 MCP 서버(mcp_server/server.py build_server)의 도구 중 ALLOWED_TOOLS 만 쓴다. 쓰기 주체는 claude, 원격 제한(Store remote)은
  MCP 와 같다. 파일 근거·재검증·수확·삭제·대량 등록은 뺀다.
- 대화 기록은 브라우저가 들고 매번 보낸다. 서버는 저장하지 않는다. done 이벤트의 append 는 이번 요청에서 늘어난 메시지(API 형식,
  생각·대체 모델 블록 포함)이고 브라우저는 그대로 이어 붙여 다음 요청에 보낸다. 오류로 끝나면 append 를 보내지 않는다.
- 브라우저 세션으로만 쓴다. 계정당 대화 하나만 동시에 돈다. 한 요청의 모델 호출은 MAX_TURNS 번까지다.
"""
import json
import logging
import os
from pathlib import Path
from typing import Callable

import anthropic
from cryptography.fernet import Fernet, InvalidToken
from mcp.server.mcpserver.exceptions import ToolError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from kb import control
from kb.store import Store
from mcp_server.instructions import INSTRUCTIONS
from mcp_server.server import build_server
from web.auth import ACCOUNT_KEY, AUTH_KIND_KEY

log = logging.getLogger("fast-question.claude")

MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
DEFAULT_MODEL = MODELS[0]
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}  # 거부(refusal) 때 서버가 다른 모델로 다시 돈다
FALLBACK_BETA = "server-side-fallback-2026-07-01"
ALLOWED_TOOLS = ("kb_search", "kb_get", "kb_categories", "kb_add", "kb_update", "kb_archive", "kb_restore", "kb_link",
                 "kb_add_category", "kb_rename_category", "kb_cleanup_list", "kb_cleanup_resolve")
MAX_TURNS = 20
MAX_TOKENS = 16000
MAX_MESSAGES = 200
TOOL_RESULT_MAX = 20_000  # 도구 결과를 모델에 넘기는 글자 수. 기록이 커져 요청 본문 상한(CHAT_BODY_MAX)을 넘지 않게 한다
CHAT_BODY_MAX = 8 * 1024 * 1024
SECRET_FILE = "claude-key.secret"

SYSTEM = f"""\
너는 fast-question 웹 화면 오른쪽 패널에서 이 사용자의 Q&A 지식 DB 를 다루는 비서다. 한국어로 짧고 정확하게 답한다.
- DB 에 관한 질문은 먼저 kb_search·kb_get 으로 찾아보고 답한다. 찾은 항목은 결과의 url 로 [#<id> 질문](url) 처럼 링크한다.
- 사용자가 등록·수정·보관·분류 정리를 요청하면 도구로 바로 처리하고 무엇을 했는지 한두 줄로 알린다.
- 이 화면에서는 파일 근거를 달 수 없다(서버에 사용자 파일이 없다). 근거가 필요한 등록은 근거 없이 하거나 사용자 PC 의 Claude Code 를 권한다.
- 항목 삭제·분류 삭제·대량 등록은 이 화면에서 할 수 없다. 요청받으면 웹 화면의 편집 기능이나 Claude Code 를 안내한다.

{INSTRUCTIONS}"""


class KeyBox:
    """API 키 암호화. 암호 키 파일은 처음 쓸 때 만든다(소유자만 읽기)."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / SECRET_FILE
        self._fernet: Fernet | None = None

    def _get(self) -> Fernet:
        if self._fernet is None:
            if not self.path.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:  # 다른 요청이 먼저 만들었다
                    pass
                else:
                    with os.fdopen(fd, "wb") as f:
                        f.write(Fernet.generate_key())
            self._fernet = Fernet(self.path.read_bytes().strip())
        return self._fernet

    def seal(self, key: str) -> str:
        return self._get().encrypt(key.encode("utf-8")).decode("ascii")

    def open(self, secret: str) -> str | None:
        """풀 수 없으면(암호 키 파일이 바뀌었다) None."""
        try:
            return self._get().decrypt(secret.encode("ascii")).decode("utf-8")
        except InvalidToken:
            return None


def tool_defs(tools) -> list[dict]:
    """MCP 도구 목록을 Messages API 도구 정의로. 입력은 받은 뒤 MCP 쪽 검사(pydantic)가 다시 확인한다."""
    out = []
    for t in tools:
        if t.name in ALLOWED_TOOLS:
            out.append({"name": t.name, "description": t.description or "", "input_schema": t.input_schema,
                        "eager_input_streaming": True})
    return out


def _sse(event: dict) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")


def _block(b) -> dict:
    """응답 블록을 다음 요청에 그대로 돌려보낼 수 있는 dict 로(생각 서명·대체 모델 블록 포함)."""
    return b.model_dump(mode="json", by_alias=True, exclude_none=True)


def _api_error(e: Exception) -> str:
    if isinstance(e, anthropic.AuthenticationError):
        return "API 키가 거부됐습니다. 키를 다시 등록해 주세요."
    if isinstance(e, anthropic.PermissionDeniedError):
        return "이 API 키로는 고른 모델을 쓸 수 없습니다."
    if isinstance(e, anthropic.RateLimitError):
        return "Anthropic 요청 한도에 걸렸습니다. 잠시 뒤 다시 보내 주세요."
    if isinstance(e, anthropic.BadRequestError):
        return f"Anthropic 이 요청을 거부했습니다: {e.message}"
    if isinstance(e, anthropic.APIStatusError):
        return f"Anthropic 서버 오류({e.status_code})입니다. 잠시 뒤 다시 보내 주세요."
    if isinstance(e, anthropic.APIConnectionError):
        return "Anthropic 에 연결하지 못했습니다."
    return "대화를 처리하지 못했습니다."


def _check_messages(messages) -> str | None:
    """요청 본문의 대화 기록 모양만 본다(내용은 Anthropic 이 검사한다). 문제가 있으면 이유."""
    if not isinstance(messages, list) or not messages:
        return "messages 가 비었습니다."
    if len(messages) > MAX_MESSAGES:
        return "대화가 너무 깁니다. 새 대화를 시작해 주세요."
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") or "content" not in m:
            return "messages 형식이 올바르지 않습니다."
    if messages[-1]["role"] != "user":
        return "마지막 메시지는 사용자 메시지여야 합니다."
    return None


def routes(control_path: Path, data_dir: Path, open_store: Callable[[int], Store], web_url: str,
           client_factory: Callable[[str], anthropic.AsyncAnthropic] = lambda key: anthropic.AsyncAnthropic(api_key=key)
           ) -> list[Route]:
    """open_store(account_id): 그 계정의 KB 를 쓰기 주체 claude, remote=True 로 연 Store(도구 호출마다 새로 연다)."""
    box = KeyBox(data_dir)
    busy: set[int] = set()

    def db(fn, *args):
        conn = control.connect_control(control_path)
        try:
            return fn(conn, *args)
        finally:
            conn.close()

    def account(request: Request) -> int | None:
        return request.scope.get(ACCOUNT_KEY) if request.scope.get(AUTH_KIND_KEY) == "session" else None

    def unauthorized() -> Response:
        return JSONResponse({"error": "브라우저에서 로그인해야 합니다.", "code": "unauthorized"}, status_code=401)

    async def body_of(request: Request) -> dict | None:
        try:
            body = await request.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    async def key(request: Request) -> Response:
        if (account_id := account(request)) is None:
            return unauthorized()
        if request.method == "GET":
            row = await run_in_threadpool(db, control.get_claude_key, account_id)
            usable = row is not None and await run_in_threadpool(box.open, row["secret"]) is not None
            return JSONResponse({"set": usable, "last4": row["last4"] if usable else None,
                                 "model": row["model"] if row else DEFAULT_MODEL, "models": list(MODELS),
                                 "stale": row is not None and not usable})
        if request.method == "DELETE":
            await run_in_threadpool(db, control.delete_claude_key, account_id)
            log.info("Claude API 키 삭제: 계정 %s", account_id)
            return JSONResponse({"ok": True})
        body = await body_of(request)
        if body is None:
            return JSONResponse({"error": "요청 본문이 JSON 이 아닙니다.", "code": "invalid"}, status_code=400)
        model = body.get("model", DEFAULT_MODEL)
        if model not in MODELS:
            return JSONResponse({"error": f"모델은 {', '.join(MODELS)} 중 하나입니다.", "code": "invalid"},
                                status_code=400)
        api_key = str(body.get("key") or "").strip()
        if not api_key:  # 키 없이 오면 모델만 바꾼다
            if not await run_in_threadpool(db, control.set_claude_model, account_id, model):
                return JSONResponse({"error": "먼저 API 키를 등록해 주세요.", "code": "no_key"}, status_code=400)
            return JSONResponse({"ok": True, "model": model})
        if len(api_key) > 300 or not api_key.isascii():
            return JSONResponse({"error": "API 키 형식이 아닙니다.", "code": "invalid"}, status_code=400)
        try:
            await client_factory(api_key).models.retrieve(model)  # 가벼운 요청으로 키와 모델 권한을 확인한다
        except Exception as e:  # noqa: BLE001 - 어떤 실패든 등록하지 않고 이유를 돌려준다
            log.info("Claude API 키 확인 실패: 계정 %s, %s", account_id, type(e).__name__)
            return JSONResponse({"error": _api_error(e), "code": "rejected"}, status_code=400)
        await run_in_threadpool(db, control.put_claude_key, account_id, await run_in_threadpool(box.seal, api_key),
                                api_key[-4:], model)
        log.info("Claude API 키 등록: 계정 %s", account_id)
        return JSONResponse({"ok": True, "last4": api_key[-4:], "model": model})

    async def run_chat(account_id: int, api_key: str, model: str, messages: list):
        client = client_factory(api_key)
        server = build_server(lambda: open_store(account_id), web_url=web_url, remote=True)
        tools = tool_defs(await server.list_tools())
        extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"} if model in FALLBACK_MODELS else {}
        appended: list[dict] = []
        try:
            for _ in range(MAX_TURNS):
                async with client.beta.messages.stream(
                        model=model, max_tokens=MAX_TOKENS, system=SYSTEM, tools=tools, messages=messages + appended,
                        thinking={"type": "adaptive", "display": "summarized"}, output_config={"effort": "medium"},
                        cache_control={"type": "ephemeral"}, **extra) as stream:
                    async for event in stream:
                        if event.type == "content_block_start" and event.content_block.type == "tool_use":
                            yield _sse({"type": "tool_start", "id": event.content_block.id,
                                        "name": event.content_block.name})
                        elif event.type == "content_block_delta":
                            if event.delta.type == "text_delta":
                                yield _sse({"type": "text", "text": event.delta.text})
                            elif event.delta.type == "thinking_delta":
                                yield _sse({"type": "thinking", "text": event.delta.thinking})
                    final = await stream.get_final_message()
                appended.append({"role": "assistant", "content": [_block(b) for b in final.content]})
                tool_uses = [b for b in final.content if b.type == "tool_use"]
                if final.stop_reason == "refusal":
                    yield _sse({"type": "notice", "text": "Claude 가 이 요청을 거절했습니다."})
                    break
                if final.stop_reason == "pause_turn":
                    continue
                if not tool_uses:
                    break
                if final.stop_reason == "max_tokens":  # 잘린 도구 입력은 실행하지 않는다
                    appended.pop()
                    yield _sse({"type": "error", "text": "응답이 길어 도중에 끊겼습니다. 요청을 나눠 보내 주세요."})
                    return
                results = []
                for b in tool_uses:
                    text, is_error = await _call(server, b.name, b.input)
                    yield _sse({"type": "tool_result", "id": b.id, "name": b.name, "input": b.input,
                                "is_error": is_error, "text": text[:2000]})
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": text[:TOOL_RESULT_MAX],
                                    **({"is_error": True} if is_error else {})})
                appended.append({"role": "user", "content": results})
            else:
                yield _sse({"type": "notice", "text": f"도구를 {MAX_TURNS}번 넘게 불러 여기서 멈췄습니다."})
        except ValueError:  # 모델이 만든 도구 입력 JSON 을 SDK 가 읽지 못했다
            yield _sse({"type": "error", "text": "Claude 응답을 읽지 못했습니다. 다시 보내 주세요."})
            return
        except Exception as e:  # noqa: BLE001 - 어떤 실패든 브라우저에 이유를 보내고 기록을 늘리지 않는다
            if not isinstance(e, anthropic.APIError):
                log.exception("Claude 대화 실패: 계정 %s", account_id)
            yield _sse({"type": "error", "text": _api_error(e)})
            return
        yield _sse({"type": "done", "append": appended})

    async def chat(request: Request) -> Response:
        if (account_id := account(request)) is None:
            return unauthorized()
        body = await body_of(request)
        if body is None:
            return JSONResponse({"error": "요청 본문이 JSON 이 아닙니다.", "code": "invalid"}, status_code=400)
        messages = body.get("messages")
        if (problem := _check_messages(messages)) is not None:
            return JSONResponse({"error": problem, "code": "invalid"}, status_code=400)
        row = await run_in_threadpool(db, control.get_claude_key, account_id)
        api_key = None if row is None else await run_in_threadpool(box.open, row["secret"])
        if api_key is None:
            return JSONResponse({"error": "먼저 Anthropic API 키를 등록해 주세요.", "code": "no_key"}, status_code=400)
        if account_id in busy:
            return JSONResponse({"error": "이전 요청을 처리하는 중입니다.", "code": "busy"}, status_code=409)
        busy.add(account_id)

        async def events():
            try:
                async for chunk in run_chat(account_id, api_key, row["model"], messages):
                    yield chunk
            finally:  # 브라우저가 끊으면 여기서 반복이 멈춘다(제너레이터가 취소된다)
                busy.discard(account_id)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return [Route("/api/claude/key", key, methods=["GET", "PUT", "DELETE"]),
            Route("/api/claude/chat", chat, methods=["POST"])]


async def _call(server, name: str, args) -> tuple[str, bool]:
    """MCP 도구를 부른다. (모델에 넘길 글, 오류 여부)."""
    if name not in ALLOWED_TOOLS:
        return f"{name} 은 이 화면에서 쓸 수 없는 도구입니다.", True
    if not isinstance(args, dict):
        return "도구 입력이 객체가 아닙니다.", True
    try:
        result = await server.call_tool(name, args)
    except ToolError as e:
        return str(e).removeprefix(f"Error executing tool {name}: "), True
    except Exception as e:  # noqa: BLE001 - 입력 검사 실패 등도 도구 오류로 돌려준다
        return f"{type(e).__name__}: {e}", True
    text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
    return text, bool(result.is_error)
