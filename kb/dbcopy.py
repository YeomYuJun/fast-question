"""SQLite 온라인 백업 API 로 DB 사본 파일 하나를 만든다(WAL 이어도 사본은 journal_mode DELETE 파일 하나)."""
import sqlite3
from pathlib import Path


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
