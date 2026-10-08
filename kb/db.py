"""SQLite 연결, 트랜잭션, meta. 연결할 때 스키마가 없으면 만든다."""
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from kb.schema import BUMP, NOW, ddl

__all__ = ["BUMP", "NOW", "connect", "tx", "get_meta", "set_meta"]


def _regexp(pattern: str, value: str | None) -> bool:
    return value is not None and re.search(pattern, value) is not None


def connect(path: Path | str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: 트랜잭션은 tx() 가 BEGIN IMMEDIATE 로 직접 연다
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.create_function("regexp", 2, _regexp, deterministic=True)
    # 두 프로세스가 동시에 처음 열어도 한쪽만 만들도록 스크립트 전체를 IMMEDIATE 트랜잭션으로 감싼다
    conn.executescript("BEGIN IMMEDIATE;\n" + ddl() + "\nCOMMIT;")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if conn.in_transaction:  # 바깥 트랜잭션에 합류
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return default if row is None else row[0]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
