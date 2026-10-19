"""HTTP API 와 화면. 쓰기 주체는 user 다.

화면(web/static)이 쓰는 API 만 둔다. 근거 재기록·관련 질문·사전·재검증 같은 관리 기능은 MCP 도구에만 있다.
요청마다 SQLite 연결을 새로 열고(스레드 풀에서 실행), 형태소 분석기는 앱에 하나를 둔다.
오류는 ValidationError·KbError 400, NotFoundError 404, ConflictError 409(+ current),
쓰기 잠금 대기 초과 503 으로 돌려준다.
run_jobs 면 기동 직후와 10분마다 주기 작업을 스레드에서 돌린다. 로컬 모드는 kb.jobs.run_due(백업, 근거 재검증,
재색인), 원격 모드는 kb.jobs.run_due_all(사용자 DB 마다 백업·재색인, 제어 DB 백업)이다.

원격 모드(cfg.mode == "remote")는 요청의 계정이 DB 파일(kb/tenant.py)을 정한다. 웹 요청이 사용자 DB 를 처음
만들 때 원격용 기본 가이드(kb/guide.py load_remote)를 1회 넣는다(가져오기가 DB 를 통째로 바꾸면 다시 넣는다). 계정은 인증 계층(web/auth.py
AccountAuth)이 ASGI scope 의 ACCOUNT_KEY 에 넣은 값만 쓰고, 없으면 /api/* 는 401 이다. 로그인 경로 /auth/* 와
/api/me 는 web/auth.py 에, PC 로그인 승인 /cli/authorize 와 토큰 교환 POST /api/cli/token(계정 대신 1회용 코드로
받는다)은 web/cli_login.py 에, 로컬 DB 가져오기 POST /api/import/db 와 미리보기 /api/import/db/plan 은 web/importer.py 에 있다. 원격 MCP(/mcp)는 mcp_server/http.py 를 마지막 라우트로 붙이고 lifespan 에서 시작한다.
원격 모드는 단일 DB(cfg.db_path) 를 쓰지 않고 Kiwi 를 미리 띄우지 않는다.
/healthz 는 인증·Host 검사 없이 열린다. DB 연결(원격은 제어 DB, 로컬은 KB 파일), 데이터 디스크 여유 10% 이상,
형태소 분석기 켜짐을 보고 하나라도 실패면 503 이다. 분석기는 처음 로드를 시도하기 전까지 켜짐으로 본다.
"""
import dataclasses
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlsplit

import httpx
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from kb import control, guide, tenant
from kb.config import Config, load_config
from kb.control import connect_control
from kb.db import connect
from kb.dbcopy import copy_db
from kb.errors import ConflictError, KbError, NotFoundError
from kb.evidence import line_of, parse_line
from kb.jobs import run_due, run_due_all
from kb.morph import Analyzer
from kb.store import Store
from mcp_server.http import RemoteMcp
from web import auth, cli_login, importer, oauth_as
from web.auth import ACCOUNT_KEY
from web.limits import BodyLimit, RateLimit

STATIC = Path(__file__).resolve().parent / "static"
# 화면 파일은 매번 서버에 확인하게 한다(안 바뀌었으면 304). 헤더가 없으면 브라우저가 옛 app.js 를
# 캐시에서 꺼내 새 index.html 과 섞어 쓴다
NO_CACHE = {"Cache-Control": "no-cache"}
JOB_INTERVAL_S = 600
# /healthz 기준. 외부 상태 검사가 503 을 알람으로 받는다(Lightsail 지표에는 메모리·디스크 사용률이 없다)
HEALTH_DISK_MIN = 0.20  # 디스크 80% 사용
HEALTH_MEM_MIN = 0.15  # 메모리 85% 사용. Kiwi 를 띄운 앱이 약 550 MiB 라 2 GB 서버에서 여유가 크지 않다
HEALTH_PATH = "/healthz"
log = logging.getLogger("fast-question.web")


def meminfo_available(text: str) -> float | None:
    """/proc/meminfo 내용에서 MemAvailable / MemTotal. 둘 중 하나라도 없으면 None."""
    kb = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name in ("MemTotal", "MemAvailable") and rest.split():
            kb[name] = int(rest.split()[0])
    if len(kb) < 2 or not kb["MemTotal"]:
        return None
    return round(kb["MemAvailable"] / kb["MemTotal"], 4)


