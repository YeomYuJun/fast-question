"""사람 손 없이 도는 주기 작업: 백업, 근거 재검증, 형태소 재색인. web 프로세스 안에서 돈다(쓰기 주체 system).

마지막 실행 시각은 meta(last_backup, last_recheck)에 DB 시각으로 남는다. web 이 꺼져 있던 동안 밀린 작업은
기동 직후 한 번 돈다. 작업 하나가 실패해도 나머지는 돈다.
"""
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from kb.config import Config
from kb.db import NOW, connect, get_meta
from kb.morph import pending_count, reindex
from kb.store import Store

log = logging.getLogger("fast-question.jobs")
BACKUP_EVERY_HOURS = 24


def backup(conn: sqlite3.Connection, backup_dir: Path, keep: int, today: date | None = None) -> Path:
    """SQLite 온라인 백업 API 로 kb-YYYYMMDD.sqlite3 를 만들고 quick_check 를 돌린다. 최신 keep 개만 남긴다."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / f"kb-{(today or date.today()):%Y%m%d}.sqlite3"
    tmp = dst.with_name(dst.name + ".tmp")
    target = sqlite3.connect(tmp)
    try:
        conn.backup(target)
        target.execute("PRAGMA journal_mode = DELETE")  # 원본이 WAL 이라도 사본은 파일 하나로 둔다
        check = target.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        target.close()
    if check != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"백업 사본의 quick_check 가 실패했다: {check}")
    tmp.replace(dst)
    for old in sorted(backup_dir.glob("kb-*.sqlite3"))[:-keep]:
        old.unlink()
    conn.execute(f"INSERT INTO meta(key, value) VALUES ('last_backup', {NOW}) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    return dst


def _due(conn: sqlite3.Connection, key: str, hours: float, now: datetime) -> bool:
    last = get_meta(conn, key)
    if not last:
        return True
    at = datetime.fromisoformat(last.replace("Z", "+00:00"))
    return now - at >= timedelta(hours=hours)


def run_due(cfg: Config, analyzer=None, now: datetime | None = None) -> dict:
    """기한이 된 작업만 돌리고 결과를 돌려준다. 돌린 것이 없으면 빈 dict."""
    now = now or datetime.now(timezone.utc)
    done: dict = {}
    conn = connect(cfg.db_path)
    try:
        jobs = []
        if _due(conn, "last_backup", BACKUP_EVERY_HOURS, now):
            jobs.append(("backup", lambda: {"path": str(backup(conn, cfg.backup_dir, cfg.backup_keep))}))
        if _due(conn, "last_recheck", cfg.recheck_interval_hours, now):
            def recheck() -> dict:
                results = Store(conn, "system", analyzer).recheck()
                return {"checked": len(results), "changed": sum(r["changed"] for r in results)}
            jobs.append(("recheck", recheck))
        if analyzer is not None and analyzer.enabled and pending_count(conn):
            jobs.append(("reindex", lambda: reindex(conn, analyzer) if analyzer.ready(conn) else 0))
        for name, job in jobs:
            try:
                done[name] = job()
                log.info("%s: %s", name, done[name])
            except Exception as e:  # 백업이 실패해도 재검증은 돈다
                log.exception("%s 실패", name)
                done[name] = {"error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()
    return done
