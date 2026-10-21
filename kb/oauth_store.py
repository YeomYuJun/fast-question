"""원격 MCP OAuth 의 제어 DB 표(kb/control.py OAUTH_DDL) 접근. web/oauth_as.py 의 KbAuthProvider 가 쓴다.

- 클라이언트: 동적 등록 결과(OAuthClientInformationFull)의 JSON 과 토큰을 마지막으로 받은 시각.
  토큰을 1년간 받지 않은 클라이언트와, 토큰을 한 번도 받지 않고 하루가 지난 클라이언트(로그인 없이 등록할 수 있다)는
  delete_idle_clients 가 지운다(대기 요청·코드·토큰도 함께 지워진다).
- 계정당 연결: 그 계정의 refresh 토큰이 살아 있는 클라이언트는 CLIENT_MAX 개까지다. 넘으면 exchange_code 가
  refresh 를 가장 오래전에 받은 연결의 그 계정 토큰을 지운다(새 연결을 거부하지 않는다. claude.ai·Claude Code 는
  다시 연결할 때마다 클라이언트를 새로 등록한다).
- 대기 인가 요청: /authorize 의 매개변수를 10분 둔다. 동의 화면에서 승인·거부할 때 꺼내 지운다(1회).
- 인가 코드: 160비트 난수, 10분, 1회. 쓴 코드는 끝날 때까지 grant_id 를 달고 남아, 다시 오면 그 코드로 낸 토큰을 모두 지운다.
- 토큰: access 1시간, refresh 30일. 한 번의 승인에서 나온 토큰은 grant_id 를 같이 쓴다. refresh 를 쓰면 같은 grant 의
  토큰을 지우고 새 쌍을 낸다(옛 refresh 는 더 받지 않는다). 폐기도 grant 단위다.
- 탈퇴한 계정의 코드·토큰은 찾지 못한 것으로 본다.
- 연결 패널의 "연결된 앱"(list_connections, disconnect): 계정의 살아 있는 refresh 토큰이 있는 클라이언트가 한 연결이다.
  연결한 날은 클라이언트 등록 시각(다시 연결할 때마다 새로 등록된다), 마지막 사용은 그 계정·클라이언트 토큰을 마지막으로
  낸 시각이다(access 가 1시간이라 그 단위로 맞다. 요청마다 쓰지 않으려고 따로 기록하지 않는다).
  끊기는 그 계정·클라이언트의 토큰과 아직 교환하지 않은 코드만 지우고 클라이언트 행은 둔다.
코드·토큰은 sha256 만 둔다. 시각은 epoch 초(SDK 모델과 같다)이고 now 인자로 테스트한다.
"""
import hashlib
import json
import secrets
import sqlite3
import time
from datetime import datetime, timezone

from kb.db import tx

PENDING_TTL = 600
CODE_TTL = 600
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CLIENT_IDLE = 365 * 86400
UNUSED_CLIENT_TTL = 86400
CLIENT_MAX = 20


def _now(now: float | None) -> int:
    return int(time.time() if now is None else now)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def put_client(conn: sqlite3.Connection, client_id: str, info_json: str, now: float | None = None) -> None:
    with tx(conn):
        conn.execute("INSERT INTO oauth_client(client_id, info_json, created_at) VALUES (?, ?, ?)",
                     (client_id, info_json, _now(now)))


def get_client(conn: sqlite3.Connection, client_id: str) -> str | None:
    row = conn.execute("SELECT info_json FROM oauth_client WHERE client_id = ?", (client_id,)).fetchone()
    return None if row is None else row[0]


def delete_idle_clients(conn: sqlite3.Connection, now: float | None = None) -> int:
    """토큰을 1년간 받지 않은 클라이언트와 토큰을 받은 적 없이 등록한 지 하루가 지난 클라이언트를 지우고 그 수를 돌려준다."""
    t = _now(now)
    with tx(conn):
        return conn.execute("DELETE FROM oauth_client WHERE coalesce(last_token_at, created_at) < ? "
                            "OR (last_token_at IS NULL AND created_at < ?)",
                            (t - CLIENT_IDLE, t - UNUSED_CLIENT_TTL)).rowcount


def put_pending(conn: sqlite3.Connection, client_id: str, params: dict, now: float | None = None) -> str:
    t = _now(now)
    pending_id = secrets.token_urlsafe(32)
    with tx(conn):
        conn.execute("DELETE FROM pending_auth WHERE created_at < ?", (t - PENDING_TTL,))
        conn.execute("INSERT INTO pending_auth(id, client_id, params_json, created_at) VALUES (?, ?, ?, ?)",
                     (pending_id, client_id, json.dumps(params, ensure_ascii=False), t))
    return pending_id


