"""공개 링크 라우트(원격 모드). kb/share.py 의 표를 쓴다.

- GET /s/<토큰>: 공개 페이지(web/static/share.html). 로그인 없이 연다. 페이지 스크립트가 아래 API 를 부른다.
- GET /api/share/<토큰>: 공개 내용(제목, 분류 경로, 항목의 질문·답변·수정 시각). 로그인 없이 받는다(비로그인 IP 제한을 받는다).
  없는·폐기한 토큰, 보관한 항목, 삭제한 분류, 탈퇴한 계정은 모두 404 다. 그 계정 DB 는 읽기만 한다.
- 브라우저 세션 전용: GET /api/account/shares(목록), POST /api/account/shares {kind, id}(만들기. 그 계정 DB 에 대상이 살아 있어야
  한다), DELETE /api/account/shares/<id>(폐기).
"""
import sqlite3
from pathlib import Path
from typing import Callable

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from kb import control, share, tenant
from web.auth import ACCOUNT_KEY, AUTH_KIND_KEY

STATIC = Path(__file__).resolve().parent / "static"
NO_CACHE = {"Cache-Control": "no-cache"}


def routes(control_path: Path, data_dir: Path, open_kb: Callable[[Path], sqlite3.Connection], public_url: str) -> list[Route]:
    def db(fn, *args):
        conn = control.connect_control(control_path)
        try:
            return fn(conn, *args)
        finally:
            conn.close()

    def not_found() -> Response:
        return JSONResponse({"error": "공개 링크를 찾지 못했습니다. 폐기됐거나 없는 주소입니다.", "code": "not_found"}, status_code=404)

    def need_session(request: Request) -> Response | None:
        if request.scope.get(AUTH_KIND_KEY) != "session":
            return JSONResponse({"error": "브라우저에서 로그인해 주세요.", "code": "unauthorized"}, status_code=401)
        return None

    def read_shared(token: str) -> dict | None:
        found = db(share.resolve, token)
        if found is None:
            return None
        path = tenant.db_path_for(data_dir, found["account_id"])
        if not path.is_file():
            return None
        kb = open_kb(path)
        try:
            data = share.read_target(kb, found["kind"], found["target_id"])
        finally:
            kb.close()
        return None if data is None else {**data, "created_at": found["created_at"]}

    async def page(_request: Request) -> Response:
        return FileResponse(STATIC / "share.html", headers=NO_CACHE)

    async def api_read(request: Request) -> Response:
        data = await run_in_threadpool(read_shared, request.path_params["token"])
        return not_found() if data is None else JSONResponse(data)

    def target_title(account_id: int, kind: str, target_id: int) -> str | None:
        kb = open_kb(tenant.db_path_for(data_dir, account_id))
        try:
            data = share.read_target(kb, kind, target_id, limit=1)
        finally:
            kb.close()
        return None if data is None else data["title"]

    def with_url(row: dict) -> dict:
        return {**row, "url": f"{public_url}/s/{row['token']}"} if "token" in row else row

    async def shares(request: Request) -> Response:
        if (denied := need_session(request)) is not None:
            return denied
        account_id = request.scope[ACCOUNT_KEY]
        if request.method == "GET":
            return JSONResponse({"shares": await run_in_threadpool(db, share.list_shares, account_id)})
        try:
            body = await request.json()
            kind, target_id = str(body.get("kind", "")), int(body.get("id"))
        except (ValueError, TypeError):
            return JSONResponse({"error": "kind(qa·category)와 id 가 필요합니다.", "code": "invalid"}, status_code=400)
        if kind not in share.KINDS:
            return JSONResponse({"error": "kind 는 qa 또는 category 입니다.", "code": "invalid"}, status_code=400)
        title = await run_in_threadpool(target_title, account_id, kind, target_id)
        if title is None:
            return JSONResponse({"error": "공개할 항목이나 분류를 찾지 못했습니다.", "code": "not_found"}, status_code=404)
        try:
            made = await run_in_threadpool(db, share.create, account_id, kind, target_id, title)
        except share.ShareRefused as e:
            return JSONResponse({"error": str(e), "code": "invalid"}, status_code=400)
        return JSONResponse(with_url(made))

    async def revoke(request: Request) -> Response:
        if (denied := need_session(request)) is not None:
            return denied
        ok = await run_in_threadpool(db, share.revoke, request.scope[ACCOUNT_KEY], int(request.path_params["id"]))
        return JSONResponse({"ok": True}) if ok else JSONResponse({"error": "공개 링크를 찾지 못했습니다.", "code": "not_found"},
                                                                  status_code=404)

    return [
        Route("/s/{token}", page),
        Route("/api/share/{token}", api_read),
        Route("/api/account/shares", shares, methods=["GET", "POST"]),
        Route("/api/account/shares/{id:int}", revoke, methods=["DELETE"]),
    ]
