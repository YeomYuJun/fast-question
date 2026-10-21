"""원격 모드의 제어 DB(data_dir/control.sqlite3): 계정, 로그인 신원, 브라우저 세션, 진행 중인 로그인.
사용자 KB 와 다른 파일이고 KB 스키마를 깔지 않는다.

표는 기능별 DDL 상수(DDL_PARTS)에 CREATE TABLE IF NOT EXISTS 로 더한다. 버전(meta 의 control_schema_version)은
DDL 전체의 sha256 이라 손으로 올리지 않는다. DDL 이 바뀐 뒤 처음 연결할 때 전체 DDL 을 다시 실행한다.
- 신원(provider, provider_sub)은 계정 하나에 묶인다. 공급자가 다르면 계정도 다르다(이메일로 합치지 않는다).
- 세션 쿠키 값은 256비트 난수이고 표에는 sha256 만 둔다. 비활동 30일 또는 생성 90일이 지나면 끝난다.
- 로그인 상태(state, PKCE verifier)는 10분 안에 한 번만 쓸 수 있다.
- 개인 토큰(fq_pat_…)은 훅·로컬 프록시가 Bearer 로 쓴다. 표에는 sha256 만 두고, 180일 뒤 끝난다. 계정당 살아 있는 토큰 10개.
- PC 로그인 승인 코드(cli_grant)는 5분 안에 한 번만 개인 토큰으로 바꿀 수 있다(web/cli_login.py).
- 웹 Claude 패널의 Anthropic API 키(claude_key)는 계정당 하나이고 암호화한 값만 둔다(web/claude_chat.py).
- 형태소 사용자 단어는 원격 모드에서 서버 전역 사전(global_user_word) 하나다. Kiwi 가 프로세스에 하나라 사용자마다
  둘 수 없다. 운영자가 scripts/global_words.py 로 바꾸고, 분석기는 words_source() 로 이 표를 읽는다.
세션·로그인 상태의 시각은 이 모듈이 UTC ISO 문자열로 넣고 비교한다(now 인자로 테스트한다).
"""
import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kb.db import NOW, get_meta, open_db, tx
from kb.remote import TOKEN_PREFIX

PROVIDERS = ("github", "google")
SESSION_IDLE = timedelta(days=30)
SESSION_MAX = timedelta(days=90)
SESSION_TOUCH = timedelta(hours=1)  # 마지막 사용 시각은 이만큼 지났을 때만 고친다(요청마다 쓰지 않는다)
LOGIN_STATE_TTL = timedelta(minutes=10)
CLI_GRANT_TTL = timedelta(minutes=5)
TOKEN_LIFETIME = timedelta(days=180)
TOKEN_MAX = 10
SIGNUP_PER_HOUR = 5

ACCOUNT_DDL = f"""
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
"""

SESSION_DDL = """
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
"""

TOKEN_DDL = """
CREATE TABLE IF NOT EXISTS personal_token (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES account(id),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
    hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS personal_token_account ON personal_token(account_id);
"""

# 원격 MCP OAuth(접근 함수는 kb/oauth_store.py). 시각은 SDK 모델과 같은 epoch 초다.
# 코드·토큰은 sha256 만 둔다. 클라이언트 비밀값은 SDK 가 평문으로 비교해 info_json 에 그대로 있다.
OAUTH_DDL = """
CREATE TABLE IF NOT EXISTS oauth_client (
    client_id TEXT PRIMARY KEY,
    info_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_token_at INTEGER
);

CREATE TABLE IF NOT EXISTS pending_auth (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES oauth_client(client_id) ON DELETE CASCADE,
    params_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_code (
    hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES oauth_client(client_id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES account(id),
    params_json TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    grant_id TEXT
);

CREATE TABLE IF NOT EXISTS oauth_token (
    hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    grant_id TEXT NOT NULL,
    client_id TEXT NOT NULL REFERENCES oauth_client(client_id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES account(id),
    scopes TEXT NOT NULL,
    resource TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS oauth_token_grant ON oauth_token(grant_id);
"""

# 계정을 새로 만든 IP 와 시각(IP 당 가입 수 제한). 하루 지난 행은 가입 때 지운다
SIGNUP_DDL = """
CREATE TABLE IF NOT EXISTS signup (ip TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS signup_ip_at ON signup(ip, at);
"""

