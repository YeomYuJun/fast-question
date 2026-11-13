"""내보내기. GET /api/export?category=<id> 는 분류 하나를 Markdown 으로, GET /api/export/db 는 내 DB 전체를 SQLite 파일로 내려준다.

DB 사본(최대 200 MB)은 원격 모드에서 데이터 디스크의 tmp 에 두고(디스크 여유를 /healthz 가 본다), 한 계정은 한 번에 하나만
내려받는다(가져오기와 같은 importing 집합. 응답을 읽지 않는 요청을 겹쳐 열어 디스크를 채우지 못하게 한다).
오류 응답은 web/app.py 의 api() 와 같은 JSON 이다(StoreCalls).
"""
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from kb.dbcopy import copy_db
from kb.errors import NotFoundError
from kb.store import Store


def export_markdown(store: Store, category_id: int) -> tuple[str, str]:
    """(분류 경로, Markdown). 분류 경로를 제목으로, 항목마다 "### 질문" 과 답변을 쓴다.
    대분류는 바로 아래 항목 뒤에 중분류마다 "## 경로" 절을 둔다. 삭제한 항목과 빈 중분류는 뺀다."""
    tree = store.categories()
    cat = next((c for t in tree for c in [t, *t["children"]] if c["id"] == category_id), None)
    if cat is None:
        raise NotFoundError(f"분류를 찾지 못했습니다: {category_id}")

    def items(cid: int) -> list[str]:
        rows = store.conn.execute(
            "SELECT question, answer FROM qa WHERE category_id = ? AND deleted_at IS NULL ORDER BY id", (cid,))
        return [f"### {q}\n\n{a}\n" for q, a in rows]

    parts = [f"# {cat['path']}\n", *items(cat["id"])]
    for child in cat["children"]:
        if body := items(child["id"]):
            parts += [f"## {child['path']}\n", *body]
    return cat["path"], "\n".join(parts)


def routes(calls, tmp_dir: Path | None, importing: set[Path]) -> list[Route]:
    """calls: web/app.py 의 StoreCalls. tmp_dir: DB 사본을 둘 폴더(None 이면 시스템 임시 폴더)."""

    async def markdown(request: Request) -> Response:
        raw = request.query_params.get("category", "")
        if not raw.isdigit():
            return JSONResponse({"error": "category 에 분류 id 가 필요합니다."}, status_code=400)
        try:
            cat_path, text = await calls.run(request, lambda s: export_markdown(s, int(raw)))
        except calls.Reply as r:
            return r.response
        name = re.sub(r'[\\/:*?"<>|]+', "_", cat_path.replace(" > ", "-")) + ".md"
        # 한글 파일 이름은 latin-1 헤더에 그대로 못 넣어 RFC 5987 형식으로 보낸다
        return Response(text, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    async def database(request: Request) -> Response:
        """로컬 모드 config.json 의 db_path 로 그대로 열리는 파일을 내려준다."""
        db = calls.db_for(request)
        if db is None:
            return calls.unauthorized()
        if db in importing:
            return JSONResponse({"error": "이 계정의 가져오기나 내보내기가 진행 중입니다. 끝난 뒤 다시 시도해 주세요.",
                                 "code": "busy"}, status_code=409)
        importing.add(db)

        def done(tmp: str) -> None:
            importing.discard(db)
            try:
                os.unlink(tmp)
            except OSError:
                pass

        def work(store: Store) -> str:
            if tmp_dir:
                tmp_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="fq-export-", suffix=".sqlite3", dir=tmp_dir)
            os.close(fd)
            try:
                copy_db(store.conn, Path(tmp))
            except BaseException:
                os.unlink(tmp)
                raise
            return tmp

        try:
            tmp = await calls.run(request, work)
        except calls.Reply as r:
            importing.discard(db)
            return r.response
        except BaseException:
            importing.discard(db)
            raise
        return FileResponse(tmp, media_type="application/vnd.sqlite3",
                            filename=f"fast-question-{datetime.now():%Y%m%d}.sqlite3",
                            background=BackgroundTask(done, tmp))

    return [Route("/api/export", markdown), Route("/api/export/db", database)]
