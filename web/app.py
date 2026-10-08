"""HTTP API 와 화면. 쓰기 주체는 user 다.

요청마다 SQLite 연결을 새로 열고(스레드 풀에서 실행), 형태소 분석기는 앱에 하나를 둔다.
오류는 ValidationError·KbError 400, NotFoundError 404, ConflictError 409(+ current) 로 돌려준다.
"""
import json
import threading
from pathlib import Path
from typing import Callable

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from kb.config import Config, load_config
from kb.db import connect, get_meta
from kb.errors import ConflictError, KbError, NotFoundError
from kb.morph import Analyzer, pending_count, reindex
from kb.store import Store

STATIC = Path(__file__).resolve().parent / "static"


class SameOriginOnly:
    """Host 가 허용 목록(127.0.0.1·localhost + 포트)이고, Origin 이 있으면 같은 출처인 요청만 받는다.

    다른 사이트가 사용자 브라우저로 보내는 요청(text/plain POST 는 사전 확인 없이 간다)과
    DNS 재바인딩으로 KB 를 읽거나 사용자 이름으로 항목을 심는 것을 막는다.
    """

    def __init__(self, app, hosts: set[str]):
        self.app = app
        self.hosts = hosts

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.lower(): v.decode("latin-1").lower() for k, v in scope["headers"]}
            host = headers.get(b"host", "")
            origin = headers.get(b"origin")
            if host not in self.hosts or (origin is not None and origin.split("://", 1)[-1] not in self.hosts):
                resp = JSONResponse({"error": "이 서버는 127.0.0.1·localhost 의 같은 출처 요청만 받는다."},
                                    status_code=403)
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _flag(request: Request, name: str) -> bool:
    return request.query_params.get(name, "") in ("1", "true", "on")


