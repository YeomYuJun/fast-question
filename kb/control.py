"""원격 모드의 제어 DB(data_dir/control.sqlite3): 계정. 사용자 KB 와 다른 파일이고 KB 스키마를 깔지 않는다.

표는 쓰는 단계에서 CREATE TABLE IF NOT EXISTS 로 더한다. 버전은 meta 의 control_schema_version 이다.
"""
import sqlite3
from pathlib import Path

from kb.db import NOW, get_meta, open_db, tx

CONTROL_SCHEMA_VERSION = "1"

DDL = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT ({NOW}),
    deleted_at TEXT,
    display_name TEXT NOT NULL DEFAULT '' CHECK (length(display_name) <= 100)
);

INSERT INTO meta(key, value) VALUES ('control_schema_version', '{CONTROL_SCHEMA_VERSION}')
ON CONFLICT(key) DO UPDATE SET value = excluded.value;
"""


def _version(conn: sqlite3.Connection) -> str | None:
    try:
        return get_meta(conn, "control_schema_version")
    except sqlite3.OperationalError:  # meta 표가 아직 없다
        return None


def connect_control(path: Path | str) -> sqlite3.Connection:
    conn = open_db(path)
    if _version(conn) != CONTROL_SCHEMA_VERSION:
        conn.executescript("BEGIN IMMEDIATE;\n" + DDL + "\nCOMMIT;")
    return conn


def create_account(conn: sqlite3.Connection, display_name: str = "") -> int:
    with tx(conn):
        return conn.execute("INSERT INTO account(display_name) VALUES (?)", (display_name,)).lastrowid


def get_account(conn: sqlite3.Connection, account_id: int) -> dict | None:
    row = conn.execute("SELECT id, created_at, deleted_at, display_name FROM account WHERE id = ?",
                       (account_id,)).fetchone()
    return None if row is None else dict(row)
