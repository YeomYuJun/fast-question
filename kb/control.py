"""원격 모드의 제어 DB(data_dir/control.sqlite3): 계정, 로그인 신원, 브라우저 세션, 진행 중인 로그인.
사용자 KB 와 다른 파일이고 KB 스키마를 깔지 않는다.

표는 쓰는 단계에서 CREATE TABLE IF NOT EXISTS 로 더한다. 버전은 meta 의 control_schema_version 이다.
- 신원(provider, provider_sub)은 계정 하나에 묶인다. 공급자가 다르면 계정도 다르다(이메일로 합치지 않는다).
- 세션 쿠키 값은 256비트 난수이고 표에는 sha256 만 둔다. 비활동 30일 또는 생성 90일이 지나면 끝난다.
- 로그인 상태(state, PKCE verifier)는 10분 안에 한 번만 쓸 수 있다.
세션·로그인 상태의 시각은 이 모듈이 UTC ISO 문자열로 넣고 비교한다(now 인자로 테스트한다).
"""
import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kb.db import NOW, get_meta, open_db, tx

CONTROL_SCHEMA_VERSION = "2"
PROVIDERS = ("github", "google")
SESSION_IDLE = timedelta(days=30)
SESSION_MAX = timedelta(days=90)
SESSION_TOUCH = timedelta(hours=1)  # 마지막 사용 시각은 이만큼 지났을 때만 고친다(요청마다 쓰지 않는다)
LOGIN_STATE_TTL = timedelta(minutes=10)

DDL = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT ({NOW}),
    deleted_at TEXT,
    display_name TEXT NOT NULL DEFAULT '' CHECK (length(display_name) <= 100)
);

CREATE TABLE IF NOT EXISTS identity (
    provider TEXT NOT NULL CHECK (provider IN ('github', 'google')),
    provider_sub TEXT NOT NULL CHECK (length(provider_sub) BETWEEN 1 AND 255),
    account_id INTEGER NOT NULL REFERENCES account(id),
    email TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT ({NOW}),
    PRIMARY KEY (provider, provider_sub)
);
CREATE INDEX IF NOT EXISTS identity_account ON identity(account_id);

CREATE TABLE IF NOT EXISTS session (
    id_hash TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES account(id),
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    ua TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS session_account ON session(account_id);

CREATE TABLE IF NOT EXISTS login_state (
    state TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    verifier TEXT NOT NULL,
    next TEXT NOT NULL,
    created_at TEXT NOT NULL
);

INSERT INTO meta(key, value) VALUES ('control_schema_version', '{CONTROL_SCHEMA_VERSION}')
ON CONFLICT(key) DO UPDATE SET value = excluded.value;
"""


class LoginRefused(Exception):
    """허용 목록(allowed_logins)에 없는 신원이거나 탈퇴한 계정이다."""


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


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_account(conn: sqlite3.Connection, display_name: str = "") -> int:
    with tx(conn):
        return conn.execute("INSERT INTO account(display_name) VALUES (?)", (display_name,)).lastrowid


def get_account(conn: sqlite3.Connection, account_id: int) -> dict | None:
    row = conn.execute("SELECT id, created_at, deleted_at, display_name FROM account WHERE id = ?",
                       (account_id,)).fetchone()
    return None if row is None else dict(row)


def account_providers(conn: sqlite3.Connection, account_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT provider FROM identity WHERE account_id = ? ORDER BY provider", (account_id,))]


def login_identity(conn: sqlite3.Connection, provider: str, sub: str, display_name: str = "",
                   allowed: frozenset[str] | None = None) -> tuple[int, bool]:
    """신원의 계정 id 와 새로 만들었는지. allowed 가 비어 있지 않으면 "provider:sub" 가 그 안에 있어야 한다."""
    if allowed and f"{provider}:{sub}" not in allowed:
        raise LoginRefused(f"{provider}:{sub}")
    with tx(conn):
        row = conn.execute("SELECT a.id, a.deleted_at FROM identity i JOIN account a ON a.id = i.account_id "
                           "WHERE i.provider = ? AND i.provider_sub = ?", (provider, sub)).fetchone()
        if row is not None:
            if row["deleted_at"] is not None:
                raise LoginRefused(f"{provider}:{sub}")
            return row["id"], False
        account_id = conn.execute("INSERT INTO account(display_name) VALUES (?)",
                                  (display_name[:100],)).lastrowid
        conn.execute("INSERT INTO identity(provider, provider_sub, account_id) VALUES (?, ?, ?)",
                     (provider, sub, account_id))
        return account_id, True


def create_session(conn: sqlite3.Connection, account_id: int, ua: str = "", now: datetime | None = None) -> str:
    """새 세션의 쿠키 값. 끝난 세션은 이때 지운다."""
    t = _now(now)
    token = secrets.token_urlsafe(32)
    with tx(conn):
        conn.execute("DELETE FROM session WHERE created_at < ? OR last_seen_at < ?",
                     (_iso(t - SESSION_MAX), _iso(t - SESSION_IDLE)))
        conn.execute("INSERT INTO session(id_hash, account_id, created_at, last_seen_at, ua) VALUES (?, ?, ?, ?, ?)",
                     (_hash(token), account_id, _iso(t), _iso(t), ua[:200]))
    return token


def session_account(conn: sqlite3.Connection, token: str, now: datetime | None = None) -> int | None:
    """살아 있는 세션의 계정 id. 없거나 끝났거나 계정이 탈퇴했으면 None."""
    if not token:
        return None
    t = _now(now)
    row = conn.execute("SELECT s.account_id, s.created_at, s.last_seen_at FROM session s "
                       "JOIN account a ON a.id = s.account_id WHERE s.id_hash = ? AND a.deleted_at IS NULL",
                       (_hash(token),)).fetchone()
    if row is None or row["created_at"] < _iso(t - SESSION_MAX) or row["last_seen_at"] < _iso(t - SESSION_IDLE):
        return None
    if row["last_seen_at"] < _iso(t - SESSION_TOUCH):
        with tx(conn):
            conn.execute("UPDATE session SET last_seen_at = ? WHERE id_hash = ?", (_iso(t), _hash(token)))
    return row["account_id"]


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    with tx(conn):
        conn.execute("DELETE FROM session WHERE id_hash = ?", (_hash(token),))


def put_login_state(conn: sqlite3.Connection, provider: str, verifier: str, next_path: str,
                    now: datetime | None = None) -> str:
    t = _now(now)
    state = secrets.token_urlsafe(32)
    with tx(conn):
        conn.execute("DELETE FROM login_state WHERE created_at < ?", (_iso(t - LOGIN_STATE_TTL),))
        conn.execute("INSERT INTO login_state(state, provider, verifier, next, created_at) VALUES (?, ?, ?, ?, ?)",
                     (state, provider, verifier, next_path, _iso(t)))
    return state


def take_login_state(conn: sqlite3.Connection, state: str, provider: str, now: datetime | None = None) -> dict | None:
    """state 를 지우고 그 내용을 돌려준다. 공급자가 다르거나 10분이 지났으면 None."""
    t = _now(now)
    with tx(conn):
        row = conn.execute("SELECT provider, verifier, next, created_at FROM login_state WHERE state = ?",
                           (state,)).fetchone()
        conn.execute("DELETE FROM login_state WHERE state = ?", (state,))
    if row is None or row["provider"] != provider or row["created_at"] < _iso(t - LOGIN_STATE_TTL):
        return None
    return {"verifier": row["verifier"], "next": row["next"]}
