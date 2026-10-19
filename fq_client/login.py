"""`fast-question login`: 브라우저 승인으로 개인 토큰을 받아 credentials.json(kb/remote.py)에 쓴다. 서버 쪽은 web/cli_login.py.

1. 127.0.0.1 의 빈 포트에 임시 HTTP 서버를 띄우고 state 와 PKCE(S256) verifier 를 만든다.
2. 브라우저로 <url>/cli/authorize 를 연다. 사용자가 로그인해 승인하면 브라우저가 127.0.0.1:<포트>/callback 으로
   1회용 코드를 넘긴다. WAIT_S 안에 오지 않으면 끝낸다.
3. 코드와 verifier 로 <url>/api/cli/token 에서 개인 토큰을 받아 save_credentials 로 쓴다. device_id 는 그대로 둔다.
   같은 서버에 쓰던 토큰이 있으면 replaces 로 보내 서버가 폐기하게 한다.
서버 주소: --url, 없으면 credentials.json 의 주소, 그것도 없으면 DEFAULT_URL. 표준 라이브러리만 쓴다.
"""
import base64
import hashlib
import json
import platform
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from kb.remote import credentials_path, load_credentials, save_credentials

DEFAULT_URL = "https://fast-question.com"
WAIT_S = 300
DEVICE_MAX = 40  # 서버(web/cli_login.py)도 이 길이로 자른다


class LoginError(Exception):
    """로그인을 끝내지 못했다. 메시지는 사용자에게 그대로 보인다."""


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


DONE_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>fast-question</title></head>
<body><p>{text}</p></body></html>"""


class CallbackServer(HTTPServer):
    """GET /callback?state=<state>&code=… 또는 error=… 를 한 번 받는다. state 가 다른 요청은 무시한다."""

    def __init__(self, state: str):
        self.state = state
        self.result: dict | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if u.path != "/callback" or q.get("state") != outer.state or outer.result is not None:
                    self.send_error(404)
                    return
                outer.result = q
                text = ("로그인을 마쳤습니다. 이 창을 닫고 터미널로 돌아가세요." if q.get("code")
                        else "연결을 거부했습니다. 이 창을 닫아도 됩니다.")
                body = DONE_PAGE.format(text=text).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        super().__init__(("127.0.0.1", 0), Handler)
        self.timeout = 0.5

    @property
    def port(self) -> int:
        return self.server_address[1]

    def wait(self, seconds: float) -> dict | None:
        deadline = time.monotonic() + seconds
        while self.result is None and time.monotonic() < deadline:
            self.handle_request()
        return self.result


def authorize_url(url: str, port: int, state: str, challenge: str, device: str) -> str:
    return f"{url}/cli/authorize?" + urlencode({"port": port, "state": state, "code_challenge": challenge,
                                                "device": device[:DEVICE_MAX]})


def exchange(url: str, code: str, verifier: str, replaces: str = "", opener=urlopen) -> dict:
    body = json.dumps({"code": code, "code_verifier": verifier, "replaces": replaces}).encode("utf-8")
    req = Request(f"{url}/api/cli/token", data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with opener(req, timeout=30) as r:  # noqa: S310 - 사용자가 정한 서버 주소만 연다
            return json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error")
        except ValueError:
            msg = None
        raise LoginError(msg or f"서버가 토큰 발급을 거부했습니다(HTTP {e.code}).") from None
    except URLError as e:
        raise LoginError(f"서버에 연결하지 못했습니다: {url} ({e.reason})") from None


def resolve_url(given: str | None, old: dict | None) -> str:
    url = (given or (old["url"] if old else "") or DEFAULT_URL).strip().rstrip("/")
    if not url.startswith(("https://", "http://")):
        raise LoginError(f"서버 주소는 https:// 로 시작해야 합니다: {url}")
    return url


def login(url: str | None = None, *, path: Path | None = None, open_browser: Callable[[str], object] = webbrowser.open,
          opener=urlopen, wait_s: float = WAIT_S, say: Callable[[str], None] = print) -> Path:
    """로그인해 자격 증명 파일을 쓰고 그 경로를 돌려준다. 실패하면 LoginError."""
    old = load_credentials(path)
    url = resolve_url(url, old)
    replaces = old["token"] if old and old["url"] == url else ""
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(24)
    server = CallbackServer(state)
    try:
        target = authorize_url(url, server.port, state, challenge, platform.node() or "PC")
        say(f"브라우저에서 이 PC 의 연결을 승인하세요. 브라우저가 열리지 않으면 이 주소를 여세요:\n{target}")
        open_browser(target)
        got = server.wait(wait_s)
    finally:
        server.server_close()
    if got is None:
        raise LoginError(f"{int(wait_s // 60)}분 안에 승인하지 않아 끝냈습니다. 다시 실행하세요.")
    if not got.get("code"):
        raise LoginError("브라우저에서 연결을 거부했습니다.")
    made = exchange(url, got["code"], verifier, replaces, opener)
    saved = save_credentials(url, made["token"], path=path)
    acc = made.get("account") or {}
    who = acc.get("display_name") or ", ".join(acc.get("providers", []))
    say(f"로그인했습니다{f': {who}' if who else ''}. 토큰 이름: {made.get('name', '')}, 끝나는 때: {made.get('expires_at', '')}")
    say(f"자격 증명: {saved}")
    return saved


def main(url: str | None = None) -> int:
    try:
        login(url)
    except LoginError as e:
        print(str(e))
        return 1
    except OSError as e:
        print(f"자격 증명 파일을 쓰지 못했습니다: {credentials_path()} ({e})")
        return 1
    return 0
