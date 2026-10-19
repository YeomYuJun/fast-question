"""SQLite 온라인 백업 API 로 DB 사본 파일 하나를 만든다(WAL 이어도 사본은 journal_mode DELETE 파일 하나).

prune() 은 원격 서버로 올릴 사본에서 기본 가이드와 제외할 분류를 행째 지운다. 보관(deleted_at)으로는 부족하다:
통째로 바꾸는 가져오기는 보관한 행, revision 의 before/after, 검색 색인까지 서버로 옮긴다.
"""
import sqlite3
from pathlib import Path

from kb.schema import ddl


def copy_db(conn: sqlite3.Connection, dst: Path) -> Path:
    """사본을 만들고 quick_check 가 ok 가 아니면 지우고 RuntimeError."""
    target = sqlite3.connect(dst)
    try:
        conn.backup(target)
        target.execute("PRAGMA journal_mode = DELETE")
        check = target.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        target.close()
    if check != "ok":
        Path(dst).unlink(missing_ok=True)
        raise RuntimeError(f"사본의 quick_check 가 실패했다: {check}")
    return Path(dst)


def _resolve(cats: dict[int, sqlite3.Row], path: str) -> int:
    """살아 있는 분류 경로("대분류" 또는 "대분류 > 중분류")의 id. 없으면 ValueError."""
    parts = [p.strip() for p in path.split(">")]
    parent = None
    for name in parts:
        found = [cid for cid, r in cats.items()
                 if r["parent_id"] == parent and r["name"] == name and r["deleted_at"] is None]
        if not found:
            raise ValueError(f"분류를 찾지 못했다: {path}")
        parent = found[0]
    return parent


def _delete(conn: sqlite3.Connection, table: str, ids: set[int], col: str = "id") -> None:
    conn.executemany(f"DELETE FROM {table} WHERE {col} = ?", [(i,) for i in ids])


def prune(path: Path, exclude: list[str] | tuple[str, ...] = ()) -> dict:
    """사본 파일에서 system 이 만든 Q&A(기본 가이드)와 exclude 분류(하위 분류 포함)의 Q&A 를 지운다.

    보관한 항목도 지운다. 그 Q&A 의 근거·연결·이력·검색 색인 행도 지운다. 비게 된 system 분류와 제외한 분류도
    지운다. 물리 삭제를 막는 트리거를 잠시 지웠다가 kb.schema.ddl() 로 되살리고, 지운 내용이 빈 페이지에 남지
    않게 VACUUM 한다. 돌려주는 값: {"system": 지운 기본 가이드 수, "excluded": 제외한 분류에서 지운 Q&A 수}.
    exclude 의 분류가 없으면 ValueError 이고 파일을 바꾸지 않는다.
    """
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        cats = {r["id"]: r for r in conn.execute("SELECT id, parent_id, name, created_by, deleted_at FROM category")}
        drop_cats = set()
        for p in exclude:
            top = _resolve(cats, p)
            drop_cats |= {top} | {cid for cid, r in cats.items() if r["parent_id"] == top}
        qa = conn.execute("SELECT id, category_id, created_by FROM qa").fetchall()
        excluded = {r["id"] for r in qa if r["category_id"] in drop_cats}
        system = {r["id"] for r in qa if r["created_by"] == "system"} - excluded
        drop_qa = excluded | system
        left = {r["category_id"] for r in qa if r["id"] not in drop_qa}
        while True:  # 비게 된 system 분류(하위 분류부터)
            parents = {r["parent_id"] for cid, r in cats.items() if cid not in drop_cats}
            empty = {cid for cid, r in cats.items() if cid not in drop_cats and r["created_by"] == "system"
                     and cid not in left and cid not in parents}
            if not empty:
                break
            drop_cats |= empty
        ph = ",".join("?" * len(drop_qa))
        ev = {r[0] for r in conn.execute(f"SELECT id FROM evidence WHERE qa_id IN ({ph})", list(drop_qa))}
        links = {r[0] for r in conn.execute(f"SELECT id FROM qa_link WHERE a_id IN ({ph}) OR b_id IN ({ph})",
                                            list(drop_qa) * 2)}
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")]
        conn.execute("BEGIN IMMEDIATE")
        try:
            for name in names:
                conn.execute(f"DROP TRIGGER {name}")
            for table, ids in (("qa", drop_qa), ("evidence", ev), ("qa_link", links), ("category", drop_cats)):
                conn.executemany("DELETE FROM revision WHERE table_name = ? AND row_id = ?", [(table, i) for i in ids])
            _delete(conn, "evidence", ev)
            _delete(conn, "qa_link", links)
            _delete(conn, "qa", drop_qa)
            _delete(conn, "qa_fts", drop_qa, "rowid")
            _delete(conn, "qa_morph_fts", drop_qa, "rowid")
            for cid in sorted(drop_cats, key=lambda c: cats[c]["parent_id"] is None):  # 하위 분류 먼저
                conn.execute("DELETE FROM category WHERE id = ?", (cid,))
            conn.execute("INSERT INTO qa_fts(qa_fts) VALUES ('optimize')")
            conn.execute("INSERT INTO qa_morph_fts(qa_morph_fts) VALUES ('optimize')")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        # 트리거를 되살린다. 실패하면 예외로 끝나 이 사본은 올라가지 않는다
        conn.executescript("BEGIN IMMEDIATE;\n" + ddl() + "\nCOMMIT;")
        conn.execute("VACUUM")
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        conn.close()
    if check != "ok":
        raise RuntimeError(f"정리한 사본의 quick_check 가 실패했다: {check}")
    return {"system": len(system), "excluded": len(excluded)}
