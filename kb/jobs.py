"""사람 손 없이 도는 주기 작업: 백업, 근거 재검증, 형태소 재색인. web 프로세스 안에서 돈다(쓰기 주체 system).

마지막 실행 시각은 meta(last_backup, last_recheck)에 DB 시각으로 남는다. web 이 꺼져 있던 동안 밀린 작업은
기동 직후 한 번 돈다. 작업 하나가 실패해도 나머지는 돈다.
로컬 모드는 run_due 가 cfg.db_path 하나를 맡는다. 원격 모드는 run_due_all 이 사용자 DB(kb/tenant.py)마다
백업(users/<id>/backup/)과 재색인을 하고, 제어 DB 를 data_dir/backup/control-YYYYMMDD.sqlite3 로 백업한다.
원격 모드는 근거 재검증을 하지 않는다(근거 파일은 사용자 PC 에 있다). 한 사용자의 실패가 다른 사용자를 막지 않는다.
backup_s3_bucket 이 있으면 그 주기에 만든 백업 사본을 S3 에 올린다(kb/s3backup.py). 업로드 실패는 백업을 되돌리지 않는다.
"""
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from kb import s3backup, tenant
from kb.config import Config
from kb.control import connect_control
from kb.db import NOW, connect, get_meta
from kb.morph import pending_count, reindex
from kb.store import Store

log = logging.getLogger("fast-question.jobs")
BACKUP_EVERY_HOURS = 24


def backup(conn: sqlite3.Connection, backup_dir: Path, keep: int, today: date | None = None,
           name: str = "kb") -> Path:
    """SQLite 온라인 백업 API 로 <name>-YYYYMMDD.sqlite3 를 만들고 quick_check 를 돌린다. 최신 keep 개만 남긴다."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / f"{name}-{(today or date.today()):%Y%m%d}.sqlite3"
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
    for old in sorted(backup_dir.glob(f"{name}-*.sqlite3"))[:-keep]:
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


def _run(jobs: list, label: str = "") -> dict:
    done: dict = {}
    for name, job in jobs:
        try:
            done[name] = job()
            log.info("%s%s: %s", label, name, done[name])
        except Exception as e:  # 백업이 실패해도 재검증은 돈다
            log.exception("%s%s 실패", label, name)
            done[name] = {"error": f"{type(e).__name__}: {e}"}
    return done


def _reindex_job(conn: sqlite3.Connection, analyzer) -> list:
    if analyzer is not None and analyzer.enabled and pending_count(conn, analyzer):
        return [("reindex", lambda: reindex(conn, analyzer) if analyzer.ready(conn) else 0)]
    return []


def run_due(cfg: Config, analyzer=None, now: datetime | None = None) -> dict:
    """기한이 된 작업만 돌리고 결과를 돌려준다. 돌린 것이 없으면 빈 dict."""
    now = now or datetime.now(timezone.utc)
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
        return _run(jobs + _reindex_job(conn, analyzer))
    finally:
        conn.close()


class _Uploader:
    """S3 클라이언트는 올릴 사본이 처음 생길 때 만든다(boto3 가 없으면 그때 업로드 오류가 된다)."""

    def __init__(self, cfg: Config, s3=None):
        self.bucket, self.prefix, self._s3 = cfg.backup_s3_bucket, cfg.backup_s3_prefix, s3

    def job(self, result: dict, key_of) -> list:
        if not self.bucket or "path" not in result.get("backup", {}):
            return []

        def upload() -> dict:
            if self._s3 is None:
                self._s3 = s3backup.client()
            path = Path(result["backup"]["path"])
            return s3backup.upload(self._s3, self.bucket, key_of(self.prefix, path), path)
        return [("upload", upload)]


def run_due_all(cfg: Config, analyzer=None, now: datetime | None = None, s3=None) -> dict:
    """원격 모드: 사용자 DB 마다 기한이 된 백업·재색인을 돌리고 제어 DB 를 백업한다. s3: 업로드에 쓸 클라이언트
    (없으면 backup_s3_bucket 이 있을 때 boto3 로 만든다).
    결과는 {"users": {계정 id: {작업: 결과}}, "control": {작업: 결과}} 에서 돌린 것만 담는다. 없으면 빈 dict."""
    now = now or datetime.now(timezone.utc)
    uploader = _Uploader(cfg, s3)
    done: dict = {}
    users: dict = {}
    for account_id, path in tenant.list_user_dbs(cfg.data_dir):
        label = f"user {account_id} "
        try:
            conn = connect(path)
        except Exception as e:  # 손상된 파일 하나가 다른 사용자의 작업을 막지 않는다
            log.exception("%sDB 열기 실패", label)
            users[account_id] = {"open": {"error": f"{type(e).__name__}: {e}"}}
            continue
        try:
            jobs = []
            if _due(conn, "last_backup", BACKUP_EVERY_HOURS, now):
                backup_dir = tenant.user_dir(cfg.data_dir, account_id) / "backup"
                jobs.append(("backup", lambda: {"path": str(backup(conn, backup_dir, cfg.backup_keep))}))
            result = _run(jobs + _reindex_job(conn, analyzer), label)
            result.update(_run(uploader.job(result, lambda p, f: s3backup.user_key(p, account_id, f)), label))
            if result:
                users[account_id] = result
        except Exception as e:  # noqa: BLE001 - meta 를 못 읽는 DB 도 다음 사용자로 넘어간다
            log.exception("%s작업 실패", label)
            users[account_id] = {"open": {"error": f"{type(e).__name__}: {e}"}}
        finally:
            conn.close()
    if users:
        done["users"] = users
    try:
        conn = connect_control(tenant.control_path(cfg.data_dir))
        try:
            if _due(conn, "last_backup", BACKUP_EVERY_HOURS, now):
                control = _run([("backup", lambda: {"path": str(backup(
                    conn, cfg.data_dir / "backup", cfg.backup_keep, name="control"))})], "control ")
                control.update(_run(uploader.job(control, s3backup.control_key), "control "))
                done["control"] = control
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        log.exception("제어 DB 작업 실패")
        done["control"] = {"open": {"error": f"{type(e).__name__}: {e}"}}
    return done
