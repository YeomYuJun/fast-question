"""원격 서버의 유입·사용 집계(운영자용, 읽기 전용). 새로 모으는 값은 없고 이미 저장된 값만 센다.

- 가입: 제어 DB account.created_at 의 날짜(UTC).
- 최근 7일 사용 계정: 브라우저 세션의 last_seen_at(웹), 개인 토큰의 last_used_at(PC 프록시·훅).
  OAuth 토큰(claude.ai·한 줄 연결)은 마지막 사용 시각을 두지 않아 세지 못한다. 그 계정의 등록은 아래 등록 건수에 잡힌다.
- 등록: 사용자 DB revision 의 qa insert 를 날짜·주체(user = 화면, claude = MCP)별로 센다. system(기본 가이드)은 뺀다.
사용자 DB 는 읽기 전용 URI 로 열어 스키마를 올리거나 WAL 을 만들지 않는다. 열지 못한 DB 는 unreadable 에 계정 id 만 남긴다.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kb import tenant

ACTIVE_DAYS = 7


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5.0)


def inflow(data_dir: Path, days: int = 14, now: datetime | None = None) -> dict:
    """최근 days 일(오늘 포함, UTC)의 날짜별 가입·등록과 현재 계정 수."""
    now = now or datetime.now(timezone.utc)
    first = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    active_from = (now - timedelta(days=ACTIVE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    table = {(now - timedelta(days=i)).strftime("%Y-%m-%d"): {"signups": 0, "writers": set(), "user": 0, "claude": 0}
             for i in range(days)}
    conn = _read_only(tenant.control_path(data_dir))
    try:
        alive, closed = conn.execute(
            "SELECT count(*) FILTER (WHERE deleted_at IS NULL), count(*) FILTER (WHERE deleted_at IS NOT NULL) "
            "FROM account").fetchone()
        for day, n in conn.execute(
                "SELECT substr(created_at, 1, 10), count(*) FROM account WHERE substr(created_at, 1, 10) >= ? GROUP BY 1",
                (first,)):
            if day in table:
                table[day]["signups"] = n
        web = conn.execute("SELECT count(DISTINCT account_id) FROM session WHERE last_seen_at >= ?",
                           (active_from,)).fetchone()[0]
        pc = conn.execute("SELECT count(DISTINCT account_id) FROM personal_token "
                          "WHERE revoked_at IS NULL AND last_used_at >= ?", (active_from,)).fetchone()[0]
    finally:
        conn.close()
    items, unreadable = 0, []
    for account_id, path in tenant.list_user_dbs(data_dir):
        try:
            db = _read_only(path)
            try:
                items += db.execute(
                    "SELECT count(*) FROM qa WHERE deleted_at IS NULL AND created_by != 'system'").fetchone()[0]
                rows = db.execute(
                    "SELECT substr(at, 1, 10), actor, count(*) FROM revision WHERE table_name = 'qa' AND op = 'insert' "
                    "AND actor IN ('user', 'claude') AND substr(at, 1, 10) >= ? GROUP BY 1, 2", (first,)).fetchall()
            finally:
                db.close()
        except sqlite3.Error:
            unreadable.append(account_id)
            continue
        for day, actor, n in rows:
            if day in table:
                table[day][actor] += n
                table[day]["writers"].add(account_id)
    return {"accounts": {"alive": alive, "closed": closed},
            "active": {"days": ACTIVE_DAYS, "web": web, "pc": pc},
            "items": items, "unreadable": unreadable,
            "days": [{"day": d, "signups": v["signups"], "writers": len(v["writers"]),
                      "added_user": v["user"], "added_claude": v["claude"]} for d, v in sorted(table.items())]}
