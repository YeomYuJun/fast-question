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
