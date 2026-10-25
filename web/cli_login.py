"""PC 클라이언트 로그인(`fast-question login`, fq_client/login.py)의 브라우저 승인과 토큰 교환. 원격 모드에서만 켠다.

흐름: PC 가 127.0.0.1 의 임시 포트에서 기다리며 브라우저로 GET /cli/authorize?port=&state=&code_challenge=&device= 를
연다 → 브라우저 세션이 없으면 로그인 버튼(/auth/login/<공급자>?next=<이 주소>), 있으면 승인 화면 → 승인하면 1회용 코드를
붙여 http://127.0.0.1:<port>/callback?code=&state= 로, 거부하면 error=access_denied 로 보낸다 → PC 가
POST /api/cli/token {code, code_verifier, replaces} 로 개인 토큰을 받는다(코드는 5분 안에 한 번, PKCE S256).
- 코드는 승인한 사람의 PC(127.0.0.1)로만 간다. 남이 만든 승인 주소를 열어 승인해도 코드가 그 사람에게 가지 않는다.
- 승인 POST 는 같은 출처만 받는다(web/app.py SameOriginOnly). 화면은 프레임에 넣을 수 없다(X-Frame-Options, CSP).
- replaces 에 그 PC 가 쓰던 개인 토큰을 주면, 같은 계정의 살아 있는 토큰일 때 새 토큰을 만들기 전에 폐기한다.
  다시 로그인할 때마다 토큰이 쌓여 계정당 상한(kb/control.py TOKEN_MAX)에 걸리지 않게 한다.
- 토큰 이름은 "PC 로그인: <PC 이름>" 이다. 연결 패널의 토큰 목록에 이 이름으로 보인다.
- 화면 CSS 는 /cli/login.css 로 낸다(CSP 가 인라인 스타일을 막는다). 색·글꼴·간격은 tokens.css 변수만 쓴다.
"""
import base64
import hashlib
import hmac
import html
import logging
import re
from urllib.parse import urlencode

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from kb import control
from web.auth import ACCOUNT_KEY, AUTH_KIND_KEY

PORT_MIN, PORT_MAX = 1024, 65535
STATE_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
CHALLENGE_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # base64url(sha256), 패딩 없음
DEVICE_MAX = 40
TOKEN_NAME_PREFIX = "PC 로그인: "
PROVIDER_LABELS = {"github": "GitHub", "google": "Google"}

log = logging.getLogger("fast-question.cli_login")

CSS = """/* PC 로그인 승인 화면(web/cli_login.py). tokens.css 변수만 쓴다 */
body { margin: 0; background: var(--bg); color: var(--ink); font: var(--text-answer); }
main { max-width: 28rem; margin: var(--space-12) auto; padding: var(--space-6); background: var(--surface);
  border: 1px solid var(--line); border-radius: var(--radius-lg); box-shadow: var(--shadow-1); }
h1 { margin: 0 0 var(--space-4); font: var(--text-question-lg); }
p { margin: 0 0 var(--space-3); color: var(--muted); }
p.note { font: var(--text-meta); color: var(--faint); }
dl { margin: 0 0 var(--space-6); }
dt { font: var(--text-label); color: var(--faint); }
dd { margin: 0 0 var(--space-3); font: var(--text-evidence); word-break: break-all; }
.actions { display: flex; gap: var(--space-2); flex-wrap: wrap; }
.btn { display: inline-block; padding: var(--space-2) var(--space-4); border: 1px solid var(--line-strong);
  border-radius: var(--radius-md); background: var(--surface); color: var(--ink); font: var(--text-question);
  text-decoration: none; cursor: pointer; }
.btn:hover { border-color: var(--accent); }
.btn.primary { border-color: var(--accent); background: var(--accent); color: var(--accent-ink); }
"""

