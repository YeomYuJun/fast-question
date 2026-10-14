import sqlite3
import subprocess
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from kb import evidence
from kb.config import Config
from kb.db import connect, get_meta, set_meta
from kb.jobs import backup, run_due
from kb.store import Store


def make_cfg(tmp_path: Path, **kw) -> Config:
    base = dict(db_path=tmp_path / "kb.sqlite3", host="127.0.0.1", port=8765, backup_dir=tmp_path / "백업 폴더",
                backup_keep=3, log_dir=tmp_path / "logs", log_keep_days=14, recheck_interval_hours=24)
    base.update(kw)
    return Config(**base)


def seeded(cfg: Config):
    conn = connect(cfg.db_path)
    s = Store(conn, "user")
    s.add_category("이관")
    s.add_qa("이관", "백업 확인 질문?", "백업 사본에도 이 행이 있어야 한다.")
    return conn


def test_backup_copies_db_checks_it_and_keeps_n(tmp_path):
    cfg = make_cfg(tmp_path)
    conn = seeded(cfg)
    paths = [backup(conn, cfg.backup_dir, cfg.backup_keep, today=date(2026, 10, d)) for d in range(1, 6)]
    assert paths[-1].name == "kb-20261005.sqlite3"
    assert sorted(p.name for p in cfg.backup_dir.glob("kb-*.sqlite3")) == [
        "kb-20261003.sqlite3", "kb-20261004.sqlite3", "kb-20261005.sqlite3"]
    copy = sqlite3.connect(paths[-1])
    assert copy.execute("SELECT question FROM qa").fetchone()[0] == "백업 확인 질문?"
    assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # 사본 하나만 옮겨도 열린다
    copy.close()
    assert get_meta(conn, "last_backup")
    assert list(cfg.backup_dir.glob("*.tmp*")) == []
    conn.close()


def test_run_due_runs_overdue_jobs_and_skips_fresh(tmp_path):
    cfg = make_cfg(tmp_path)
    conn = seeded(cfg)
    f = tmp_path / "근거.txt"
    f.write_text("v1", encoding="utf-8")
    s = Store(conn, "user")
    item = s.add_qa("이관", "근거 질문?", "근거 답변이다.", evidence=[{"kind": "auto", "path": str(f)}])["item"]
    f.write_text("v2", encoding="utf-8")

    done = run_due(cfg)  # 처음에는 기록이 없으므로 모두 돈다
    assert set(done) >= {"backup", "recheck"}
    assert done["recheck"]["changed"] == 1
    assert s.get_qa(item["id"])["evidence_state"] == "stale"
    actors = [r["actor"] for r in s.history("qa", item["id"])]
    assert actors[0] == "system"

    assert run_due(cfg) == {}  # 방금 돌았으므로 건너뛴다

    later = datetime.now(timezone.utc) + timedelta(hours=25)
    assert set(run_due(cfg, now=later)) == {"backup", "recheck"}
    conn.close()


def test_run_due_reindexes_pending_rows(tmp_path):
    from kb.morph import Analyzer, pending_count

    cfg = make_cfg(tmp_path)
    conn = seeded(cfg)  # 분석기 없이 쓴 행은 재색인 대상이다
    assert pending_count(conn) == 1

    class FakeKiwi:
        def add_user_word(self, *_):
            return True

        def tokenize(self, _):
            return []

    done = run_due(cfg, analyzer=Analyzer(loader=FakeKiwi))
    assert done["reindex"] == 1 and pending_count(conn) == 0
    conn.close()


