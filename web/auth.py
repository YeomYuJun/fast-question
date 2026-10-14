"""원격 모드 브라우저 로그인. 앱이 직접 GitHub(OAuth 2.0 웹 흐름)·Google(OpenID Connect)의 클라이언트가 된다.

- 두 공급자 모두 인가 코드 + PKCE(S256) + state 를 쓴다. state 는 제어 DB 에 10분 두고, 같은 값을 쿠키에도 넣어
  로그인을 시작한 브라우저에서만 끝낼 수 있게 한다.
- Google: 디스커버리 문서의 endpoint·jwks_uri 를 쓰고, id_token 의 서명·iss·aud·exp 를 검증해 sub 를 신원 키로 쓴다.
  scope 는 openid 만(이메일·이름을 받지 않는다).
- GitHub: access token 으로 /user 를 불러 숫자 id 를 신원 키, login 을 표시 이름으로 쓴다. 토큰은 저장하지 않는다.
- 세션 쿠키: Secure(https 일 때, 이름 앞에 __Host-) · HttpOnly · SameSite=Lax · Path=/. 값은 kb/control.py 세션.
- AccountAuth 는 살아 있는 세션 쿠키 또는 `Authorization: Bearer fq_pat_…`(개인 토큰)의 계정 id 를 scope 의
  ACCOUNT_KEY 에, 어느 쪽인지("session"/"token")를 AUTH_KIND_KEY 에 넣는다. 이 값을 넣는 곳은 이 미들웨어뿐이다.
- 개인 토큰 관리(/api/account/tokens)와 탈퇴(/api/account/delete)는 브라우저 세션으로만 한다.
- client secret 은 환경 변수 FQ_GITHUB_SECRET, FQ_GOOGLE_SECRET. client_id 와 secret 이 둘 다 있는 공급자만 켠다.
로그인 실패는 /?login_error=<denied|state|provider|closed|limit> 로 돌려보내고 화면이 문구를 보인다.
새 계정은 IP 당 한 시간에 5개까지 만든다(kb/control.py SIGNUP_PER_HOUR).
"""
import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Callable
from urllib.parse import urlencode

import httpx
import jwt
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from kb import control, tenant
from kb.config import Config

ACCOUNT_KEY = "fq.account_id"
AUTH_KIND_KEY = "fq.auth"
GOOGLE_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_USER = "https://api.github.com/user"
CACHE_S = 24 * 3600
log = logging.getLogger("fast-question.auth")

ClientFactory = Callable[[], httpx.AsyncClient]


class AuthError(Exception):
    """공급자 응답이 올바르지 않다. 메시지는 로그에만 남긴다."""


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def safe_next(value: str | None) -> str:
    """로그인 뒤 돌아갈 곳. 이 사이트 안의 경로만 받는다(//host, /\\host 같은 다른 사이트 주소는 / 로)."""
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return "/"
    return value


class Google:
    name = "google"

    def __init__(self, client_id: str, secret: str, redirect_uri: str, client: ClientFactory):
        self.client_id, self.secret, self.redirect_uri, self.client = client_id, secret, redirect_uri, client
        self._meta: tuple[float, dict] | None = None
        self._keys: dict[str, dict] = {}

    async def meta(self) -> dict:
        if self._meta is None or time.monotonic() - self._meta[0] > CACHE_S:
            async with self.client() as c:
                r = await c.get(GOOGLE_DISCOVERY)
                r.raise_for_status()
                self._meta = (time.monotonic(), r.json())
        return self._meta[1]

    async def authorize_url(self, state: str, challenge: str) -> str:
        endpoint = (await self.meta())["authorization_endpoint"]
        return endpoint + "?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri, "response_type": "code",
            "scope": "openid", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})

    async def _key(self, kid: str):
        if kid not in self._keys:  # 키가 바뀌었으면 한 번 다시 받는다
            async with self.client() as c:
                r = await c.get((await self.meta())["jwks_uri"])
                r.raise_for_status()
                self._keys = {k["kid"]: k for k in r.json().get("keys", []) if "kid" in k}
        if kid not in self._keys:
            raise AuthError(f"id_token 서명 키를 찾지 못했다: {kid}")
        return jwt.PyJWK(self._keys[kid]).key

    async def identity(self, code: str, verifier: str) -> tuple[str, str]:
        async with self.client() as c:
            r = await c.post((await self.meta())["token_endpoint"], data={
                "code": code, "client_id": self.client_id, "client_secret": self.secret,
                "redirect_uri": self.redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier})
        if r.status_code != 200 or "id_token" not in r.json():
            raise AuthError(f"Google 토큰 교환 실패: {r.status_code}")
        id_token = r.json()["id_token"]
        try:
            kid = jwt.get_unverified_header(id_token).get("kid", "")
            claims = jwt.decode(id_token, await self._key(kid), algorithms=["RS256"], audience=self.client_id,
                                issuer=GOOGLE_ISSUERS, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.PyJWTError as e:
            raise AuthError(f"id_token 검증 실패: {type(e).__name__}: {e}") from e
        return str(claims["sub"]), ""


class GitHub:
    name = "github"

    def __init__(self, client_id: str, secret: str, redirect_uri: str, client: ClientFactory):
        self.client_id, self.secret, self.redirect_uri, self.client = client_id, secret, redirect_uri, client

    async def authorize_url(self, state: str, challenge: str) -> str:
        return GITHUB_AUTHORIZE + "?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri, "scope": "read:user",
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "allow_signup": "true"})

    async def identity(self, code: str, verifier: str) -> tuple[str, str]:
        async with self.client() as c:
            r = await c.post(GITHUB_TOKEN, headers={"Accept": "application/json"}, data={
                "client_id": self.client_id, "client_secret": self.secret, "code": code,
                "redirect_uri": self.redirect_uri, "code_verifier": verifier})
            token = r.json().get("access_token") if r.status_code == 200 else None
            if not token:
                raise AuthError(f"GitHub 토큰 교환 실패: {r.status_code} {r.json().get('error', '')}")
            u = await c.get(GITHUB_USER, headers={"Authorization": f"Bearer {token}",
                                                  "Accept": "application/vnd.github+json",
                                                  "User-Agent": "fast-question"})
        if u.status_code != 200 or not isinstance(u.json().get("id"), int):
            raise AuthError(f"GitHub 사용자 조회 실패: {u.status_code}")
        return str(u.json()["id"]), str(u.json().get("login") or "")