def mem_available() -> float | None:
    """서버(호스트) 메모리 여유 비율. 컨테이너 안에서도 /proc/meminfo 는 호스트 값이다. Linux 가 아니면 None."""
    try:
        return meminfo_available(Path("/proc/meminfo").read_text(encoding="ascii"))
    except OSError:
        return None


class RevalidatedStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers.update(NO_CACHE)
        return resp


class SameOriginOnly:
    """Host 가 허용 목록(로컬 모드 127.0.0.1·localhost + 포트, 원격 모드 public_url 의 호스트)이고,
    Origin 이 있으면 같은 출처인 요청만 받는다.

    다른 사이트가 사용자 브라우저로 보내는 요청(text/plain POST 는 사전 확인 없이 간다)과
    DNS 재바인딩으로 KB 를 읽거나 사용자 이름으로 항목을 심는 것을 막는다.
    상태 점검(/healthz)은 사용자 데이터를 돌려주지 않아 검사하지 않는다. 컨테이너 안 점검은 Host 가 도메인이 아니다.
    """

    def __init__(self, app, hosts: set[str]):
        self.app = app
        self.hosts = hosts
        self.message = f"이 서버는 {'·'.join(sorted(h.split(':')[0] for h in hosts))} 의 같은 출처 요청만 받는다."

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != HEALTH_PATH:
            headers = {k.lower(): v.decode("latin-1").lower() for k, v in scope["headers"]}
            host = headers.get(b"host", "")
            origin = headers.get(b"origin")
            if host not in self.hosts or (origin is not None and origin.split("://", 1)[-1] not in self.hosts):
                resp = JSONResponse({"error": self.message}, status_code=403)
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


# 화면은 외부 CDN·글꼴·인라인 스크립트/스타일을 쓰지 않는다(theme.js, oauth.css 로 뺐다). JS 가 element.style 로
# 높이를 바꾸는 것은 CSP 가 막지 않는다. form-action 은 두지 않는다: 동의 화면 승인 뒤 303 으로 클라이언트 콜백
# 주소(다른 출처)로 가는데 Chrome 은 폼 제출 뒤 리다이렉트에도 form-action 을 적용한다
CSP = "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'"