def create_app(cfg: Config | None = None, *, db_path: Path | None = None, run_jobs: bool = True,
               analyzer: Analyzer | None = None) -> Starlette:
    cfg = cfg or load_config()
    path = Path(db_path or cfg.db_path)
    analyzer = analyzer if analyzer is not None else Analyzer()
    connect(path).close()  # 스키마 준비

    def warm_up() -> None:  # Kiwi 첫 로드(약 2초)를 첫 검색이 떠안지 않게 미리 띄운다
        conn = connect(path)
        try:
            analyzer.ready(conn)
        finally:
            conn.close()

    threading.Thread(target=warm_up, name="kiwi-warmup", daemon=True).start()

    def api(fn: Callable[[Store, Request, dict], object]):
        async def handler(request: Request) -> JSONResponse:
            body: dict = {}
            if request.method in ("POST", "PATCH"):
                raw = await request.body()
                if raw:
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        return JSONResponse({"error": "요청 본문이 JSON 이 아니다."}, status_code=400)

            def work():
                conn = connect(path)
                try:
                    return fn(Store(conn, "user", analyzer), request, body)
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
            except (KeyError, TypeError, ValueError) as e:
                return JSONResponse({"error": f"요청 값이 올바르지 않다: {e}"}, status_code=400)
            return JSONResponse(data)
        return handler

    def pid(request: Request, name: str = "id") -> int:
        return int(request.path_params[name])

    def search(s: Store, r: Request, _):
        qp = r.query_params
        return s.search(qp.get("q", ""), category_id=int(qp["category"]) if qp.get("category") else None,
                        include_archived=_flag(r, "archived"), case_sensitive=_flag(r, "case"),
                        regex=_flag(r, "regex"), only=qp.get("only") or None, limit=int(qp.get("limit", 200)))

    def dict_post(s: Store, _, b: dict):
        b = dict(b)
        res = s.dict_action(b.pop("action"), **b)
        if res["reindex_needed"]:
            res["reindexed"] = reindex(s.conn, analyzer)
        return res

    def status(s: Store, _, __):
        c = s.conn
        return {
            "morph": "off" if not analyzer.enabled else ("on" if analyzer.version else "idle"),
            "analyzer_error": analyzer.error,
            "pending_reindex": pending_count(c),
            "last_recheck": get_meta(c, "last_recheck"),
            "last_backup": get_meta(c, "last_backup"),
            "counts": {
                "qa": c.execute("SELECT count(*) FROM qa WHERE deleted_at IS NULL").fetchone()[0],
                "archived": c.execute("SELECT count(*) FROM qa WHERE deleted_at IS NOT NULL").fetchone()[0],
                "categories": c.execute("SELECT count(*) FROM category WHERE deleted_at IS NULL").fetchone()[0],
            },
            "db_path": str(path),
        }

    routes = [
        Route("/", lambda r: FileResponse(STATIC / "index.html")),
        Route("/api/search", api(search)),
        Route("/api/categories", api(lambda s, r, b: {"categories": s.categories()})),
        Route("/api/categories", api(lambda s, r, b: s.add_category(
            b["name"], parent=b.get("parent"), description=b.get("description", ""))), methods=["POST"]),
        Route("/api/categories/{id:int}", api(lambda s, r, b: s.update_category(
            pid(r), b["version"], name=b.get("name"), description=b.get("description"), note=b.get("note"))),
            methods=["PATCH"]),
        Route("/api/categories/{id:int}/archive", api(lambda s, r, b: s.archive_category(
            pid(r), b["version"], note=b.get("note"))), methods=["POST"]),
        Route("/api/qa", api(lambda s, r, b: s.add_qa(
            b["category"], b["question"], b["answer"], evidence=b.get("evidence"), links=b.get("links"),
            force=bool(b.get("force")))), methods=["POST"]),
        Route("/api/qa/{id:int}", api(lambda s, r, b: s.get_qa(pid(r)))),
        Route("/api/qa/{id:int}", api(lambda s, r, b: s.update_qa(
            pid(r), b["version"], question=b.get("question"), answer=b.get("answer"),
            category=b.get("category"), note=b.get("note"))), methods=["PATCH"]),
        Route("/api/qa/{id:int}/archive", api(lambda s, r, b: s.archive_qa(
            pid(r), b["version"], note=b.get("note"))), methods=["POST"]),
        Route("/api/qa/{id:int}/restore", api(lambda s, r, b: s.restore_qa(
            pid(r), b["version"], note=b.get("note"))), methods=["POST"]),
        Route("/api/qa/{id:int}/review", api(lambda s, r, b: s.review_qa(pid(r))), methods=["POST"]),
        Route("/api/qa/{id:int}/links", api(lambda s, r, b: (
            s.unlink(pid(r), int(b["other"])) if b.get("remove") else s.link(pid(r), int(b["other"])))),
            methods=["POST"]),
        Route("/api/qa/{id:int}/evidence", api(lambda s, r, b: s.add_evidence(pid(r), b)), methods=["POST"]),
        Route("/api/evidence/{id:int}/recapture", api(lambda s, r, b: s.recapture_evidence(pid(r))),
              methods=["POST"]),
        Route("/api/evidence/{id:int}/archive", api(lambda s, r, b: s.archive_evidence(pid(r))), methods=["POST"]),
        Route("/api/recheck", api(lambda s, r, b: {"results": s.recheck(b.get("qa_id"))}), methods=["POST"]),
        Route("/api/dict", api(lambda s, r, b: s.dictionary())),
        Route("/api/dict", api(dict_post), methods=["POST"]),
        Route("/api/reindex", api(lambda s, r, b: {"reindexed": reindex(s.conn, analyzer)}), methods=["POST"]),
        Route("/api/oversight", api(lambda s, r, b: s.oversight())),
        Route("/api/status", api(status)),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    hosts = {f"127.0.0.1:{cfg.port}", f"localhost:{cfg.port}"}
    app = Starlette(routes=routes, middleware=[Middleware(SameOriginOnly, hosts=hosts)])
    app.state.db_path = path
    app.state.analyzer = analyzer
    app.state.run_jobs = run_jobs
    return app
