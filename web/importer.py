"""로컬 DB 파일을 원격 사용자 DB 로 옮긴다(POST /api/import/db, 원격 모드). 쓰기 주체는 user 다.

요청 본문은 SQLite 파일 그대로(application/octet-stream)이고 IMPORT_MAX_BYTES 까지 받는다. 사용자 PC 쪽은
scripts/push_local.py 가 온라인 백업 사본을 만들어 개인 토큰으로 올린다. 브라우저 세션으로도 부를 수 있다.
받은 파일은 integrity_check → meta.schema_version(이 서버와 같아야 한다) → 표 목록(kb/schema.py 와 같아야 한다)
순서로 검사하고, 하나라도 틀리면 400 으로 거부하고 사용자 DB 를 건드리지 않는다.
- 사용자 DB 가 없거나 비었으면(system 이 아닌 주체가 만든 행이 없다. 기본 가이드만 있는 DB 도 여기에 든다)
  SQLite 백업 API 로 파일 내용을 통째로 바꾼다. revision 이력까지 그대로 옮겨진다.
- 비어 있지 않으면 살아 있는 항목을 하나씩 add_qa 로 넣는다. 가져오기 전부터 있던 항목과 질문이 비슷하면
  (Store.similar 와 같은 기준) 넣지 않고 skipped 로 돌려준다. 근거는 파일의 캡처값을 그대로 넣고(Store(remote=True)
  의 captured 경로) 판정은 same 으로 시작한다. 보관한 항목, 관련 질문 연결, 사전(동의어·불용어·사용자 단어)은
  옮기지 않는다.
- 쿼리 origin(올린 PC 의 device_id)이 있으면 파일·폴더·git 근거의 캡처값에 origin 으로 붙인다. 로컬 검사기는 자기
  PC 의 origin 이 붙은 근거만 판정하므로, 붙이지 않으면 옮긴 근거를 아무 PC 도 판정하지 않는다.
"""
import json
import re
import shutil
import sqlite3
import tempfile
import threading
from functools import lru_cache
from pathlib import Path
from typing import Callable

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from kb import search as search_mod
from kb.db import BUMP, connect, tx
from kb.errors import KbError
from kb.evidence import ROW_KEYS, spec_of
from kb.schema import SCHEMA_VERSION
from kb.store import SIMILAR_MIN, Store, bigrams, dice

IMPORT_MAX_BYTES = 200 * 1024 * 1024
CHECKED_KINDS = ("file", "dir", "git", "githead")
ORIGIN_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
AUDITED_BY = ("category", "qa", "evidence", "qa_link", "synonym", "user_word", "stopword")


