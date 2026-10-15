"""원격 MCP 의 OAuth 인가 서버. mcp SDK 가 /register /authorize /token /revoke 와 메타데이터 문서를 만들고, 표 저장과
판단은 KbAuthProvider 가 한다(표 접근은 kb/oauth_store.py). 사용자 확인은 이 모듈의 /oauth/continue/<id> 가 한다.

흐름: /authorize → authorize() 가 매개변수를 대기 요청으로 두고 /oauth/continue/<id> 로 보낸다 → 브라우저 세션이 없으면
로그인 버튼(/auth/login/<공급자>?next=/oauth/continue/<id>), 있으면 동의 화면 → 승인하면 인가 코드를 붙여
redirect_uri 로, 거부하면 error=access_denied 로 보낸다 → /token 이 코드(PKCE 는 SDK 가 검사)를 access·refresh 로 바꾼다.

- redirect_uri 는 http://localhost:<포트>/callback, http://127.0.0.1:<포트>/callback, https://… 만 등록한다.
- resource 는 <public_url>/mcp 여야 한다. /authorize 에서 다르면 invalid_target 으로 돌려보내고, /mcp 에서도
  토큰의 resource 가 다르면 받지 않는다. 빠지면 <public_url>/mcp 로 채우고 로그를 남긴다. 토큰을 내는 보호 자원이
  /mcp 하나뿐이라 채워도 토큰이 다른 곳에 쓰일 길이 없다. 보호 자원을 늘리면 빠진 경우도 거부로 돌린다.
- MCPServer 는 provider 와 token_verifier 를 같이 받지 않아, load_access_token 이 개인 토큰(fq_pat_…)도 확인한다.
  개인 토큰과 OAuth 토큰이 같은 /mcp 에 붙는다. AccessToken.subject 는 계정 id 문자열이다.
- 동의 화면은 클라이언트가 등록한 이름과 돌아갈 주소를 보인다. 누구나 클라이언트를 등록할 수 있어서다.
"""
import html
import logging
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
                                      RefreshToken, RegistrationError, TokenError, construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from kb import control, oauth_store
from web.auth import ACCOUNT_KEY, AUTH_KIND_KEY

SCOPE = "kb"
CLIENT_JSON_MAX = 8192  # 등록 한 건의 메타데이터 크기 상한(인증 없이 받는다)
REDIRECT_MAX = 10
LOOPBACK_HOSTS = ("localhost", "127.0.0.1")
PROVIDER_LABELS = {"github": "GitHub", "google": "Google"}
CONSENT = "Claude Code 가 당신의 KB 를 읽고 씁니다"

log = logging.getLogger("fast-question.oauth")


def allowed_redirect(uri: str) -> bool:
    try:
        u = urlsplit(uri)
        port = u.port
    except ValueError:
        return False
    if u.fragment or u.username or u.password:
        return False
    if u.scheme == "https":
        return bool(u.hostname)
    return (u.scheme == "http" and u.hostname in LOOPBACK_HOSTS and port is not None
            and u.path == "/callback" and not u.query)