class SecurityHeaders:
    """모든 HTTP 응답에 Content-Security-Policy 를 붙인다. 가장 바깥 미들웨어라 다른 미들웨어의 거부 응답에도 붙는다."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Content-Security-Policy"] = CSP
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _qa_out(item: dict) -> dict:
    """화면 편집 폼이 그대로 보여 주고 되돌려 받는 근거 한 줄을 붙인다."""
    if "evidence" in item:
        item["evidence_line"] = line_of(item["evidence"])
    return item


def _evidence(body: dict) -> list[dict] | None:
    line = body.get("evidence_line")
    return None if line is None else parse_line(line)


def export_markdown(store: Store, category_id: int) -> tuple[str, str]:
    """(분류 경로, Markdown). 분류 경로를 제목으로, 항목마다 "### 질문" 과 답변을 쓴다.
    대분류는 바로 아래 항목 뒤에 중분류마다 "## 경로" 절을 둔다. 삭제한 항목과 빈 중분류는 뺀다."""
    tree = store.categories()
    cat = next((c for t in tree for c in [t, *t["children"]] if c["id"] == category_id), None)
    if cat is None:
        raise NotFoundError(f"분류를 찾지 못했다: {category_id}")

    def items(cid: int) -> list[str]:
        rows = store.conn.execute(
            "SELECT question, answer FROM qa WHERE category_id = ? AND deleted_at IS NULL ORDER BY id", (cid,))
        return [f"### {q}\n\n{a}\n" for q, a in rows]

    parts = [f"# {cat['path']}\n", *items(cat["id"])]
    for child in cat["children"]:
        if body := items(child["id"]):
            parts += [f"## {child['path']}\n", *body]
    return cat["path"], "\n".join(parts)


def create_app(cfg: Config | None = None, *, db_path: Path | None = None, run_jobs: bool = True,
               analyzer: Analyzer | None = None, http_transport: httpx.AsyncBaseTransport | None = None) -> Starlette:
    """http_transport: 원격 모드 로그인이 GitHub·Google 을 부를 때 쓰는 httpx 전송(테스트가 바꿔 끼운다)."""
    cfg = cfg or load_config()
    remote = cfg.mode == "remote"
    if analyzer is None:  # 원격 모드의 사용자 단어는 제어 DB 의 서버 전역 사전이다
        analyzer = (Analyzer(words=control.words_source(tenant.control_path(cfg.data_dir))) if remote
                    else Analyzer())
    mcp = RemoteMcp(cfg, analyzer) if remote else None
    if remote:
        path = None
        connect_control(tenant.control_path(cfg.data_dir)).close()  # 스키마 준비
    else:
        path = Path(db_path or cfg.db_path)
        connect(path).close()  # 스키마 준비

        def warm_up() -> None:  # Kiwi 첫 로드(약 2초)를 첫 검색이 떠안지 않게 미리 띄운다
            conn = connect(path)
            try:
                analyzer.ready(conn)
            finally:
                conn.close()

        threading.Thread(target=warm_up, name="kiwi-warmup", daemon=True).start()

    def db_for(request: Request) -> Path | None:
        """이 요청이 쓸 KB 파일. 원격 모드에서 인증 계층이 계정을 넣지 않았으면 None."""
        if not remote:
            return path
        account_id = request.scope.get(ACCOUNT_KEY)
        return None if account_id is None else tenant.db_path_for(cfg.data_dir, account_id)

    user_db_lock = threading.Lock()  # 사용자 DB 를 새로 만들거나 통째로 바꾸는 일을 한 번에 하나만 한다

    def open_kb(db: Path) -> sqlite3.Connection:
        """요청의 KB 연결. 원격 모드에서 파일이 없으면 만들면서 기본 가이드를 넣는다(스레드 풀에서 부른다)."""
        if not remote or db.exists():
            return connect(db)
        with user_db_lock:
            fresh = not db.exists()
            conn = connect(db)
            if fresh:
                try:
                    log.info("기본 가이드(%s): %s", db.parent.name,
                             guide.seed(Store(conn, "system"), guide.load_remote(cfg.public_url)))
                except Exception:  # noqa: BLE001 - 가이드를 못 넣어도 요청은 처리한다
                    log.exception("기본 가이드를 넣지 못했다: %s", db)
            return conn

    if mcp is not None:
        mcp.opener = open_kb  # MCP 로 처음 연결한 계정도 기본 가이드를 받는다

    def unauthorized() -> JSONResponse:
        return JSONResponse({"error": "로그인이 필요하다.", "code": "unauthorized"}, status_code=401)

    job_cfg = cfg if db_path is None else dataclasses.replace(cfg, db_path=path)

    def jobs_loop(stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                run_due_all(cfg, analyzer) if remote else run_due(job_cfg, analyzer)
            except Exception:  # 다음 주기에 다시 돈다
                log.exception("주기 작업 실패")
            stop.wait(JOB_INTERVAL_S)

    @asynccontextmanager
    async def lifespan(_app):
        stop = threading.Event()
        if run_jobs:
            threading.Thread(target=jobs_loop, args=(stop,), name="jobs", daemon=True).start()
        if mcp is None:
            yield
        else:
            async with mcp.running():
                yield
        stop.set()

    def api(fn: Callable[[Store, Request, dict], object]):
        async def handler(request: Request) -> JSONResponse:
            db = db_for(request)
            if db is None:
                return unauthorized()
            body: dict = {}
            if request.method in ("POST", "PATCH"):
                raw = await request.body()
                if raw:
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        return JSONResponse({"error": "요청 본문이 JSON 이 아니다."}, status_code=400)

            def work():
                conn = open_kb(db)
                try:
                    return fn(Store(conn, "user", analyzer, remote=remote), request, body)
                finally:
                    conn.close()

            try:
                data = await run_in_threadpool(work)
            except ConflictError as e:
                return JSONResponse({"error": e.message, "code": e.code, "current": e.current}, status_code=409)
            except NotFoundError as e:
                return JSONResponse({"error": e.message, "code": e.code}, status_code=404)
            except KbError as e:
                return JSONResponse({"error": e.message, "code": e.code}, status_code=400)
            except sqlite3.OperationalError as e:  # busy_timeout(5초) 안에 쓰기 잠금을 얻지 못했다
                return JSONResponse({"error": f"DB 가 다른 쓰기로 잠겨 있다. 잠시 뒤 다시 시도한다. ({e})",
                                     "code": "busy"}, status_code=503)
            except (KeyError, TypeError, ValueError) as e:
                return JSONResponse({"error": f"요청 값이 올바르지 않다: {e}"}, status_code=400)
            return JSONResponse(data)
        return handler

    async def export(request: Request) -> Response:
        """분류 하나를 Markdown 파일로 내려준다. 오류는 api() 와 같은 JSON 이다."""
        db = db_for(request)
        if db is None:
            return unauthorized()
        raw = request.query_params.get("category", "")
        if not raw.isdigit():
            return JSONResponse({"error": "category 에 분류 id 가 필요하다."}, status_code=400)

        def work():
            conn = open_kb(db)
            try:
                return export_markdown(Store(conn, "user", analyzer, remote=remote), int(raw))
            finally:
                conn.close()

        try:
            cat_path, text = await run_in_threadpool(work)
        except NotFoundError as e:
            return JSONResponse({"error": e.message, "code": e.code}, status_code=404)
        except sqlite3.OperationalError as e:
            return JSONResponse({"error": f"DB 가 다른 쓰기로 잠겨 있다. 잠시 뒤 다시 시도한다. ({e})",
                                 "code": "busy"}, status_code=503)
        name = re.sub(r'[\\/:*?"<>|]+', "_", cat_path.replace(" > ", "-")) + ".md"
        # 한글 파일 이름은 latin-1 헤더에 그대로 못 넣어 RFC 5987 형식으로 보낸다
        return Response(text, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    async def export_db(request: Request) -> Response:
        """내 DB 전체를 SQLite 파일 하나로 내려준다. 로컬 모드 config.json 의 db_path 로 그대로 열린다."""
        db = db_for(request)
        if db is None:
            return unauthorized()

        def work() -> str:
            fd, tmp = tempfile.mkstemp(prefix="fq-export-", suffix=".sqlite3")
            os.close(fd)
            conn = open_kb(db)
            try:
                copy_db(conn, Path(tmp))
            finally:
                conn.close()
            return tmp

        try:
            tmp = await run_in_threadpool(work)
        except sqlite3.OperationalError as e:
            return JSONResponse({"error": f"DB 가 다른 쓰기로 잠겨 있다. 잠시 뒤 다시 시도한다. ({e})",
                                 "code": "busy"}, status_code=503)
        return FileResponse(tmp, media_type="application/vnd.sqlite3",
                            filename=f"fast-question-{datetime.now():%Y%m%d}.sqlite3",
                            background=BackgroundTask(os.unlink, tmp))

    def health_checks() -> dict:
        out: dict = {"db": "ok"}
        try:
            conn = connect_control(tenant.control_path(cfg.data_dir)) if remote else connect(path)
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001 - 어떤 실패든 점검 실패로 돌려준다
            out["db"] = f"{type(e).__name__}: {e}"
        try:
            usage = shutil.disk_usage(cfg.data_dir if remote else path.parent)
            out["disk_free"] = round(usage.free / usage.total, 4)
        except OSError as e:
            out["disk_free"], out["disk_error"] = None, f"{type(e).__name__}: {e}"
        out["mem_available"] = mem_available()
        out["morph"] = bool(analyzer.enabled)
        out["ok"] = (out["db"] == "ok" and out["disk_free"] is not None
                     and out["disk_free"] >= HEALTH_DISK_MIN and out["morph"]
                     and (out["mem_available"] is None or out["mem_available"] >= HEALTH_MEM_MIN))
        return out

    async def healthz(_request: Request) -> JSONResponse:
        out = await run_in_threadpool(health_checks)
        return JSONResponse(out, status_code=200 if out["ok"] else 503)

    def pid(request: Request, name: str = "id") -> int:
        return int(request.path_params[name])

    def search(s: Store, r: Request, _):
        qp = r.query_params
        # since: 그 시간 안에 등록된 항목만. relax=1: 단어 빼기 제안을 붙인다(화면의 본 검색만 켠다.
        # 즉석 질문 훅·건수만 세는 요청은 넘기지 않아 단어 수만큼의 추가 검색을 하지 않는다)
        return s.search(qp.get("q", ""), category_id=int(qp["category"]) if qp.get("category") else None,
                        limit=int(qp.get("limit", 200)), relax=qp.get("relax") == "1",
                        since_hours=float(qp["since"]) if qp.get("since") else None)

    def evidence_check(s: Store, r: Request, _):
        """로컬 검사기(scripts/local_check.py, 로컬 프록시 kb_recheck)가 판정할 이 PC 의 근거."""
        qa = r.query_params.get("qa")
        return {"rows": s.evidence_for_check(r.query_params["origin"], int(qa) if qa else None)}

    def verdicts(s: Store, _, b: dict):
        results = s.apply_verdicts(list(b["results"]))
        ids = sorted({x["qa_id"] for x in results})
        return {"results": results, "items": [{"id": i["id"], "question": i["question"],
                                               "evidence_state": i["evidence_state"]} for i in s.items(ids)]}

    def add_qa(s: Store, _, b: dict):
        res = s.add_qa(b["category"], b["question"], b["answer"], evidence=_evidence(b), force=bool(b.get("force")))
        if res["status"] == "created":
            _qa_out(res["item"])
        return res

    routes = [
        Route("/", lambda r: FileResponse(STATIC / "index.html", headers=NO_CACHE)),
        Route(HEALTH_PATH, healthz),
        Route("/api/search", api(search)),
        Route("/api/categories", api(lambda s, r, b: {"categories": s.categories()})),
        Route("/api/projects", api(lambda s, r, b: s.project_sources())),
        Route("/api/evidence/check", api(evidence_check)),
        Route("/api/evidence/verdicts", api(verdicts), methods=["POST"]),
        Route("/api/categories", api(lambda s, r, b: s.add_category(
            b["name"], parent=b.get("parent"), description=b.get("description", ""))), methods=["POST"]),
        Route("/api/categories/{id:int}", api(lambda s, r, b: s.update_category(
            pid(r), b["version"], name=b.get("name"), description=b.get("description"))), methods=["PATCH"]),
        Route("/api/export", export),
        Route("/api/export/db", export_db),
        Route("/api/categories/{id:int}/impact", api(lambda s, r, b: s.category_impact(pid(r)))),
        Route("/api/categories/{id:int}/archive", api(lambda s, r, b: s.archive_category(
            pid(r), b["version"])), methods=["POST"]),
        Route("/api/qa", api(add_qa), methods=["POST"]),
        Route("/api/qa/{id:int}", api(lambda s, r, b: _qa_out(s.get_qa(pid(r))))),
        Route("/api/qa/{id:int}", api(lambda s, r, b: _qa_out(s.update_qa(
            pid(r), b["version"], question=b.get("question"), answer=b.get("answer"),
            category=b.get("category"), evidence=_evidence(b), note=b.get("note")))), methods=["PATCH"]),
        Route("/api/qa/{id:int}/archive", api(lambda s, r, b: _qa_out(s.archive_qa(
            pid(r), b["version"], note=b.get("note")))), methods=["POST"]),
        Route("/api/qa/{id:int}/restore", api(lambda s, r, b: _qa_out(s.restore_qa(
            pid(r), b["version"], note=b.get("note")))), methods=["POST"]),
        Mount("/static", RevalidatedStaticFiles(directory=STATIC), name="static"),
    ]
    middleware = [Middleware(SecurityHeaders),
                  Middleware(SameOriginOnly, hosts={f"127.0.0.1:{cfg.port}", f"localhost:{cfg.port}"})]
    if remote:
        control_file = tenant.control_path(cfg.data_dir)
        routes[1:1] = auth.routes(cfg, auth.providers_from(cfg, http_transport), control_file)
        routes[1:1] = oauth_as.routes(auth.providers_from(cfg), control_file)  # /oauth/continue/<id>
        routes[1:1] = cli_login.routes(auth.providers_from(cfg), control_file)  # /cli/authorize, /api/cli/token
        routes[1:1] = importer.routes(db_for, cfg.data_dir / "tmp", analyzer, user_db_lock,
                                       lambda: guide.load_remote(cfg.public_url))
        routes.append(Mount("/", app=mcp))  # /mcp 와 OAuth 메타데이터. 다른 라우트가 먼저 맞는다
        middleware = [Middleware(SecurityHeaders),
                      Middleware(SameOriginOnly, hosts={urlsplit(cfg.public_url).netloc.lower()}),
                      Middleware(BodyLimit),
                      Middleware(auth.AccountAuth, control_path=control_file, cookie=auth.cookie_names(cfg)[0]),
                      Middleware(RateLimit),
                      Middleware(oauth_as.RevokeFieldLog)]
    else:
        routes.insert(1, Route("/api/me", lambda r: JSONResponse({"mode": "local"})))
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.db_path = path
    app.state.analyzer = analyzer
    app.state.run_jobs = run_jobs
    return app