# 서버 전역 형태소 사용자 단어(원격 모드, scripts/global_words.py)
GLOBAL_WORD_TAGS = ("NNP", "NNG", "SL", "XR", "VV", "VA")  # 사용자 KB 의 user_word 와 같은 품사
GLOBAL_WORD_DDL = f"""
CREATE TABLE IF NOT EXISTS global_user_word (
    word TEXT PRIMARY KEY CHECK (length(word) BETWEEN 1 AND 40 AND instr(word, ' ') = 0),
    tag TEXT NOT NULL CHECK (tag IN ({", ".join(f"'{t}'" for t in GLOBAL_WORD_TAGS)})),
    created_at TEXT NOT NULL DEFAULT ({NOW})
);
"""

# 로그인 수단 추가: 이 state 의 로그인이 끝나면 새 세션 대신 이 계정에 신원을 붙인다(login_state 와 같이 10분 1회)
LINK_DDL = """
CREATE TABLE IF NOT EXISTS link_state (
    state TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES account(id)
);
"""

# PC 클라이언트 로그인(web/cli_login.py): 브라우저에서 승인하면 1회용 코드를 PC 의 127.0.0.1 로 넘기고, PC 가 그 코드와
# PKCE verifier 로 개인 토큰을 받는다. 코드는 sha256 만 두고 CLI_GRANT_TTL 안에 한 번만 쓴다
CLI_GRANT_DDL = """
CREATE TABLE IF NOT EXISTS cli_grant (
    code_hash TEXT PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES account(id),
    challenge TEXT NOT NULL,
    device TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# 웹 화면 Claude 패널(web/claude_chat.py): 계정이 등록한 Anthropic API 키. 값은 서버 키 파일로 암호화한 것만 둔다
CLAUDE_KEY_DDL = """
CREATE TABLE IF NOT EXISTS claude_key (
    account_id INTEGER PRIMARY KEY REFERENCES account(id),
    secret TEXT NOT NULL,
    last4 TEXT NOT NULL,
    model TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# 기능을 더하면 그 기능의 DDL 상수를 만들어 끝에 붙인다
DDL_PARTS = (ACCOUNT_DDL, SESSION_DDL, TOKEN_DDL, OAUTH_DDL, SIGNUP_DDL, GLOBAL_WORD_DDL, LINK_DDL, CLI_GRANT_DDL,
             CLAUDE_KEY_DDL)
DDL = "".join(DDL_PARTS)
CONTROL_SCHEMA_VERSION = hashlib.sha256(DDL.encode("utf-8")).hexdigest()[:16]


class LoginRefused(Exception):
    """허용 목록(allowed_logins)에 없는 신원이거나 탈퇴한 계정이다."""


class SignupLimited(LoginRefused):
    """같은 IP 에서 한 시간 안에 SIGNUP_PER_HOUR 개 넘게 계정을 만들려 했다."""


class LinkRefused(Exception):
    """계정에 이미 같은 공급자의 다른 신원이 있다(공급자마다 하나)."""


class TokenRefused(ValueError):
    """개인 토큰을 만들 수 없다(이름이 비었거나 길다, 살아 있는 토큰이 이미 TOKEN_MAX 개)."""


def _version(conn: sqlite3.Connection) -> str | None:
    try:
        return get_meta(conn, "control_schema_version")
    except sqlite3.OperationalError:  # meta 표가 아직 없다
        return None


def connect_control(path: Path | str) -> sqlite3.Connection:
    conn = open_db(path)
    if _version(conn) != CONTROL_SCHEMA_VERSION:
        conn.executescript("BEGIN IMMEDIATE;\n" + DDL + "\nINSERT INTO meta(key, value) VALUES "
                           f"('control_schema_version', '{CONTROL_SCHEMA_VERSION}') "
                           "ON CONFLICT(key) DO UPDATE SET value = excluded.value;\nCOMMIT;")
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
                   allowed: frozenset[str] | None = None, ip: str | None = None,
                   now: datetime | None = None) -> tuple[int, bool]:
    """신원의 계정 id 와 새로 만들었는지. allowed 가 비어 있지 않으면 "provider:sub" 가 그 안에 있어야 한다.
    ip 를 주면 새 계정을 만들 때 그 IP 의 한 시간 가입 수를 SIGNUP_PER_HOUR 로 막는다(이미 있는 계정 로그인은 막지 않는다)."""
    t = _now(now)
    if allowed and f"{provider}:{sub}" not in allowed:
        raise LoginRefused(f"{provider}:{sub}")
    with tx(conn):
        row = conn.execute("SELECT a.id, a.deleted_at FROM identity i JOIN account a ON a.id = i.account_id "
                           "WHERE i.provider = ? AND i.provider_sub = ?", (provider, sub)).fetchone()
        if row is not None:
            if row["deleted_at"] is not None:
                raise LoginRefused(f"{provider}:{sub}")
            return row["id"], False
        if ip is not None:
            conn.execute("DELETE FROM signup WHERE at < ?", (_iso(t - timedelta(days=1)),))
            recent = conn.execute("SELECT count(*) FROM signup WHERE ip = ? AND at >= ?",
                                  (ip, _iso(t - timedelta(hours=1)))).fetchone()[0]
            if recent >= SIGNUP_PER_HOUR:
                raise SignupLimited(ip)
            conn.execute("INSERT INTO signup(ip, at) VALUES (?, ?)", (ip, _iso(t)))
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