class ImportRefused(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "invalid_db"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


@lru_cache(maxsize=1)
def expected_tables() -> frozenset[str]:
    conn = connect(":memory:")
    try:
        return _tables(conn)
    finally:
        conn.close()


def _tables(conn: sqlite3.Connection) -> frozenset[str]:
    return frozenset(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"))


def validate(path: Path) -> None:
    """kb.db.connect 는 스키마를 깔고 이전하므로 쓰지 않고 sqlite3 로 그대로 연다."""
    try:
        conn = sqlite3.connect(str(path))
    except sqlite3.Error as e:
        raise ImportRefused(f"SQLite 파일을 열지 못했다: {e}") from None
    try:
        try:
            check = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
        except sqlite3.DatabaseError as e:
            raise ImportRefused(f"SQLite 파일이 아니거나 손상됐다: {e}") from None
        if check != ["ok"]:
            raise ImportRefused(f"파일이 손상됐다(integrity_check): {'; '.join(check[:3])}")
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.DatabaseError:
            row = None
        if row is None:
            raise ImportRefused("fast-question DB 가 아니다(meta.schema_version 이 없다).")
        if row[0] != SCHEMA_VERSION:
            raise ImportRefused(f"스키마 버전이 {row[0]} 이다. 이 서버는 {SCHEMA_VERSION} 만 받는다. "
                                "로컬 저장소를 최신으로 올리고 웹 서버를 한 번 띄운 뒤 다시 올린다.")
        tables = _tables(conn)
        if tables != expected_tables():
            diff = sorted(tables ^ expected_tables())
            raise ImportRefused(f"표 목록이 이 서버의 스키마와 다르다: {', '.join(diff)}")
    finally:
        conn.close()


def is_empty(conn: sqlite3.Connection) -> bool:
    return not any(conn.execute(f"SELECT 1 FROM {t} WHERE created_by != 'system' LIMIT 1").fetchone()
                   for t in AUDITED_BY)


def _drop_cache(db: Path) -> None:
    """검색 메모리 사본은 revision·qa 최대 id 로 갱신을 판정한다. 파일을 바꾸면 우연히 같을 수 있어 지운다."""
    with search_mod._LOCK:
        for key in [k for k in search_mod._CACHE if Path(k).resolve() == db.resolve()]:
            del search_mod._CACHE[key]


def _stamp_origin(conn: sqlite3.Connection, origin: str) -> None:
    kinds = ", ".join(f"'{k}'" for k in CHECKED_KINDS)
    with tx(conn):
        conn.execute(f"UPDATE evidence SET captured_json = json_set(captured_json, '$.origin', ?), "
                     f"updated_by = 'system', {BUMP} WHERE kind IN ({kinds}) AND captured_json IS NOT NULL "
                     "AND json_extract(captured_json, '$.origin') IS NULL", (origin,))


def replace(db: Path, upload: Path, origin: str = "") -> dict:
    src = sqlite3.connect(str(upload))
    dst = connect(db)
    try:
        src.backup(dst)  # 파일을 바꿔 끼우지 않는다. 남은 -wal 이 새 파일에 다시 적용되는 일을 막는다
        dst.execute("PRAGMA journal_mode = WAL")
        with tx(dst):  # 로컬의 마지막 백업 시각이 따라오면 서버 백업이 그만큼 미뤄진다
            dst.execute("DELETE FROM meta WHERE key = 'last_backup'")
        if origin:
            _stamp_origin(dst, origin)
        items = dst.execute("SELECT count(*) FROM qa WHERE deleted_at IS NULL").fetchone()[0]
    finally:
        dst.close()
        src.close()
    _drop_cache(db)
    return {"mode": "replaced", "items": items}


def _category_paths(src: sqlite3.Connection) -> dict[int, tuple[str, str | None, str]]:
    """분류 id → (이름, 상위 이름, 설명)."""
    rows = {r[0]: r for r in src.execute("SELECT id, parent_id, name, description FROM category")}
    return {cid: (r[2], rows[r[1]][2] if r[1] in rows else None, r[3] or "") for cid, r in rows.items()}


def _ensure_category(s: Store, name: str, parent: str | None, description: str) -> str:
    path = name if parent is None else f"{parent} > {name}"
    try:
        s.resolve_category(path)
    except KbError:
        if parent is not None:
            try:
                s.resolve_category(parent)
            except KbError:
                s.add_category(parent)
        s.add_category(name, parent, description)
    return path


def _evidence_specs(src: sqlite3.Connection, qa_id: int, origin: str = "") -> list[dict]:
    specs = []
    for r in src.execute("SELECT * FROM evidence WHERE qa_id = ? AND deleted_at IS NULL ORDER BY id", (qa_id,)):
        if r["kind"] in ("url", "note"):
            specs.append(spec_of(r))  # 서버가 직접 만드는 근거다
            continue
        row = {k: r[k] for k in ROW_KEYS}
        if origin and row["captured_json"] is not None:
            cap = json.loads(row["captured_json"])
            row["captured_json"] = json.dumps({"origin": origin, **cap}, ensure_ascii=False)
        specs.append({"captured": row})
    return specs


def merge(db: Path, upload: Path, analyzer=None, origin: str = "") -> dict:
    src = sqlite3.connect(str(upload))
    src.row_factory = sqlite3.Row
    conn = connect(db)
    try:
        s = Store(conn, "user", analyzer, remote=True)
        before = [(r["id"], r["question"], bigrams(r["question"]))
                  for r in conn.execute("SELECT id, question FROM qa WHERE deleted_at IS NULL")]
        cats = _category_paths(src)
        added, skipped, failed = 0, [], []
        for r in src.execute("SELECT id, category_id, question, answer FROM qa WHERE deleted_at IS NULL ORDER BY id"):
            target = bigrams(r["question"])
            similar = sorted(((round(dice(target, b), 2), i, q) for i, q, b in before
                              if dice(target, b) >= SIMILAR_MIN), reverse=True)[:5]
            if similar:
                skipped.append({"question": r["question"],
                                "similar": [{"id": i, "question": q, "score": sc} for sc, i, q in similar]})
                continue
            try:
                path = _ensure_category(s, *cats[r["category_id"]])
                s.add_qa(path, r["question"], r["answer"], evidence=_evidence_specs(src, r["id"], origin),
                        force=True)
                added += 1
            except KbError as e:
                failed.append({"question": r["question"], "error": e.message})
    finally:
        conn.close()
        src.close()
    return {"mode": "merged", "added": added, "skipped": skipped, "failed": failed}


def run_import(db: Path, upload: Path, analyzer=None, lock=None, origin: str = "") -> dict:
    if origin and not ORIGIN_RE.fullmatch(origin):
        raise ImportRefused("origin 은 영문·숫자·- 64자 이하다.")
    validate(upload)
    with lock or threading.Lock():
        if not db.exists():
            return replace(db, upload, origin)
        conn = connect(db)
        try:
            empty = is_empty(conn)
        finally:
            conn.close()
        return replace(db, upload, origin) if empty else merge(db, upload, analyzer, origin)


def route(db_for: Callable[[Request], Path | None], tmp_root: Path, analyzer, lock) -> Route:
    """db_for: 요청의 계정 DB 경로(로그인 전이면 None). tmp_root: 받은 파일을 잠시 둘 폴더(데이터 디스크 안)."""
    return Route("/api/import/db", handler(db_for, tmp_root, analyzer, lock), methods=["POST"])


def handler(db_for: Callable[[Request], Path | None], tmp_root: Path, analyzer, lock):
    async def import_db(request: Request) -> JSONResponse:
        db = db_for(request)
        if db is None:
            return JSONResponse({"error": "로그인이 필요하다.", "code": "unauthorized"}, status_code=401)
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > IMPORT_MAX_BYTES:
            return _too_large()
        tmp_root.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(dir=tmp_root))
        try:
            upload = work / "upload.sqlite3"
            size = 0
            with upload.open("wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > IMPORT_MAX_BYTES:
                        return _too_large()
                    f.write(chunk)
            if size == 0:
                return JSONResponse({"error": "요청 본문에 SQLite 파일이 없다.", "code": "invalid_db"},
                                    status_code=400)
            try:
                result = await run_in_threadpool(run_import, db, upload, analyzer, lock,
                                                 request.query_params.get("origin", ""))
            except ImportRefused as e:
                return JSONResponse({"error": e.message, "code": e.code}, status_code=e.status)
            except sqlite3.OperationalError as e:
                return JSONResponse({"error": f"DB 가 다른 쓰기로 잠겨 있다. 잠시 뒤 다시 시도한다. ({e})",
                                     "code": "busy"}, status_code=503)
            return JSONResponse(result)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return import_db


def _too_large() -> JSONResponse:
    return JSONResponse({"error": f"가져올 파일은 {IMPORT_MAX_BYTES // (1024 * 1024)} MB 까지 받는다.",
                         "code": "too_large"}, status_code=413)
