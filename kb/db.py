"""SQLite 연결, 트랜잭션, meta. 연결할 때 스키마가 없으면 만든다."""
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from kb.schema import BUMP, NOW, SCHEMA_VERSION, ddl

__all__ = ["BUMP", "NOW", "connect", "open_db", "tx", "get_meta", "set_meta"]


def _regexp(pattern: str, value: str | None) -> bool:
    return value is not None and re.search(pattern, value) is not None


def open_db(path: Path | str) -> sqlite3.Connection:
    """연결 설정(WAL, 외래 키, 잠금 대기 5초)만 하고 스키마는 건드리지 않는다. 제어 DB(kb/control.py)도 쓴다."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: 트랜잭션은 tx() 가 BEGIN IMMEDIATE 로 직접 연다
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def connect(path: Path | str) -> sqlite3.Connection:
    conn = open_db(path)
    conn.create_function("regexp", 2, _regexp, deterministic=True)
    if _schema_version(conn) != SCHEMA_VERSION:
        _upgrade(conn)
    return conn


def _upgrade(conn: sqlite3.Connection) -> None:
    """스키마가 없으면 만들고, 옛 버전이면 MIGRATIONS 의 단계를 버전 순으로 적용한 뒤 ddl() 로 트리거·새 표·버전을 맞춘다.
    이 코드보다 새 버전의 DB 는 열지 않는다(그대로 열면 ddl() 이 버전을 낮춰 적고 옛 트리거로 바꾼다)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        have = _schema_version(conn)  # 잠금을 얻는 사이 다른 프로세스가 올렸을 수 있어 다시 읽는다
        if have is not None and have.isdigit() and int(have) > int(SCHEMA_VERSION):
            raise sqlite3.DatabaseError(
                f"이 저장소의 스키마 버전({have})이 프로그램의 버전({SCHEMA_VERSION})보다 높습니다. 프로그램을 최신으로 올려 주세요.")
        if have is not None:
            for below, step in MIGRATIONS:
                if not have.isdigit() or int(have) < below:
                    step(conn)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    # 두 프로세스가 동시에 처음 열어도 한쪽만 만들도록 스크립트 전체를 IMMEDIATE 트랜잭션으로 감싼다
    conn.executescript("BEGIN IMMEDIATE;\n" + ddl() + "\nCOMMIT;")


_OLD_ANSWER_LINES = "length(answer) - length(replace(answer, char(10), '')) <= 1 AND"
_NEW_ANSWER_LINES = "length(answer) - length(replace(answer, char(10), '')) <= 5 AND"


def _relax_answer_lines(conn: sqlite3.Connection) -> None:
    """스키마 2 이하의 qa 표는 답변 줄바꿈을 1개까지만 받는다. CHECK 는 ALTER TABLE 로 바꿀 수 없어
    SQLite 문서의 writable_schema 절차로 표 정의 문자열만 고친다. 한도를 넓히기만 해 기존 행은 그대로 맞는다."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'qa'").fetchone()
    if row is None or _OLD_ANSWER_LINES not in row[0]:
        return
    ver = conn.execute("PRAGMA schema_version").fetchone()[0]
    conn.execute("PRAGMA writable_schema = ON")
    try:
        conn.execute("UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'qa'",
                     (row[0].replace(_OLD_ANSWER_LINES, _NEW_ANSWER_LINES),))
        conn.execute(f"PRAGMA schema_version = {ver + 1}")
    finally:
        conn.execute("PRAGMA writable_schema = OFF")


_OLD_EVIDENCE_KINDS = "CHECK (kind IN ('file','dir','git','githead','url','note'))"
_NEW_EVIDENCE_KINDS = "CHECK (kind IN ('file','dir','git','githead','url','note','github'))"


def _widen_evidence_kinds(conn: sqlite3.Connection) -> None:
    """스키마 4 이하의 evidence 표는 근거 종류에 github 가 없다. _relax_answer_lines 와 같은 writable_schema 절차로
    표 정의 문자열의 CHECK 만 넓힌다(기존 행은 그대로 맞는다)."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'evidence'").fetchone()
    if row is None or _OLD_EVIDENCE_KINDS not in row[0]:
        return
    ver = conn.execute("PRAGMA schema_version").fetchone()[0]
    conn.execute("PRAGMA writable_schema = ON")
    try:
        conn.execute("UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'evidence'",
                     (row[0].replace(_OLD_EVIDENCE_KINDS, _NEW_EVIDENCE_KINDS),))
        conn.execute(f"PRAGMA schema_version = {ver + 1}")
    finally:
        conn.execute("PRAGMA writable_schema = OFF")


# (이 버전 미만의 DB 에 적용, 단계 함수). 버전 순으로 둔다. 표를 새로 만들거나 트리거를 바꾸는 변경은 ddl() 이 하므로
# 단계가 필요 없다. 기존 표의 정의·데이터를 바꾸는 변경만 SCHEMA_VERSION 을 올리면서 여기에 단계를 더한다.
# 단계는 _upgrade 의 트랜잭션 안에서 돌고, 이미 적용된 DB 에 다시 돌아도 바뀌는 것이 없어야 한다.
MIGRATIONS = [
    (3, _relax_answer_lines),
    (5, _widen_evidence_kinds),
]


def _schema_version(conn: sqlite3.Connection) -> str | None:
    """스키마가 이미 있으면 쓰기 잠금 없이 확인만 한다(web 은 요청마다 연결을 연다)."""
    try:
        return get_meta(conn, "schema_version")
    except sqlite3.OperationalError:  # meta 테이블이 아직 없다
        return None


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