def _pending(row, now: float | None) -> dict | None:
    if row is None or row["created_at"] < _now(now) - PENDING_TTL:
        return None
    return {"client_id": row["client_id"], "params": json.loads(row["params_json"])}


def get_pending(conn: sqlite3.Connection, pending_id: str, now: float | None = None) -> dict | None:
    """{client_id, params}. 없거나 10분이 지났으면 None."""
    return _pending(conn.execute("SELECT client_id, params_json, created_at FROM pending_auth WHERE id = ?",
                                 (pending_id,)).fetchone(), now)


def take_pending(conn: sqlite3.Connection, pending_id: str, now: float | None = None) -> dict | None:
    """get_pending 과 같고, 행을 지운다."""
    with tx(conn):
        row = conn.execute("SELECT client_id, params_json, created_at FROM pending_auth WHERE id = ?",
                           (pending_id,)).fetchone()
        conn.execute("DELETE FROM pending_auth WHERE id = ?", (pending_id,))
    return _pending(row, now)


def create_code(conn: sqlite3.Connection, client_id: str, account_id: int, params: dict,
                now: float | None = None) -> str:
    t = _now(now)
    code = secrets.token_urlsafe(20)
    with tx(conn):
        conn.execute("DELETE FROM auth_code WHERE expires_at < ?", (t,))
        conn.execute("INSERT INTO auth_code(hash, client_id, account_id, params_json, expires_at) VALUES (?, ?, ?, ?, ?)",
                     (_hash(code), client_id, account_id, json.dumps(params, ensure_ascii=False), t + CODE_TTL))
    return code


def load_code(conn: sqlite3.Connection, code: str) -> dict | None:
    """{client_id, account_id, params, expires_at}. 끝났는지는 부르는 쪽(SDK)이 expires_at 으로 본다.
    이미 쓴 코드면 그 코드로 낸 토큰을 지우고 None."""
    with tx(conn):
        row = conn.execute("SELECT c.client_id, c.account_id, c.params_json, c.expires_at, c.grant_id "
                           "FROM auth_code c JOIN account a ON a.id = c.account_id "
                           "WHERE c.hash = ? AND a.deleted_at IS NULL", (_hash(code),)).fetchone()
        if row is not None and row["grant_id"] is not None:
            conn.execute("DELETE FROM oauth_token WHERE grant_id = ?", (row["grant_id"],))
            return None
    if row is None:
        return None
    return {"client_id": row["client_id"], "account_id": row["account_id"],
            "params": json.loads(row["params_json"]), "expires_at": row["expires_at"]}