def test_one_failing_job_does_not_stop_others(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    conn = seeded(cfg)

    def broken(*_, **__):
        raise OSError("디스크 가득 참")
    monkeypatch.setattr("kb.jobs.backup", broken)
    done = run_due(cfg)
    assert "디스크 가득 참" in done["backup"]["error"]
    assert "checked" in done["recheck"]
    conn.close()


def test_git_calls_hide_console_window(monkeypatch, tmp_path):
    repo = tmp_path / "저장소"
    repo.mkdir()
    (repo / "a.txt").write_text("a", encoding="utf-8")
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                       check=True, capture_output=True)
    flags = []
    real = subprocess.run

    def spy(*a, **kw):
        flags.append(kw.get("creationflags"))
        return real(*a, **kw)
    monkeypatch.setattr(evidence.subprocess, "run", spy)
    evidence.capture({"kind": "auto", "path": str(repo / "a.txt")})
    assert flags and set(flags) == {getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def test_web_lifespan_runs_jobs_in_background(tmp_path):
    from starlette.testclient import TestClient

    from kb.morph import Analyzer
    from web.app import create_app

    class FakeKiwi:
        def add_user_word(self, *_):
            return True

        def tokenize(self, _):
            return []

    cfg = make_cfg(tmp_path)
    seeded(cfg).close()
    app = create_app(cfg, run_jobs=True, analyzer=Analyzer(loader=FakeKiwi))
    with TestClient(app, base_url="http://127.0.0.1:8765"):
        for _ in range(50):
            if list(cfg.backup_dir.glob("kb-*.sqlite3")):
                break
            threading.Event().wait(0.1)
    assert list(cfg.backup_dir.glob("kb-*.sqlite3")), "web 이 뜨면 밀린 백업이 돌아야 한다"


def test_autostart_builds_command_without_running(monkeypatch, capsys):
    import scripts.autostart as auto

    calls = []
    monkeypatch.setattr(auto.subprocess, "run", lambda *a, **kw: calls.append(a))
    assert auto.main([]) == 2  # 인자가 없으면 사용법만 보이고 아무것도 바꾸지 않는다
    assert calls == []
    args = auto.install_args()
    assert args[:2] == ["schtasks", "/Create"] and "ONLOGON" in args
    tr = args[args.index("/TR") + 1]
    assert "pythonw.exe" in tr and "run_web.py" in tr and tr.count('"') == 4


def remote_cfg(tmp_path: Path, **kw) -> Config:
    return make_cfg(tmp_path, mode="remote", public_url="https://fq.example.com", data_dir=tmp_path / "data", **kw)


def user_db(cfg: Config, account_id: int, question: str = "사용자 백업 질문?"):
    from kb import tenant

    conn = connect(tenant.db_path_for(cfg.data_dir, account_id))
    s = Store(conn, "user")
    s.add_category("이관")
    s.add_qa("이관", question, "사용자 사본에도 이 행이 있어야 한다.")
    return conn


def test_run_due_all_backs_up_each_user_and_control_db(tmp_path):
    from kb import tenant
    from kb.control import connect_control
    from kb.jobs import run_due_all

    cfg = remote_cfg(tmp_path)
    connect_control(tenant.control_path(cfg.data_dir)).close()
    for acc in (1, 2):
        user_db(cfg, acc, f"계정 {acc} 의 질문?").close()

    done = run_due_all(cfg)
    assert set(done["users"]) == {1, 2}
    for acc in (1, 2):
        assert set(done["users"][acc]) == {"backup"}  # 근거 재검증은 서버에서 돌지 않는다
        copies = list((tenant.user_dir(cfg.data_dir, acc) / "backup").glob("kb-*.sqlite3"))
        assert len(copies) == 1
        copy = sqlite3.connect(copies[0])
        assert copy.execute("SELECT question FROM qa").fetchone()[0] == f"계정 {acc} 의 질문?"
        copy.close()
    assert done["control"]["backup"]["path"].endswith(".sqlite3")
    assert len(list((cfg.data_dir / "backup").glob("control-*.sqlite3"))) == 1
    assert not cfg.db_path.exists() and not cfg.backup_dir.exists()  # 단일 DB 경로는 건드리지 않는다

    assert run_due_all(cfg) == {}  # 방금 돌았으므로 건너뛴다
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    assert set(run_due_all(cfg, now=later)["users"]) == {1, 2}


def test_run_due_all_one_broken_user_does_not_stop_others(tmp_path):
    from kb import tenant
    from kb.jobs import run_due_all

    cfg = remote_cfg(tmp_path)
    broken = tenant.db_path_for(cfg.data_dir, 1)
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"not a database" * 100)
    user_db(cfg, 2).close()

    done = run_due_all(cfg)
    assert "error" in done["users"][1]["open"]
    assert "path" in done["users"][2]["backup"]
    assert "path" in done["control"]["backup"]


def test_run_due_all_reindexes_users_without_recheck(tmp_path):
    from kb import tenant
    from kb.jobs import run_due_all
    from kb.morph import Analyzer, pending_count

    class FakeKiwi:
        def add_user_word(self, *_):
            return True

        def tokenize(self, _):
            return []

    cfg = remote_cfg(tmp_path)
    user_db(cfg, 1).close()
    done = run_due_all(cfg, analyzer=Analyzer(loader=FakeKiwi))
    assert done["users"][1]["reindex"] == 1
    conn = connect(tenant.db_path_for(cfg.data_dir, 1))
    assert pending_count(conn) == 0 and get_meta(conn, "last_recheck") is None
    conn.close()