class KbAuthProvider:
    """OAuthAuthorizationServerProvider 구현. 상태는 모두 제어 DB 에 둔다(lifespan 마다 서버를 새로 만든다)."""

    def __init__(self, control_path: Path, public_url: str):
        self.control_path = control_path
        self.public_url = public_url
        self.resource = f"{public_url}/mcp"

    def _db(self, fn, *args):
        conn = control.connect_control(self.control_path)
        try:
            return fn(conn, *args)
        finally:
            conn.close()

    async def _run(self, fn, *args):
        return await run_in_threadpool(self._db, fn, *args)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = await self._run(oauth_store.get_client, client_id)
        return None if raw is None else OAuthClientInformationFull.model_validate_json(raw)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not 1 <= len(uris) <= REDIRECT_MAX:
            raise RegistrationError("invalid_redirect_uri", f"redirect_uri 는 1~{REDIRECT_MAX}개다.")
        bad = [u for u in uris if not allowed_redirect(u)]
        if bad:
            raise RegistrationError("invalid_redirect_uri", "redirect_uri 는 http://localhost:<포트>/callback, "
                                    f"http://127.0.0.1:<포트>/callback, https://… 만 받는다: {bad[0]}")
        raw = client_info.model_dump_json(exclude_none=True)
        if len(raw) > CLIENT_JSON_MAX:
            raise RegistrationError("invalid_client_metadata", f"등록 정보가 {CLIENT_JSON_MAX}바이트를 넘는다.")
        await self._run(oauth_store.put_client, client_info.client_id, raw)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        resource = params.resource
        if resource is None:
            log.info("resource 없는 /authorize 를 %s 로 채움: client_id=%s client_name=%s",
                     self.resource, client.client_id, client.client_name)
            resource = self.resource
        if resource != self.resource:
            raise AuthorizeError("invalid_target", f"resource 는 {self.resource} 여야 한다.")
        pending_id = await self._run(oauth_store.put_pending, client.client_id, {
            "scopes": params.scopes or [SCOPE], "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": resource, "state": params.state})
        return f"{self.public_url}/oauth/continue/{pending_id}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        found = await self._run(oauth_store.load_code, authorization_code)
        if found is None or found["client_id"] != client.client_id:
            return None
        p = found["params"]
        return AuthorizationCode(code=authorization_code, scopes=p["scopes"], expires_at=found["expires_at"],
                                 client_id=found["client_id"], code_challenge=p["code_challenge"],
                                 redirect_uri=p["redirect_uri"],
                                 redirect_uri_provided_explicitly=p["redirect_uri_provided_explicitly"],
                                 resource=p["resource"], subject=str(found["account_id"]))

    @staticmethod
    def _oauth_token(issued: dict | None) -> OAuthToken:
        if issued is None:
            raise TokenError("invalid_grant", "이미 썼거나 끝난 코드·토큰이다.")
        return OAuthToken(access_token=issued["access_token"], token_type="Bearer", expires_in=issued["expires_in"],
                          scope=" ".join(issued["scopes"]), refresh_token=issued["refresh_token"])

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        return self._oauth_token(await self._run(oauth_store.exchange_code, authorization_code.code))

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        found = await self._run(oauth_store.load_token, refresh_token, "refresh")
        if found is None or found["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=found["client_id"], scopes=found["scopes"],
                            expires_at=found["expires_at"], subject=str(found["account_id"]))

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        return self._oauth_token(await self._run(oauth_store.rotate_refresh, refresh_token.token, scopes))

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token.startswith(control.TOKEN_PREFIX):
            pat = await self._run(control.token_account, token)
            if pat is None:
                return None
            return AccessToken(token=token, client_id="personal-token", scopes=[SCOPE], resource=self.resource,
                               subject=str(pat["account_id"]))
        found = await self._run(oauth_store.load_token, token, "access")
        if found is None or found["resource"] != self.resource:
            return None
        return AccessToken(token=token, client_id=found["client_id"], scopes=found["scopes"],
                           expires_at=found["expires_at"], resource=found["resource"],
                           subject=str(found["account_id"]))

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await self._run(oauth_store.revoke, token.token)


