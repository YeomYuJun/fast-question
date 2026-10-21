"""원격 모드 요청 제한(토큰 버킷)."""
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tests.test_auth import URL, analyzer, app, cfg, idp, login, secrets_env  # noqa: F401 - 픽스처
from web.auth import ACCOUNT_KEY
from web.limits import RateLimit


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def small_app(clock, account=None, **limits):
    inner = Starlette(routes=[Route("/{p:path}", lambda r: PlainTextResponse("ok"), methods=["GET", "POST"])])
    limited = RateLimit(inner, limits=limits, clock=clock)

    async def with_account(scope, receive, send):
        if account is not None:
            scope = dict(scope, **{ACCOUNT_KEY: account})
        await limited(scope, receive, send)
    return with_account


def test_bucket_refills_over_time():
    clock = Clock()
    with TestClient(small_app(clock, account=1, write=2)) as c:
        assert [c.post("/api/qa").status_code for _ in range(3)] == [200, 200, 429]
        r = c.post("/api/qa")
        assert r.status_code == 429 and r.headers["retry-after"] == "30" and r.json()["code"] == "rate_limited"
        clock.t += 30  # 분당 2건 = 30초에 1건
        assert c.post("/api/qa").status_code == 200
        assert c.get("/api/search").status_code == 200  # 읽기는 다른 버킷


def test_accounts_and_ips_have_separate_buckets():
    clock = Clock()
    with TestClient(small_app(clock, account=1, write=1)) as a, TestClient(small_app(clock, account=2, write=1)) as b:
        assert a.post("/api/qa").status_code == 200
        assert b.post("/api/qa").status_code == 200
    with TestClient(small_app(clock, anon=1)) as anon:
        assert anon.get("/api/search").status_code == 200
        assert anon.get("/api/search").status_code == 429
        assert anon.get("/").status_code == 200 and anon.get("/static/app.js").status_code == 200  # 화면은 제한 없음


def test_register_and_mcp_limits():
    clock = Clock()
    with TestClient(small_app(clock, register=1, mcp=1)) as c:
        assert [c.post("/register").status_code for _ in range(2)] == [200, 429]
        h1, h2 = {"Authorization": "Bearer t-one"}, {"Authorization": "Bearer t-two"}
        assert [c.post("/mcp", headers=h1).status_code for _ in range(2)] == [200, 429]
        assert c.post("/mcp", headers=h2).status_code == 200  # 토큰마다 따로


def test_remote_app_limits_anonymous_api(app):
    with TestClient(app, base_url=URL) as c:
        codes = [c.get("/api/categories").status_code for _ in range(61)]
    assert codes[:60] == [401] * 60 and codes[60] == 429


def test_remote_app_takes_limits_from_config(tmp_path, analyzer):
    from tests.test_auth import make_cfg
    from tests.test_remote_web import client_for
    from web.app import create_app

    app = create_app(make_cfg(tmp_path, limits={"write": 1}), run_jobs=False, analyzer=analyzer)
    with client_for(app, 1) as c:
        assert [c.post("/api/categories", json={"name": n}).status_code for n in ("가", "나")] == [200, 429]
        assert c.get("/api/categories").status_code == 200  # 읽기는 기본값 그대로