def put_link_state(conn: sqlite3.Connection, state: str, account_id: int) -> None:
    """state(put_login_state 가 만든 것)의 로그인을 account_id 에 신원을 붙이는 데 쓴다고 적는다."""
    with tx(conn):
        conn.execute("DELETE FROM link_state WHERE state NOT IN (SELECT state FROM login_state)")
        conn.execute("INSERT INTO link_state(state, account_id) VALUES (?, ?)", (state, account_id))


def take_link_state(conn: sqlite3.Connection, state: str) -> int | None:
    """로그인 수단 추가로 시작한 state 면 그 계정 id 를 돌려주고 지운다. 아니면 None."""
    with tx(conn):
        row = conn.execute("SELECT account_id FROM link_state WHERE state = ?", (state,)).fetchone()
        conn.execute("DELETE FROM link_state WHERE state = ?", (state,))
    return None if row is None else row[0]


def link_identity(conn: sqlite3.Connection, account_id: int, provider: str, sub: str,
                  allowed: frozenset[str] | None = None, now: datetime | None = None) -> int | None:
    """신원을 account_id 에 붙인다. 그 신원이 있던 다른 계정에 남은 신원이 없으면 그 계정을 탈퇴시키고 그 id 를 돌려준다
    (부르는 쪽이 사용자 폴더를 deleted/ 로 옮긴다). 버린 계정이 없으면 None."""
    if allowed and f"{provider}:{sub}" not in allowed:
        raise LoginRefused(f"{provider}:{sub}")
    with tx(conn):
        row = conn.execute("SELECT account_id FROM identity WHERE provider = ? AND provider_sub = ?",
                           (provider, sub)).fetchone()
        if row is not None and row[0] == account_id:
            return None
        if conn.execute("SELECT 1 FROM identity WHERE account_id = ? AND provider = ?",
                        (account_id, provider)).fetchone():
            raise LinkRefused(provider)
        if row is None:
            conn.execute("INSERT INTO identity(provider, provider_sub, account_id) VALUES (?, ?, ?)",
                         (provider, sub, account_id))
            return None
        other = row[0]
        conn.execute("UPDATE identity SET account_id = ? WHERE provider = ? AND provider_sub = ?",
                     (account_id, provider, sub))
        left = conn.execute("SELECT count(*) FROM identity WHERE account_id = ?", (other,)).fetchone()[0]
        closed = conn.execute("SELECT deleted_at FROM account WHERE id = ?", (other,)).fetchone()[0]
    if left or closed:
        return None
    delete_account(conn, other, now)
    return other


