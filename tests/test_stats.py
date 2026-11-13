"""유입·사용 집계(kb/stats.py, scripts/stats.py): 이미 저장된 값만 읽고 DB 를 바꾸지 않는다."""
import json
from datetime import datetime, timedelta, timezone

from kb import control, stats, tenant
from kb.control import connect_control
from kb.db import connect
from kb.store import Store
from scripts import stats as stats_cmd


def build(tmp_path):
    data = tmp_path / "data"
    c = connect_control(tenant.control_path(data))
    a1, a2, a3 = (control.create_account(c) for _ in range(3))
    control.create_session(c, a1)
    control.create_personal_token(c, a2, "pc")
    c.execute("UPDATE personal_token SET last_used_at = created_at")
    control.delete_account(c, a3)
    c.close()
    for acc, actor, n in ((a1, "user", 1), (a2, "claude", 2)):
        conn = connect(tenant.db_path_for(data, acc))
        Store(conn, "system").add_category("가이드")
        Store(conn, "system").add_qa("가이드", "fast-question 기본 가이드인가?", "집계에서 뺀다.")
        s = Store(conn, actor)
        s.add_category("분류")
        for i in range(n):
            s.add_qa("분류", f"집계 대상 질문 {acc}-{i} 번인가?", "그렇다.", force=True)
        conn.close()
    return data, (a1, a2, a3)


def test_inflow_counts_signups_writers_and_items(tmp_path):
    data, _ = build(tmp_path)
    out = stats.inflow(data, days=3)
    assert out["accounts"] == {"alive": 2, "closed": 1}
    assert out["active"] == {"days": 7, "web": 1, "pc": 1}
    assert out["items"] == 3 and out["unreadable"] == []
    today = out["days"][-1]
    assert today == {"day": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "signups": 3, "writers": 2,
                     "added_user": 1, "added_claude": 2}
    assert [d["signups"] for d in out["days"][:-1]] == [0, 0]


def test_inflow_window_excludes_older_days(tmp_path):
    data, _ = build(tmp_path)
    out = stats.inflow(data, days=2, now=datetime.now(timezone.utc) + timedelta(days=10))
    assert all(d["signups"] == 0 and d["writers"] == 0 for d in out["days"])
    assert out["active"]["web"] == 0 and out["active"]["pc"] == 0
    assert out["accounts"]["alive"] == 2 and out["items"] == 3


def test_inflow_does_not_write_and_skips_broken_db(tmp_path):
    data, (a1, a2, _) = build(tmp_path)
    broken = tenant.db_path_for(data, a2)
    for suffix in ("-wal", "-shm"):
        broken.with_name(broken.name + suffix).unlink(missing_ok=True)
    broken.write_bytes(b"not a database")
    good = tenant.db_path_for(data, a1)
    before = good.stat().st_mtime_ns
    out = stats.inflow(data, days=1)
    assert out["unreadable"] == [a2] and out["items"] == 1
    assert good.stat().st_mtime_ns == before


def test_command_prints_table_and_json(tmp_path, capsys):
    data, _ = build(tmp_path)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"mode": "remote", "public_url": "https://kb.example", "data_dir": str(data)}),
                   encoding="utf-8")
    assert stats_cmd.main(["--config", str(cfg), "--days", "2"]) == 0
    text = capsys.readouterr().out
    assert "계정 2개 (탈퇴 1개), 항목 3건" in text and "웹 1개, PC 연결 1개" in text
    assert stats_cmd.main(["--config", str(cfg), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["accounts"]["alive"] == 2
    local = tmp_path / "local.json"
    local.write_text("{}", encoding="utf-8")
    assert stats_cmd.main(["--config", str(local)]) == 2