def _issue(conn: sqlite3.Connection, grant_id: str, client_id: str, account_id: int, scopes: list[str],
           resource: str | None, t: int) -> dict:
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    conn.execute("DELETE FROM oauth_token WHERE expires_at < ?", (t,))
    for kind, token, ttl in (("access", access, ACCESS_TTL), ("refresh", refresh, REFRESH_TTL)):
        conn.execute("INSERT INTO oauth_token(hash, kind, grant_id, client_id, account_id, scopes, resource, "
                     "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (_hash(token), kind, grant_id, client_id, account_id, " ".join(scopes), resource, t, t + ttl))
    conn.execute("UPDATE oauth_client SET last_token_at = ? WHERE client_id = ?", (t, client_id))
    return {"access_token": access, "refresh_token": refresh, "expires_in": ACCESS_TTL, "scopes": scopes}


def exchange_code(conn: sqlite3.Connection, code: str, now: float | None = None) -> dict | None:
    """코드를 쓴 것으로 표시하고 access·refresh 를 낸다. 이미 썼거나 끝났으면 None(동시에 두 번 와도 하나만 된다)."""
    t = _now(now)
    grant_id = secrets.token_urlsafe(16)
    with tx(conn):
        cur = conn.execute("UPDATE auth_code SET grant_id = ? WHERE hash = ? AND grant_id IS NULL AND expires_at >= ?",
                           (grant_id, _hash(code), t))
        if cur.rowcount != 1:
            return None
        row = conn.execute("SELECT client_id, account_id, params_json FROM auth_code WHERE hash = ?",
                           (_hash(code),)).fetchone()
        params = json.loads(row["params_json"])
        issued = _issue(conn, grant_id, row["client_id"], row["account_id"], params["scopes"], params["resource"], t)
        _drop_old_connections(conn, row["account_id"], row["client_id"])
        return issued


def _drop_old_connections(conn: sqlite3.Connection, account_id: int, keep: str) -> None:
    """계정의 연결이 CLIENT_MAX 를 넘으면 refresh 를 가장 오래전에 받은 연결부터 그 계정의 토큰을 지운다(keep 은 남긴다)."""
    old = [r[0] for r in conn.execute(
        "SELECT client_id FROM oauth_token WHERE account_id = ? AND kind = 'refresh' AND client_id != ? "
        "GROUP BY client_id ORDER BY max(created_at) DESC LIMIT -1 OFFSET ?", (account_id, keep, CLIENT_MAX - 1))]
    conn.executemany("DELETE FROM oauth_token WHERE account_id = ? AND client_id = ?", [(account_id, c) for c in old])


def load_token(conn: sqlite3.Connection, token: str, kind: str, now: float | None = None) -> dict | None:
    """살아 있는 토큰의 {client_id, account_id, scopes, resource, expires_at}."""
    row = conn.execute("SELECT t.client_id, t.account_id, t.scopes, t.resource, t.expires_at FROM oauth_token t "
                       "JOIN account a ON a.id = t.account_id WHERE t.hash = ? AND t.kind = ? "
                       "AND a.deleted_at IS NULL", (_hash(token), kind)).fetchone()
    if row is None or row["expires_at"] <= _now(now):
        return None
    return {"client_id": row["client_id"], "account_id": row["account_id"], "scopes": row["scopes"].split(),
            "resource": row["resource"], "expires_at": row["expires_at"]}


def rotate_refresh(conn: sqlite3.Connection, refresh: str, scopes: list[str], now: float | None = None) -> dict | None:
    """refresh 를 쓰고 같은 grant 의 새 access·refresh 를 낸다. 옛 refresh 와 access 는 지운다. 없거나 끝났으면 None."""
    t = _now(now)
    with tx(conn):
        row = conn.execute("SELECT t.grant_id, t.client_id, t.account_id, t.resource FROM oauth_token t "
                           "JOIN account a ON a.id = t.account_id WHERE t.hash = ? AND t.kind = 'refresh' "
                           "AND t.expires_at > ? AND a.deleted_at IS NULL", (_hash(refresh), t)).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM oauth_token WHERE grant_id = ?", (row["grant_id"],))
        return _issue(conn, row["grant_id"], row["client_id"], row["account_id"], scopes, row["resource"], t)


def revoke(conn: sqlite3.Connection, token: str) -> None:
    """토큰(access 든 refresh 든)과 같은 grant 의 토큰을 모두 지운다. 없으면 아무것도 하지 않는다."""
    with tx(conn):
        conn.execute("DELETE FROM oauth_token WHERE grant_id IN (SELECT grant_id FROM oauth_token WHERE hash = ?)",
                     (_hash(token),))


def _iso(t: int) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_connections(conn: sqlite3.Connection, account_id: int, now: float | None = None) -> list[dict]:
    """계정의 연결 [{client_id, name, connected_at, last_used_at}], 마지막 사용이 최근인 것부터. name 은 등록 때 받은
    client_name(없으면 빈 문자열)이다. 시각은 UTC ISO 문자열."""
    rows = conn.execute(
        "SELECT c.client_id, c.info_json, c.created_at, max(t.created_at) AS last_at FROM oauth_client c "
        "JOIN oauth_token t ON t.client_id = c.client_id AND t.account_id = ? "
        "WHERE EXISTS (SELECT 1 FROM oauth_token r WHERE r.client_id = c.client_id AND r.account_id = ? "
        "AND r.kind = 'refresh' AND r.expires_at > ?) "
        "GROUP BY c.client_id ORDER BY last_at DESC, c.created_at DESC",
        (account_id, account_id, _now(now))).fetchall()
    out = []
    for r in rows:
        try:
            name = json.loads(r["info_json"]).get("client_name") or ""
        except (ValueError, AttributeError):
            name = ""
        out.append({"client_id": r["client_id"], "name": str(name), "connected_at": _iso(r["created_at"]),
                    "last_used_at": _iso(r["last_at"])})
    return out


def disconnect(conn: sqlite3.Connection, account_id: int, client_id: str) -> bool:
    """그 계정·클라이언트의 토큰과 아직 교환하지 않은 인가 코드를 지운다. 지운 것이 없으면 False."""
    with tx(conn):
        n = conn.execute("DELETE FROM oauth_token WHERE account_id = ? AND client_id = ?",
                         (account_id, client_id)).rowcount
        n += conn.execute("DELETE FROM auth_code WHERE account_id = ? AND client_id = ? AND grant_id IS NULL",
                          (account_id, client_id)).rowcount
    return n > 0
