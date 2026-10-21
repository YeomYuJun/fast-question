"""원격 MCP(Streamable HTTP, <public_url>/mcp). 도구는 mcp_server/server.py 의 18개 그대로이고 쓰기 주체는 claude 다.

Bearer 토큰의 계정이 DB 파일을 정한다(AccessToken.subject = 계정 id). 받는 토큰은 OAuth access token 과
개인 토큰(fq_pat_…)이고, 둘 다 web/oauth_as.py 의 KbAuthProvider.load_access_token 이 확인한다. 같은 앱에 SDK 의
OAuth 경로(/register /authorize /token /revoke, /.well-known/oauth-authorization-server)와
/.well-known/oauth-protected-resource/mcp 가 있다.
web/app.py 가 이 앱을 마지막 라우트로 붙인다. Mount 된 하위 앱의 lifespan 은 돌지 않으므로 세션 매니저는
web/app.py 의 lifespan 이 RemoteMcp.running() 으로 시작한다. 세션 매니저는 한 번만 시작할 수 있어 lifespan 마다
서버를 새로 만든다. 세션을 메모리에 두지 않는다(stateless_http).
"""
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings

from kb import tenant
from kb.config import Config
from kb.db import connect
from kb.store import Store
from mcp_server.server import build_server
from web.oauth_as import SCOPE, KbAuthProvider


class RemoteMcp:
    """ASGI 앱. 요청을 지금 lifespan 의 MCP 앱으로 넘긴다."""

    def __init__(self, cfg: Config, analyzer):
        self.cfg, self.analyzer = cfg, analyzer
        self.control_path = tenant.control_path(cfg.data_dir)
        self._app = None
        # 사용자 DB 를 여는 함수. web/app.py 가 새 계정 DB 에 기본 가이드를 넣는 open_kb 로 바꿔 끼운다
        self.opener = connect

    def open_store(self) -> Store:
        token = get_access_token()
        if token is None or not (token.subject or "").isdigit():
            raise ValueError("인증된 계정이 없다.")
        path = tenant.db_path_for(self.cfg.data_dir, int(token.subject))
        return Store(self.opener(path), "claude", analyzer=self.analyzer, remote=True)

    def build(self):
        url = self.cfg.public_url
        auth = AuthSettings(
            issuer_url=url, resource_server_url=f"{url}/mcp", required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE],
                                                                  default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True))
        server = build_server(self.open_store, web_url=self.cfg.web_url, remote=True, auth=auth,
                              auth_server_provider=KbAuthProvider(self.control_path, url))
        host = url.split("://", 1)[1]
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=[host],
                                             allowed_origins=[url])
        app = server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
                                         transport_security=security, host=host)
        return server, app

    @asynccontextmanager
    async def running(self):
        server, app = self.build()
        async with server.session_manager.run():
            self._app = app
            try:
                yield
            finally:
                self._app = None

    async def __call__(self, scope, receive, send):
        if self._app is None:  # lifespan 밖(기동 전·종료 뒤)
            if scope["type"] == "http":
                from starlette.responses import JSONResponse
                await JSONResponse({"error": "MCP 가 준비되지 않았다."}, status_code=503)(scope, receive, send)
            return
        await self._app(scope, receive, send)