def create_personal_token(conn: sqlite3.Connection, account_id: int, name: str,
                          now: datetime | None = None) -> dict:
    """새 토큰. 반환값의 token 은 이때 한 번만 알 수 있다."""
    name = name.strip()
    if not 1 <= len(name) <= 60 or not name.isprintable():
        raise TokenRefused("토큰 이름은 한 줄 1~60자다.")
    t = _now(now)
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with tx(conn):
        alive = conn.execute("SELECT count(*) FROM personal_token WHERE account_id = ? AND revoked_at IS NULL "
                             "AND expires_at > ?", (account_id, _iso(t))).fetchone()[0]
        if alive >= TOKEN_MAX:
            raise TokenRefused(f"살아 있는 토큰이 이미 {TOKEN_MAX}개다. 쓰지 않는 토큰을 폐기한다.")
        token_id = conn.execute(
            "INSERT INTO personal_token(account_id, name, hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (account_id, name, _hash(token), _iso(t), _iso(t + TOKEN_LIFETIME))).lastrowid
    return {"id": token_id, "name": name, "token": token, "created_at": _iso(t), "expires_at": _iso(t + TOKEN_LIFETIME)}


def list_personal_tokens(conn: sqlite3.Connection, account_id: int, now: datetime | None = None) -> list[dict]:
    """폐기하지 않은 토큰(끝난 것 포함, expired 표시). 토큰 값과 해시는 돌려주지 않는다."""
    t = _iso(_now(now))
    return [dict(r, expired=r["expires_at"] <= t) for r in conn.execute(
        "SELECT id, name, created_at, last_used_at, expires_at FROM personal_token "
        "WHERE account_id = ? AND revoked_at IS NULL ORDER BY id", (account_id,))]


def revoke_personal_token(conn: sqlite3.Connection, account_id: int, token_id: int,
                          now: datetime | None = None) -> bool:
    with tx(conn):
        cur = conn.execute("UPDATE personal_token SET revoked_at = ? WHERE id = ? AND account_id = ? "
                           "AND revoked_at IS NULL", (_iso(_now(now)), token_id, account_id))
    return cur.rowcount == 1


def token_account(conn: sqlite3.Connection, token: str, now: datetime | None = None) -> dict | None:
    """살아 있는 개인 토큰의 {account_id, token_id, expires_at}. 없거나 끝났거나 폐기됐거나 계정이 탈퇴했으면 None."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    t = _now(now)
    row = conn.execute("SELECT p.id, p.account_id, p.last_used_at, p.expires_at FROM personal_token p "
                       "JOIN account a ON a.id = p.account_id WHERE p.hash = ? AND p.revoked_at IS NULL "
                       "AND a.deleted_at IS NULL", (_hash(token),)).fetchone()
    if row is None or row["expires_at"] <= _iso(t):
        return None
    if row["last_used_at"] is None or row["last_used_at"] < _iso(t - SESSION_TOUCH):
        with tx(conn):
            conn.execute("UPDATE personal_token SET last_used_at = ? WHERE id = ?", (_iso(t), row["id"]))
    return {"account_id": row["account_id"], "token_id": row["id"], "expires_at": row["expires_at"]}


def delete_account(conn: sqlite3.Connection, account_id: int, now: datetime | None = None) -> None:
    """탈퇴: 계정에 탈퇴 시각을 적고 표시 이름을 지우고 세션·개인 토큰·OAuth 토큰·인가 코드·PC 로그인 승인 코드·
    Anthropic API 키를 모두 지운다. 신원 행은 사용자 폴더와 같이 30일 남아(purge_closed_accounts) 그동안 다시 로그인하면 거부된다."""
    t = _iso(_now(now))
    with tx(conn):
        conn.execute("UPDATE account SET deleted_at = ?, display_name = '' WHERE id = ? AND deleted_at IS NULL",
                     (t, account_id))
        conn.execute("DELETE FROM session WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM personal_token WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM oauth_token WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM auth_code WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM cli_grant WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM claude_key WHERE account_id = ?", (account_id,))


def put_claude_key(conn: sqlite3.Connection, account_id: int, secret: str, last4: str, model: str,
                   now: datetime | None = None) -> None:
    """웹 Claude 패널의 API 키(암호화한 값)와 고른 모델. 계정당 하나라 덮어쓴다."""
    with tx(conn):
        conn.execute("INSERT INTO claude_key(account_id, secret, last4, model, updated_at) VALUES (?, ?, ?, ?, ?) "
                     "ON CONFLICT(account_id) DO UPDATE SET secret = excluded.secret, last4 = excluded.last4, "
                     "model = excluded.model, updated_at = excluded.updated_at",
                     (account_id, secret, last4, model, _iso(_now(now))))


def get_claude_key(conn: sqlite3.Connection, account_id: int) -> dict | None:
    row = conn.execute("SELECT secret, last4, model, updated_at FROM claude_key WHERE account_id = ?",
                       (account_id,)).fetchone()
    return None if row is None else dict(row)


def set_claude_model(conn: sqlite3.Connection, account_id: int, model: str) -> bool:
    with tx(conn):
        return conn.execute("UPDATE claude_key SET model = ? WHERE account_id = ?", (model, account_id)).rowcount > 0


def delete_claude_key(conn: sqlite3.Connection, account_id: int) -> bool:
    with tx(conn):
        return conn.execute("DELETE FROM claude_key WHERE account_id = ?", (account_id,)).rowcount > 0


def put_cli_grant(conn: sqlite3.Connection, account_id: int, challenge: str, device: str,
                  now: datetime | None = None) -> str:
    """PC 로그인 승인. 1회용 코드를 돌려준다(표에는 sha256 만). 끝난 승인은 이때 지운다."""
    t = _now(now)
    code = secrets.token_urlsafe(32)
    with tx(conn):
        conn.execute("DELETE FROM cli_grant WHERE created_at < ?", (_iso(t - CLI_GRANT_TTL),))
        conn.execute("INSERT INTO cli_grant(code_hash, account_id, challenge, device, created_at) VALUES (?, ?, ?, ?, ?)",
                     (_hash(code), account_id, challenge, device, _iso(t)))
    return code


def take_cli_grant(conn: sqlite3.Connection, code: str, now: datetime | None = None) -> dict | None:
    """코드를 지우고 {account_id, challenge, device} 를 돌려준다. 없거나 CLI_GRANT_TTL 이 지났으면 None."""
    t = _now(now)
    with tx(conn):
        row = conn.execute("SELECT account_id, challenge, device, created_at FROM cli_grant WHERE code_hash = ?",
                           (_hash(code),)).fetchone()
        conn.execute("DELETE FROM cli_grant WHERE code_hash = ?", (_hash(code),))
    if row is None or row["created_at"] < _iso(t - CLI_GRANT_TTL):
        return None
    return {"account_id": row["account_id"], "challenge": row["challenge"], "device": row["device"]}


def purge_closed_accounts(conn: sqlite3.Connection, days: int, now: datetime | None = None) -> int:
    """탈퇴한 지 days 일이 지난 계정의 신원 행을 지운다(그 뒤 같은 공급자 계정은 새 계정으로 가입한다). 지운 계정 수.
    계정 행은 번호·시각만 든 표지로 남긴다. 번호를 다시 쓰면 S3 의 옛 백업 경로(users/<id>/)와 섞인다."""
    cutoff = _iso(_now(now) - timedelta(days=days))
    with tx(conn):
        ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT a.id FROM account a JOIN identity i ON i.account_id = a.id "
            "WHERE a.deleted_at IS NOT NULL AND a.deleted_at < ?", (cutoff,))]
        conn.executemany("DELETE FROM identity WHERE account_id = ?", [(i,) for i in ids])
    return len(ids)


def purge_signups(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """하루 지난 가입 IP 기록을 지운다(가입 제한은 최근 한 시간만 본다). 주기 작업이 하루 한 번 부른다. 지운 행 수."""
    with tx(conn):
        return conn.execute("DELETE FROM signup WHERE at < ?", (_iso(_now(now) - timedelta(days=1)),)).rowcount


def global_words(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """서버 전역 형태소 사용자 단어 (단어, 품사). kb.dictionary.load_user_words 와 같은 순서다."""
    return [(r[0], r[1]) for r in conn.execute("SELECT word, tag FROM global_user_word ORDER BY word, tag")]


def add_global_word(conn: sqlite3.Connection, word: str, tag: str = "NNP") -> None:
    word = (word or "").strip()
    if not 1 <= len(word) <= 40 or any(c.isspace() for c in word):
        raise ValueError("단어는 공백 없는 1~40자다.")
    if tag not in GLOBAL_WORD_TAGS:
        raise ValueError(f"품사는 {'·'.join(GLOBAL_WORD_TAGS)} 중 하나다: {tag}")
    try:
        with tx(conn):
            conn.execute("INSERT INTO global_user_word(word, tag) VALUES (?, ?)", (word, tag))
    except sqlite3.IntegrityError:
        raise ValueError(f"이미 있는 단어다: {word}") from None


def remove_global_word(conn: sqlite3.Connection, word: str) -> bool:
    with tx(conn):
        return conn.execute("DELETE FROM global_user_word WHERE word = ?", (word.strip(),)).rowcount == 1


def words_source(path: Path | str):
    """Analyzer(words=...) 에 넣는 공급원. 사용자 DB 연결은 쓰지 않고 제어 DB 를 매번 연다(단어가 바로 반영된다)."""
    def words(_conn: sqlite3.Connection) -> list[tuple[str, str]]:
        conn = connect_control(path)
        try:
            return global_words(conn)
        finally:
            conn.close()
    return words
