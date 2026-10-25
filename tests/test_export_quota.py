"""원격 모드: DB 파일 내보내기, 요청 본문 크기 제한, 계정당 용량 제한."""
import pytest
from starlette.testclient import TestClient

from kb import store as store_mod
from kb.db import connect
from kb.errors import ValidationError
from kb.store import Store
from tests.test_auth import URL, analyzer, app, cfg, idp, login, secrets_env  # noqa: F401 - 픽스처


def test_export_db_opens_as_local_db(app, tmp_path):
    with TestClient(app, base_url=URL) as c:
        assert c.get("/api/export/db").status_code == 401
        login(c)
        assert c.post("/api/categories", json={"name": "이관"}).status_code == 200
        assert c.post("/api/qa", json={"category": "이관", "question": "내보낼 질문", "answer": "답이다."}).status_code == 200
        r = c.get("/api/export/db")
    assert r.status_code == 200 and "fast-question-" in r.headers["content-disposition"]
    out = tmp_path / "내보낸 kb.sqlite3"
    out.write_bytes(r.content)
    conn = connect(out)  # 로컬 모드가 여는 방식 그대로
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert [r[0] for r in conn.execute("SELECT question FROM qa WHERE created_by != 'system'")] == ["내보낼 질문"]
    finally:
        conn.close()


def test_request_body_limit(app):
    with TestClient(app, base_url=URL) as c:
        login(c)
        r = c.post("/api/qa", content=b"x" * (1024 * 1024 + 1), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "too_large"


def test_quota_only_in_remote_mode(tmp_path, monkeypatch):
    monkeypatch.setitem(store_mod.QUOTAS, "category", ("분류", 2))
    monkeypatch.setitem(store_mod.QUOTAS, "qa", ("Q&A", 1))
    remote = Store(connect(tmp_path / "remote.sqlite3"), "user", remote=True)
    remote.add_category("가")
    remote.add_category("나")
    with pytest.raises(ValidationError) as e:
        remote.add_category("다")
    assert e.value.code == "quota" and "분류 2건" in e.value.message
    remote.add_qa("가", "질문 하나", "답이다.")
    with pytest.raises(ValidationError, match="Q&A"):
        remote.add_qa("가", "질문 둘", "답이다.", force=True)

    local = Store(connect(tmp_path / "local.sqlite3"), "user")
    for name in ("가", "나", "다"):
        local.add_category(name)
    remote.conn.close()
    local.conn.close()


def test_db_size_cap_blocks_writes_that_grow_the_file(tmp_path, monkeypatch):
    """파일 크기 상한은 행 수 상한이 못 막는 것(고칠 때마다 쌓이는 이력, 사전, 수확 기록)까지 막는다.
    보관·복원·연결 끊기·근거 판정은 막지 않는다."""
    s = Store(connect(tmp_path / "remote.sqlite3"), "user", remote=True)
    s.add_category("가")
    one = s.add_qa("가", "질문 하나", "답이다.")["item"]
    two = s.add_qa("가", "질문 둘", "답이다.", force=True)["item"]
    s.link(one["id"], two["id"])
    monkeypatch.setattr(store_mod, "DB_MAX_BYTES", store_mod.db_bytes(s.conn))
    writes = [lambda: s.add_category("나"), lambda: s.add_qa("가", "질문 셋", "답이다.", force=True),
              lambda: s.update_qa(one["id"], one["version"], answer="고친 답이다."),
              lambda: s.harvest_set("D:/work", {"repos": {}})]
    for write in writes:
        with pytest.raises(ValidationError) as e:
            write()
        assert e.value.code == "quota" and "저장소 파일 받기" in e.value.message
    s.unlink(one["id"], two["id"])
    s.archive_qa(one["id"], s.get_qa(one["id"])["version"])
    s.restore_qa(one["id"], s.get_qa(one["id"])["version"])
    assert Store(s.conn, "system").add_category("system 은 막지 않는다")  # 기본 가이드 갱신 등 서버 작업
    s.conn.close()


def test_db_size_cap_is_remote_only(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "DB_MAX_BYTES", 0)
    local = Store(connect(tmp_path / "local.sqlite3"), "user")
    local.add_category("가")
    local.conn.close()


def test_api_reports_size_cap_as_quota(app, monkeypatch):
    with TestClient(app, base_url=URL) as c:
        login(c)
        monkeypatch.setattr(store_mod, "DB_MAX_BYTES", 0)
        r = c.post("/api/categories", json={"name": "이관"})
    assert r.status_code == 400 and r.json()["code"] == "quota"