PAGE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - fast-question</title>
<link rel="stylesheet" href="/static/tokens.css">
<link rel="stylesheet" href="/static/oauth.css">
</head>
<body>
<main>
{body}
</main>
</body>
</html>
"""
# Referrer-Policy 는 same-origin 이다. no-referrer 면 Chrome 이 이 페이지의 승인 폼 POST 에 Origin: null 을 붙여
# 같은 출처 검사(SameOriginOnly)가 거부한다. same-origin 도 다른 사이트(콜백 주소)에는 리퍼러를 보내지 않는다.
# Content-Security-Policy 는 모든 응답에 web/app.py 의 SecurityHeaders 가 붙인다
PAGE_HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin"}


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    """body 는 이미 이스케이프한 HTML 이다."""
    return HTMLResponse(PAGE.format(title=html.escape(title), body=body), status_code=status, headers=PAGE_HEADERS)


def ended() -> HTMLResponse:
    return page("요청이 끝났다", "<h1>연결 요청이 끝났다</h1>"
                "<p>없거나 10분이 지났거나 이미 처리한 요청이다. Claude Code 에서 다시 연결한다.</p>", 400)


def login_page(providers, path: str, status: int = 200) -> HTMLResponse:
    query = urlencode({"next": path})
    links = "".join(f'<a class="btn" href="/auth/login/{html.escape(name)}?{html.escape(query)}">'
                    f"{html.escape(PROVIDER_LABELS.get(name, name))} 로 로그인</a>" for name in sorted(providers))
    body = ("<h1>로그인</h1><p>Claude Code 를 이 KB 에 연결하려면 먼저 로그인한다.</p>"
            + (f'<div class="actions">{links}</div>' if links else "<p>켜진 로그인 공급자가 없다.</p>"))
    return page("로그인", body, status)


def consent_page(client: OAuthClientInformationFull | None, params: dict) -> HTMLResponse:
    name = (client.client_name if client else None) or (client.client_id if client else "")
    body = (f"<h1>{html.escape(CONSENT)}</h1>"
            "<p>승인하면 이 프로그램이 당신의 계정으로 KB 를 검색·등록·수정한다.</p>"
            f"<dl><dt>요청한 프로그램</dt><dd>{html.escape(name)}</dd>"
            f"<dt>승인 뒤 돌아갈 주소</dt><dd>{html.escape(params['redirect_uri'])}</dd></dl>"
            '<form method="post" class="actions">'
            '<button class="btn primary" type="submit" name="decision" value="approve">승인</button>'
            '<button class="btn" type="submit" name="decision" value="deny">거부</button>'
            "</form>")
    return page("연결 승인", body)


def routes(providers, control_path) -> list[Route]:
    """providers: 켜진 로그인 공급자 이름들(web/auth.py providers_from 의 키)."""
    providers = sorted(providers)

    def db(fn, *args):
        conn = control.connect_control(control_path)
        try:
            return fn(conn, *args)
        finally:
            conn.close()

    async def continue_(request: Request) -> Response:
        pending_id = request.path_params["id"]
        path = f"/oauth/continue/{pending_id}"
        has_session = request.scope.get(AUTH_KIND_KEY) == "session"
        if request.method == "GET":
            found = await run_in_threadpool(db, oauth_store.get_pending, pending_id)
            if found is None:
                return ended()
            if not has_session:
                return login_page(providers, path)
            raw = await run_in_threadpool(db, oauth_store.get_client, found["client_id"])
            client = None if raw is None else OAuthClientInformationFull.model_validate_json(raw)
            return consent_page(client, found["params"])

        if not has_session:
            return login_page(providers, path, 401)
        decision = (await request.form()).get("decision")
        if decision not in ("approve", "deny"):
            return page("잘못된 요청", "<h1>잘못된 요청이다</h1><p>승인 또는 거부를 고른다.</p>", 400)
        found = await run_in_threadpool(db, oauth_store.take_pending, pending_id)
        if found is None:
            return ended()
        p = found["params"]
        if decision == "deny":
            target = construct_redirect_uri(p["redirect_uri"], error="access_denied", state=p["state"])
        else:
            code = await run_in_threadpool(db, oauth_store.create_code, found["client_id"],
                                           request.scope[ACCOUNT_KEY], p)
            target = construct_redirect_uri(p["redirect_uri"], code=code, state=p["state"])
        return RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})

    return [Route("/oauth/continue/{id}", continue_, methods=["GET", "POST"])]