def providers_from(cfg: Config, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=10.0)

    found = {}
    for cls, cid, env in ((GitHub, cfg.github_client_id, "FQ_GITHUB_SECRET"),
                          (Google, cfg.google_client_id, "FQ_GOOGLE_SECRET")):
        secret = os.environ.get(env, "")
        if cid and secret:
            found[cls.name] = cls(cid, secret, f"{cfg.public_url}/auth/callback/{cls.name}", client)
    return found


def cookie_names(cfg: Config) -> tuple[str, str, bool]:
    """(세션 쿠키, 로그인 state 쿠키, Secure). https 면 __Host- 접두어로 다른 하위 도메인이 덮어쓰지 못하게 한다."""
    secure = cfg.public_url.startswith("https://")
    prefix = "__Host-" if secure else ""
    return f"{prefix}fq_session", f"{prefix}fq_login", secure


def bearer(headers) -> str:
    kind, _, value = headers.get("authorization", "").partition(" ")
    return value.strip() if kind.lower() == "bearer" else ""


class AccountAuth:
    def __init__(self, app, control_path, cookie: str):
        self.app, self.control_path, self.cookie = app, control_path, cookie

    def _lookup(self, kind: str, token: str) -> int | None:
        conn = control.connect_control(self.control_path)
        try:
            if kind == "token":
                found = control.token_account(conn, token)
                return None if found is None else found["account_id"]
            return control.session_account(conn, token)
        finally:
            conn.close()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            request = Request(scope)
            pat = bearer(request.headers)
            kind, token = ("token", pat) if pat.startswith(control.TOKEN_PREFIX) else (
                "session", request.cookies.get(self.cookie, ""))
            if token:
                account_id = await run_in_threadpool(self._lookup, kind, token)
                if account_id is not None:
                    scope = dict(scope, **{ACCOUNT_KEY: account_id, AUTH_KIND_KEY: kind})
        await self.app(scope, receive, send)