PAGE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - fast question</title>
<link rel="stylesheet" href="/static/tokens.css">
<link rel="stylesheet" href="/cli/login.css">
</head>
<body>
<main>
{body}
</main>
</body>
</html>
"""
# same-origin: no-referrer 면 Chrome 이 승인 폼 POST 에 Origin: null 을 붙여 SameOriginOnly 가 거부한다
PAGE_HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin"}


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    """body 는 이미 이스케이프한 HTML 이다."""
    return HTMLResponse(PAGE.format(title=html.escape(title), body=body), status_code=status, headers=PAGE_HEADERS)


def bad_request() -> HTMLResponse:
    return page("잘못된 요청", "<h1>잘못된 요청입니다</h1>"
                "<p>PC 에서 <code>fast-question login</code> 을 다시 실행하세요.</p>", 400)


def challenge_of(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii", "replace")).digest()).rstrip(b"=").decode()


def read_params(q) -> dict | None:
    """승인 주소의 매개변수. 형식이 틀리면 None. device 는 비었으면 "PC", 길면 자른다."""
    try:
        port = int(q.get("port", ""))
    except ValueError:
        return None
    state, challenge = q.get("state", ""), q.get("code_challenge", "")
    if not PORT_MIN <= port <= PORT_MAX or not STATE_RE.fullmatch(state) or not CHALLENGE_RE.fullmatch(challenge):
        return None
    device = " ".join(str(q.get("device", "")).split())[:DEVICE_MAX]
    if not device.isprintable():
        return None
    return {"port": str(port), "state": state, "code_challenge": challenge, "device": device or "PC"}


def callback_url(params: dict, **query: str) -> str:
    return f"http://127.0.0.1:{params['port']}/callback?" + urlencode({**query, "state": params["state"]})


def login_page(providers, params: dict, status: int = 200) -> HTMLResponse:
    query = urlencode({"next": "/cli/authorize?" + urlencode(params)})
    links = "".join(f'<a class="btn" href="/auth/login/{html.escape(name)}?{html.escape(query)}">'
                    f"{html.escape(PROVIDER_LABELS.get(name, name))} 로 로그인</a>" for name in sorted(providers))
    body = ("<h1>로그인</h1><p>이 PC 의 fast-question 명령을 계정에 연결하려면 먼저 로그인하세요.</p>"
            + (f'<div class="actions">{links}</div>' if links else "<p>켜진 로그인 공급자가 없습니다.</p>"))
    return page("로그인", body, status)


def consent_page(params: dict, account: dict) -> HTMLResponse:
    hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(v)}">' for k, v in params.items())
    who = account.get("display_name") or ", ".join(PROVIDER_LABELS.get(p, p) for p in account.get("providers", []))
    body = ("<h1>이 PC 를 연결할까요?</h1>"
            "<p>승인하면 이 PC 의 fast-question 명령(Claude Code 플러그인)이 개인 토큰으로 내 질문·답변 저장소를 검색·등록·수정합니다."
            "토큰은 180일 뒤 끝나고, 연결 패널의 토큰 목록에서 언제든 폐기할 수 있습니다.</p>"
            f"<dl><dt>PC 이름</dt><dd>{html.escape(params['device'])}</dd>"
            f"<dt>계정</dt><dd>{html.escape(who)}</dd></dl>"
            '<p class="note">PC 에서 직접 <code>fast-question login</code> 을 실행하지 않았다면 거부하세요.</p>'
            f'<form method="post" action="/cli/authorize" class="actions">{hidden}'
            '<button class="btn primary" type="submit" name="decision" value="approve">승인</button>'
            '<button class="btn" type="submit" name="decision" value="deny">거부</button>'
            "</form>")
    return page("PC 연결 승인", body)


def error(message: str, code: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message, "code": code}, status_code=status, headers={"Cache-Control": "no-store"})


def routes(providers, control_path) -> list[Route]:
    """providers: 켜진 로그인 공급자 이름들(web/auth.py providers_from 의 키)."""
    providers = sorted(providers)

    def db(fn, *args):
        conn = control.connect_control(control_path)
        try:
            return fn(conn, *args)
        finally:
            conn.close()

    def account_view(conn, account_id: int) -> dict:
        return {**control.get_account(conn, account_id), "providers": control.account_providers(conn, account_id)}

    async def authorize(request: Request) -> Response:
        q = request.query_params if request.method == "GET" else await request.form()
        params = read_params(q)
        if params is None:
            return bad_request()
        if request.scope.get(AUTH_KIND_KEY) != "session":
            return login_page(providers, params, 200 if request.method == "GET" else 401)
        account_id = request.scope[ACCOUNT_KEY]
        if request.method == "GET":
            return consent_page(params, await run_in_threadpool(db, account_view, account_id))
        decision = q.get("decision")
        if decision not in ("approve", "deny"):
            return bad_request()
        if decision == "deny":
            target = callback_url(params, error="access_denied")
        else:
            code = await run_in_threadpool(db, control.put_cli_grant, account_id, params["code_challenge"],
                                           params["device"])
            target = callback_url(params, code=code)
        return RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})

    async def token(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return error("요청 본문이 JSON 객체가 아닙니다.", "invalid")
        code, verifier, replaces = (str(body.get(k) or "") for k in ("code", "code_verifier", "replaces"))

        def work(conn):
            grant = control.take_cli_grant(conn, code)
            if grant is None or not hmac.compare_digest(challenge_of(verifier), grant["challenge"]):
                return None
            revoked = None
            old = control.token_account(conn, replaces) if replaces else None
            if old is not None and old["account_id"] == grant["account_id"]:
                control.revoke_personal_token(conn, grant["account_id"], old["token_id"])
                revoked = old["token_id"]
            made = control.create_personal_token(conn, grant["account_id"], TOKEN_NAME_PREFIX + grant["device"])
            return grant["account_id"], made, revoked, account_view(conn, grant["account_id"])

        try:
            got = await run_in_threadpool(db, work)
        except control.TokenRefused:
            return error(f"살아 있는 개인 토큰이 이미 {control.TOKEN_MAX}개입니다. 연결 패널에서 쓰지 않는 토큰을 "
                         "폐기한 뒤 다시 로그인하세요.", "token_limit")
        if got is None:
            return error("승인 코드가 없거나 5분이 지났거나 이미 썼습니다. fast-question login 을 다시 실행하세요.",
                         "invalid_grant")
        account_id, made, revoked, acc = got
        log.info("PC 로그인 토큰 발급: 계정 %s, 토큰 %s, 폐기한 옛 토큰 %s", account_id, made["id"], revoked)
        return JSONResponse({"token": made["token"], "name": made["name"], "expires_at": made["expires_at"],
                             "account": {"display_name": acc["display_name"], "providers": acc["providers"]}},
                            headers={"Cache-Control": "no-store"})

    return [
        Route("/cli/authorize", authorize, methods=["GET", "POST"]),
        Route("/cli/login.css", lambda r: Response(CSS, media_type="text/css", headers={"Cache-Control": "no-cache"})),
        Route("/api/cli/token", token, methods=["POST"]),
    ]
