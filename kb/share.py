"""공개 링크(원격 모드). 계정이 자기 항목(qa) 하나 또는 분류(category) 하나를 로그인 없이 읽는 주소 /s/<토큰> 으로 연다.

- 제어 DB share 표(kb/control.py SHARE_DDL). 토큰은 24바이트 난수(urlsafe)이고 sha256 만 둔다. 만들 때 한 번만 토큰을 돌려준다.
- 읽는 쪽(web/share.py)은 토큰으로 계정·대상을 찾아 그 계정 DB 에서 질문·답변·분류 경로만 읽는다. 근거·이력·번호 이외의
  내부 정보는 내보내지 않는다. 보관한 항목·삭제한 분류·탈퇴한 계정은 없는 것으로 본다.
- 폐기(revoke)하면 그 주소는 바로 404 다. 탈퇴하면 모두 폐기된다(control.delete_account).
- 계정당 살아 있는 링크는 SHARE_MAX 개까지다.
"""
import hashlib
import secrets
import sqlite3
from datetime import datetime, timezone

from kb.db import tx

KINDS = ("qa", "category")
SHARE_MAX = 200
TOKEN_BYTES = 24


class ShareRefused(Exception):
    pass


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "kind": r["kind"], "target_id": r["target_id"], "title": r["title"],
            "created_at": r["created_at"]}


def create(conn: sqlite3.Connection, account_id: int, kind: str, target_id: int, title: str = "",
           now: datetime | None = None) -> dict:
    """새 링크. 반환값의 token 은 이때 한 번만 알 수 있다. 같은 대상의 살아 있는 링크가 있으면 새로 만들지 않고 거부한다."""
    if kind not in KINDS:
        raise ShareRefused(f"공개 링크 종류는 {', '.join(KINDS)} 중 하나입니다: {kind}")
    if type(target_id) is not int or target_id <= 0:
        raise ShareRefused("대상 번호가 올바르지 않습니다.")
    title = " ".join(str(title or "").split())[:150]
    token = secrets.token_urlsafe(TOKEN_BYTES)
    with tx(conn):
        dup = conn.execute("SELECT id FROM share WHERE account_id = ? AND kind = ? AND target_id = ? AND revoked_at IS NULL",
                           (account_id, kind, target_id)).fetchone()
        if dup is not None:
            raise ShareRefused("이 대상의 공개 링크가 이미 있습니다. 연결 화면의 공개 링크 목록에서 폐기한 뒤 다시 만들어 주세요.")
        alive = conn.execute("SELECT count(*) FROM share WHERE account_id = ? AND revoked_at IS NULL",
                             (account_id,)).fetchone()[0]
        if alive >= SHARE_MAX:
            raise ShareRefused(f"공개 링크는 계정당 {SHARE_MAX}개까지입니다. 쓰지 않는 링크를 폐기해 주세요.")
        cur = conn.execute("INSERT INTO share(token_hash, account_id, kind, target_id, title, created_at) VALUES (?,?,?,?,?,?)",
                           (_hash(token), account_id, kind, target_id, title, _now(now)))
    return {"id": cur.lastrowid, "kind": kind, "target_id": target_id, "title": title, "token": token}


def list_shares(conn: sqlite3.Connection, account_id: int) -> list[dict]:
    rows = conn.execute("SELECT id, kind, target_id, title, created_at FROM share WHERE account_id = ? AND revoked_at IS NULL "
                        "ORDER BY id DESC", (account_id,)).fetchall()
    return [_row(r) for r in rows]


def revoke(conn: sqlite3.Connection, account_id: int, share_id: int, now: datetime | None = None) -> bool:
    with tx(conn):
        cur = conn.execute("UPDATE share SET revoked_at = ? WHERE id = ? AND account_id = ? AND revoked_at IS NULL",
                           (_now(now), share_id, account_id))
    return cur.rowcount > 0


def resolve(conn: sqlite3.Connection, token: str) -> dict | None:
    """토큰의 살아 있는 링크와 계정. 탈퇴한 계정이면 None."""
    if not token or len(token) > 128:
        return None
    r = conn.execute("SELECT s.id, s.kind, s.target_id, s.title, s.created_at, s.account_id FROM share s "
                     "JOIN account a ON a.id = s.account_id AND a.deleted_at IS NULL "
                     "WHERE s.token_hash = ? AND s.revoked_at IS NULL", (_hash(token),)).fetchone()
    return None if r is None else {**_row(r), "account_id": r["account_id"]}


def read_target(kb: sqlite3.Connection, kind: str, target_id: int, limit: int = 500) -> dict | None:
    """계정 DB 에서 공개할 내용만 읽는다. 대상이 없거나 보관·삭제됐으면 None."""
    if kind == "qa":
        r = kb.execute("SELECT q.question, q.answer, q.updated_at, c.name, p.name AS top FROM qa q JOIN category c ON c.id = q.category_id "
                       "LEFT JOIN category p ON p.id = c.parent_id WHERE q.id = ? AND q.deleted_at IS NULL", (target_id,)).fetchone()
        if r is None:
            return None
        path = f"{r['top']} > {r['name']}" if r["top"] else r["name"]
        return {"kind": "qa", "title": r["question"], "path": path,
                "items": [{"question": r["question"], "answer": r["answer"], "path": path, "updated_at": r["updated_at"]}]}
    c = kb.execute("SELECT c.id, c.name, c.description, p.name AS top FROM category c LEFT JOIN category p ON p.id = c.parent_id "
                   "WHERE c.id = ? AND c.deleted_at IS NULL", (target_id,)).fetchone()
    if c is None:
        return None
    path = f"{c['top']} > {c['name']}" if c["top"] else c["name"]
    rows = kb.execute(
        "SELECT q.question, q.answer, q.updated_at, c2.name, p2.name AS top FROM qa q JOIN category c2 ON c2.id = q.category_id "
        "LEFT JOIN category p2 ON p2.id = c2.parent_id WHERE q.deleted_at IS NULL AND c2.deleted_at IS NULL "
        "AND (q.category_id = ? OR c2.parent_id = ?) ORDER BY c2.parent_id IS NOT NULL, c2.name, q.id LIMIT ?",
        (target_id, target_id, limit)).fetchall()
    return {"kind": "category", "title": path, "path": path, "description": c["description"] or "",
            "items": [{"question": r["question"], "answer": r["answer"], "updated_at": r["updated_at"],
                       "path": f"{r['top']} > {r['name']}" if r["top"] else r["name"]} for r in rows]}
