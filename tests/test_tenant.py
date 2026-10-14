import pytest

from kb import tenant
from kb.control import connect_control, create_account, get_account


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "한글 데이터"


def test_db_path_is_per_account(data_dir):
    assert tenant.db_path_for(data_dir, 7) == data_dir / "users" / "7" / "kb.sqlite3"
    assert tenant.control_path(data_dir) == data_dir / "control.sqlite3"


@pytest.mark.parametrize("bad", ["7", "../x", True, 0, -1, 7.0, None])
def test_db_path_rejects_non_positive_int(data_dir, bad):
    with pytest.raises(ValueError):
        tenant.db_path_for(data_dir, bad)


def test_list_user_dbs_skips_other_folders(data_dir):
    for name in ("10", "2", "x", "3"):
        (data_dir / "users" / name).mkdir(parents=True)
    for name in ("10", "2", "x"):
        (data_dir / "users" / name / "kb.sqlite3").write_bytes(b"")

    assert tenant.list_user_dbs(data_dir) == [(2, tenant.db_path_for(data_dir, 2)),
                                              (10, tenant.db_path_for(data_dir, 10))]
    assert tenant.list_user_dbs(data_dir / "없음") == []


def test_retire_moves_user_folder(data_dir):
    path = tenant.db_path_for(data_dir, 5)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"kb")

    moved = tenant.retire(data_dir, 5)

    assert not path.parent.exists()
    assert moved.parent == data_dir / "deleted" and moved.name.startswith("5-")
    assert (moved / "kb.sqlite3").read_bytes() == b"kb"
    assert tenant.list_user_dbs(data_dir) == []


def test_control_db_has_its_own_schema(data_dir):
    conn = connect_control(tenant.control_path(data_dir))
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"meta", "account"} <= tables
        assert "qa" not in tables and "revision" not in tables  # KB 스키마를 깔지 않는다
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_accounts_survive_reopen(data_dir):
    path = tenant.control_path(data_dir)
    conn = connect_control(path)
    a, b = create_account(conn, "본인"), create_account(conn)
    conn.close()

    conn = connect_control(path)
    try:
        assert b > a
        assert get_account(conn, a)["display_name"] == "본인"
        assert get_account(conn, b)["deleted_at"] is None
        assert get_account(conn, b + 100) is None
    finally:
        conn.close()


def test_control_db_rebuilds_when_ddl_changes(data_dir, monkeypatch):
    from kb import control
    path = tenant.control_path(data_dir)
    connect_control(path).close()
    monkeypatch.setattr(control, "DDL", control.DDL + "CREATE TABLE IF NOT EXISTS later_feature (id INTEGER PRIMARY KEY);")
    monkeypatch.setattr(control, "CONTROL_SCHEMA_VERSION", "changed")
    conn = connect_control(path)
    try:
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'later_feature'").fetchone()[0] == 1
        assert conn.execute("SELECT value FROM meta WHERE key = 'control_schema_version'").fetchone()[0] == "changed"
    finally:
        conn.close()
