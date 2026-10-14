"""원격 모드 요청 제한. RateLimit 은 프로세스 메모리의 토큰 버킷이다(인스턴스 1대 전제). 넘치면 429 와 Retry-After.
BodyLimit 은 요청 본문을 1 MB(DB 가져오기 /api/import/db 는 200 MB)로 막고 넘치면 413 이다.

- /mcp: 계정(또는 Bearer 토큰)당 분당 120
- /register(OAuth 동적 클라이언트 등록): IP 당 분당 5
- /api/* 읽기(GET): 계정당 분당 300, 쓰기: 계정당 분당 60. 로그인하지 않은 요청은 IP 당 분당 60
- 그 밖(화면, /static, /auth/*)은 제한하지 않는다
계정은 AccountAuth 가 scope 에 넣은 값을 쓰므로 이 미들웨어는 AccountAuth 안쪽에 둔다. IP 는 scope 의 client 다
(역방향 프록시 뒤에서는 uvicorn 의 proxy headers 설정이 client 를 실제 주소로 바꿔야 한다).
"""
import hashlib
import math
import threading
import time
from typing import Callable

from starlette.responses import JSONResponse

from web.auth import ACCOUNT_KEY, bearer

DEFAULT_LIMITS = {"mcp": 120, "register": 5, "read": 300, "write": 60, "anon": 60}  # 분당
PRUNE_EVERY = 1000
IDLE_S = 600


class RateLimit:
    def __init__(self, app, limits: dict | None = None, clock: Callable[[], float] = time.monotonic):
        self.app, self.clock = app, clock
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.buckets: dict[tuple, tuple[float, float]] = {}  # 키 -> (남은 토큰, 마지막 시각)
        self.lock = threading.Lock()
        self.calls = 0

    def _rule(self, scope) -> tuple[str, str] | None:
        path, method = scope["path"], scope["method"]
        ip = (scope.get("client") or ("?",))[0]
        account = scope.get(ACCOUNT_KEY)
        if path == "/mcp" or path.startswith("/mcp/"):
            if account is not None:
                return "mcp", f"a{account}"
            token = bearer({k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]})
            return "mcp", ("t" + hashlib.sha256(token.encode()).hexdigest()[:16]) if token else f"ip{ip}"
        if path == "/register":
            return "register", f"ip{ip}"
        if path.startswith("/api/"):
            if account is None:
                return "anon", f"ip{ip}"
            return ("read" if method in ("GET", "HEAD") else "write"), f"a{account}"
        return None

    def _take(self, kind: str, who: str) -> float:
        """토큰 하나를 쓴다. 남은 것이 없으면 다음 토큰까지 기다릴 초를 돌려준다(0 이면 통과)."""
        per_min = self.limits[kind]
        rate = per_min / 60.0
        now = self.clock()
        with self.lock:
            self.calls += 1
            if self.calls % PRUNE_EVERY == 0:
                self.buckets = {k: v for k, v in self.buckets.items() if now - v[1] < IDLE_S}
            tokens, last = self.buckets.get((kind, who), (float(per_min), now))
            tokens = min(float(per_min), tokens + (now - last) * rate)
            if tokens < 1.0:
                self.buckets[(kind, who)] = (tokens, now)
                return (1.0 - tokens) / rate
            self.buckets[(kind, who)] = (tokens - 1.0, now)
            return 0.0

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            rule = self._rule(scope)
            if rule is not None:
                wait = self._take(*rule)
                if wait > 0:
                    retry = max(1, math.ceil(wait))
                    resp = JSONResponse({"error": f"요청이 너무 많다. {retry}초 뒤 다시 시도한다.", "code": "rate_limited"},
                                        status_code=429, headers={"Retry-After": str(retry)})
                    await resp(scope, receive, send)
                    return
        await self.app(scope, receive, send)


class BodyLimit:
    """요청 본문 크기 제한. 넘치면 413. 경로별로 더 큰 상한을 줄 수 있다(DB 가져오기 등)."""

    def __init__(self, app, max_bytes: int = 1024 * 1024, overrides: dict[str, int] | None = None):
        self.app, self.max_bytes = app, max_bytes
        self.overrides = {"/api/import/db": 200 * 1024 * 1024, **(overrides or {})}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.overrides.get(scope["path"], self.max_bytes)
        too_big = JSONResponse({"error": f"요청 본문이 {limit // (1024 * 1024)} MB 를 넘는다.", "code": "too_large"},
                               status_code=413)
        for k, v in scope["headers"]:
            if k == b"content-length" and v.isdigit() and int(v) > limit:
                await too_big(scope, receive, send)
                return
        seen = 0
        started = False

        async def counted_receive():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > limit:
                    raise _TooLarge
            return msg

        async def tracked_send(msg):
            nonlocal started
            started = True
            await send(msg)

        try:
            await self.app(scope, counted_receive, tracked_send)
        except _TooLarge:
            if not started:  # Content-Length 없이 보낸 본문(chunked)이 넘친 경우
                await too_big(scope, receive, send)


class _TooLarge(Exception):
    pass
