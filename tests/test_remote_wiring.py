"""병합 뒤 이어 붙인 것: 원격 정리 주기 작업, MCP 로 처음 연결한 계정의 기본 가이드."""
from datetime import datetime, timedelta, timezone

from kb import control, tenant
from kb.db import connect, get_meta
from kb.jobs import run_due_all
from kb.store import Store
from tests.test_auth import analyzer, app, cfg, idp, secrets_env  # noqa: F401 - 픽스처
from tests.test_remote_mcp import call, new_token


def test_purge_deleted_after_30_days(tmp_path):
    t0 = datetime(2026, 10, 14, tzinfo=timezone.utc)
    for acc in (1, 2):
        tenant.db_path_for(tmp_path, acc).parent.mkdir(parents=True)
        tenant.db_path_for(tmp_path, acc).write_bytes(b"x")
    old = tenant.retire(tmp_path, 1, now=t0 - timedelta(days=31))
    new = tenant.retire(tmp_path, 2, now=t0 - timedelta(days=1))
    (tmp_path / "deleted" / "손으로 둔 폴더").mkdir()

    assert tenant.purge_deleted(tmp_path, 30, now=t0) == [old.name]
    assert not old.exists() and new.exists() and (tmp_path / "deleted" / "손으로 둔 폴더").exists()


def test_run_due_all_expires_and_cleans_once_a_day(cfg, tmp_path):
    conn = control.connect_control(tenant.control_path(cfg.data_dir))
    acc = control.create_account(conn)
    conn.close()
    s = Store(connect(tenant.db_path_for(cfg.data_dir, acc)), "user", remote=True)
    s.add_category("이관")
    row = {"kind": "file", "path": "C:/x.txt", "anchor": None, "repo": None, "commit_sha": None, "paths_json": None,
           "captured_json": '{"sha256": "x", "origin": "pc-a"}'}
    s.add_qa("이관", "질문", "답이다.", evidence=[{"kind": "file", "captured": row}])
    s.conn.close()
    tenant.db_path_for(cfg.data_dir, 99).parent.mkdir(parents=True)
    connect(tenant.db_path_for(cfg.data_dir, 99)).close()
    tenant.retire(cfg.data_dir, 99, now=datetime.now(timezone.utc) - timedelta(days=40))

    first = run_due_all(cfg)
    assert first["users"][acc]["expire"] == {"unknown": 0}  # 방금 등록한 근거는 그대로
    assert first["control"]["cleanup"]["oauth_clients"] == 0
    assert len(first["control"]["cleanup"]["deleted_dirs"]) == 1 and first["control"]["cleanup"]["deleted_dirs"][0].startswith("99-")
    second = run_due_all(cfg)
    assert "expire" not in second.get("users", {}).get(acc, {}) and "cleanup" not in second.get("control", {})
    conn = connect(tenant.db_path_for(cfg.data_dir, acc))
    try:
        assert get_meta(conn, "last_expire")
    finally:
        conn.close()


def test_mcp_first_account_gets_default_guide(cfg, app):
    _, token = new_token(cfg)
    err, res = call(app, token, "kb_categories")
    assert not err and "fast-question" in [c["name"] for c in res["categories"]]