def routes(cfg: Config, providers: dict, control_path) -> list[Route]:
    session_cookie, login_cookie, secure = cookie_names(cfg)

    def db(fn, *args):
        conn = control.connect_control(control_path)
        try:
            return fn(conn, *args)
        finally:
            conn.close()

    def failed(code: str) -> Response:
        resp = RedirectResponse(f"/?login_error={code}", status_code=302)
        resp.delete_cookie(login_cookie, path="/", secure=secure, httponly=True, samesite="lax")
        return resp

    async def login(request: Request) -> Response:
        p = providers.get(request.path_params["provider"])
        if p is None:
            return JSONResponse({"error": "켜지지 않은 로그인 공급자다.", "code": "not_found"}, status_code=404)
        verifier, challenge = pkce()
        state = await run_in_threadpool(db, control.put_login_state, p.name, verifier,
                                        safe_next(request.query_params.get("next")))
        try:
            url = await p.authorize_url(state, challenge)
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.warning("%s 로그인 시작 실패: %s", p.name, e)
            return failed("provider")
        resp = RedirectResponse(url, status_code=302)
        resp.set_cookie(login_cookie, state, max_age=int(control.LOGIN_STATE_TTL.total_seconds()), path="/",
                        secure=secure, httponly=True, samesite="lax")
        return resp

    async def callback(request: Request) -> Response:
        p = providers.get(request.path_params["provider"])
        if p is None:
            return JSONResponse({"error": "켜지지 않은 로그인 공급자다.", "code": "not_found"}, status_code=404)
        q = request.query_params
        if q.get("error"):
            return failed("denied")
        state = q.get("state", "")
        if not state or not hmac.compare_digest(state, request.cookies.get(login_cookie, "")):
            return failed("state")
        st = await run_in_threadpool(db, control.take_login_state, state, p.name)
        if st is None:
            return failed("state")
        try:
            sub, name = await p.identity(q.get("code", ""), st["verifier"])
        except (AuthError, httpx.HTTPError, KeyError, ValueError) as e:
            log.warning("%s 로그인 실패: %s", p.name, e)
            return failed("provider")
        try:
            account_id, created = await run_in_threadpool(db, control.login_identity, p.name, sub, name,
                                                          cfg.allowed_logins, request.client.host if request.client else "?")
        except control.SignupLimited:
            log.warning("%s 가입 제한: 같은 IP 에서 한 시간에 %s개", p.name, control.SIGNUP_PER_HOUR)
            return failed("limit")
        except control.LoginRefused:
            log.warning("%s 로그인 거부: 허용 목록에 없거나 탈퇴한 계정", p.name)
            return failed("closed")
        if created:
            log.info("새 계정 %s (%s)", account_id, p.name)
        token = await run_in_threadpool(db, control.create_session, account_id,
                                        request.headers.get("user-agent", ""))
        resp = RedirectResponse(st["next"], status_code=302)
        resp.delete_cookie(login_cookie, path="/", secure=secure, httponly=True, samesite="lax")
        resp.set_cookie(session_cookie, token, max_age=int(control.SESSION_MAX.total_seconds()), path="/",
                        secure=secure, httponly=True, samesite="lax")
        return resp

    async def logout(request: Request) -> Response:
        token = request.cookies.get(session_cookie)
        if token:
            await run_in_threadpool(db, control.delete_session, token)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(session_cookie, path="/", secure=secure, httponly=True, samesite="lax")
        return resp

    async def me(request: Request) -> Response:
        account_id = request.scope.get(ACCOUNT_KEY)
        if account_id is None:
            return JSONResponse({"error": "로그인이 필요하다.", "code": "unauthorized", "mode": "remote",
                                 "providers": sorted(providers)}, status_code=401)

        def read(conn):
            return {**control.get_account(conn, account_id), "providers": control.account_providers(conn, account_id)}

        acc = await run_in_threadpool(db, read)
        return JSONResponse({"mode": "remote", "account": {
            "id": acc["id"], "display_name": acc["display_name"], "providers": acc["providers"]}})

    def need_session(request: Request) -> Response | None:
        if request.scope.get(AUTH_KIND_KEY) != "session":
            return JSONResponse({"error": "브라우저에서 로그인해야 한다.", "code": "unauthorized"}, status_code=401)
        return None

    async def tokens(request: Request) -> Response:
        if (denied := need_session(request)) is not None:
            return denied
        account_id = request.scope[ACCOUNT_KEY]
        if request.method == "GET":
            return JSONResponse({"tokens": await run_in_threadpool(db, control.list_personal_tokens, account_id)})
        try:
            name = (await request.json()).get("name", "")
        except ValueError:
            return JSONResponse({"error": "요청 본문이 JSON 이 아니다."}, status_code=400)
        try:
            made = await run_in_threadpool(db, control.create_personal_token, account_id, str(name))
        except control.TokenRefused as e:
            return JSONResponse({"error": str(e), "code": "invalid"}, status_code=400)
        log.info("개인 토큰 발급: 계정 %s, 토큰 %s", account_id, made["id"])
        return JSONResponse(made)

    async def revoke(request: Request) -> Response:
        if (denied := need_session(request)) is not None:
            return denied
        if not await run_in_threadpool(db, control.revoke_personal_token, request.scope[ACCOUNT_KEY],
                                       request.path_params["id"]):
            return JSONResponse({"error": "토큰을 찾지 못했다.", "code": "not_found"}, status_code=404)
        return JSONResponse({"ok": True})

    async def delete_account(request: Request) -> Response:
        """탈퇴. 확인 문구("탈퇴")를 받아야 한다. 사용자 폴더는 data_dir/deleted/ 로 옮긴다(30일 뒤 지우는 것은 주기 작업)."""
        if (denied := need_session(request)) is not None:
            return denied
        try:
            confirm = (await request.json()).get("confirm")
        except ValueError:
            confirm = None
        if confirm != "탈퇴":
            return JSONResponse({"error": "확인 문구로 '탈퇴' 를 입력한다.", "code": "invalid"}, status_code=400)
        account_id = request.scope[ACCOUNT_KEY]

        def work() -> None:
            db(control.delete_account, account_id)
            if tenant.user_dir(cfg.data_dir, account_id).exists():
                tenant.retire(cfg.data_dir, account_id)

        await run_in_threadpool(work)
        log.info("탈퇴: 계정 %s", account_id)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(session_cookie, path="/", secure=secure, httponly=True, samesite="lax")
        return resp

    return [
        Route("/api/account/tokens", tokens, methods=["GET", "POST"]),
        Route("/api/account/delete", delete_account, methods=["POST"]),
        Route("/api/account/tokens/{id:int}", revoke, methods=["DELETE"]),
        Route("/auth/login/{provider}", login),
        Route("/auth/callback/{provider}", callback),
        Route("/auth/logout", logout, methods=["POST"]),
        Route("/api/me", me),
    ]
