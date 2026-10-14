"""원격 모드 웹: 요청의 계정이 DB 파일을 정한다. 계정은 인증 계층이 ASGI scope 에 넣은 값만 쓴다."""
import threading

import pytest
from starlette.testclient import TestClient

from kb import tenant
from kb.config import Config
from kb.morph import Analyzer
from web.app import ACCOUNT_KEY, create_app

URL = "https://fq.example.com"


def _no_kiwi():
    raise RuntimeError("이 파일은 형태소 계층을 쓰지 않는다")


@pytest.fixture(scope="module")
def analyzer():
    # Kiwi(약 600 MB)를 띄우지 않는다. 형태소 계층이 꺼지면 E·Y 로 찾는다(test_morph 가 확인한다)
    return Analyzer(loader=_no_kiwi)


@pytest.fixture
def cfg(tmp_path):
    return Config(db_path=tmp_path / "local.sqlite3", host="0.0.0.0", port=8765, backup_dir=tmp_path / "backup",
                  backup_keep=3, log_dir=tmp_path / "logs", log_keep_days=14, recheck_interval_hours=24,
                  mode="remote", public_url=URL, data_dir=tmp_path / "data")


class AsAccount:
    """테스트용 인증 계층: 모든 요청을 한 계정으로 표시한다."""

    def __init__(self, app, account_id):
        self.app, self.account_id = app, account_id

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.account_id is not None:
            scope = dict(scope, **{ACCOUNT_KEY: self.account_id})
        await self.app(scope, receive, send)


@pytest.fixture
def app(cfg, analyzer):
    return create_app(cfg, run_jobs=False, analyzer=analyzer)


def client_for(app, account_id, base_url=URL):
    return TestClient(AsAccount(app, account_id), base_url=base_url)


def add_basics(c):
    assert c.post("/api/categories", json={"name": "이관"}).status_code == 200
    r = c.post("/api/qa", json={"category": "이관", "question": ".part 를 쓰는 이유",
                                "answer": "다 쓴 뒤 rename 으로 확정한다."})
    assert r.status_code == 200
    return r.json()["item"]


def test_startup_prepares_control_db_only(cfg, app):
    assert tenant.control_path(cfg.data_dir).exists()
    assert not cfg.db_path.exists()  # 로컬 모드 DB 를 만들지 않는다
    assert app.state.db_path is None


def test_unauthenticated_api_is_401(cfg, app):
    with client_for(app, None) as c:
        for path in ("/api/search", "/api/categories", "/api/qa/1", "/api/export?category=1"):
            r = c.get(path)
            assert r.status_code == 401 and r.json()["code"] == "unauthorized", path
        assert c.post("/api/categories", json={"name": "이관"}).status_code == 401
        assert c.get("/").status_code == 200  # 화면은 로그인 전에도 뜬다
    assert not (cfg.data_dir / "users").exists()


def test_account_cannot_come_from_request(cfg, app):
    """헤더·쿼리로 계정을 지정할 수 없다. scope 키는 인증 계층만 채운다."""
    with client_for(app, None) as c:
        r = c.get("/api/search", params={"account": 1, ACCOUNT_KEY: 1},
                  headers={"X-Account-Id": "1", ACCOUNT_KEY: "1"})
        assert r.status_code == 401
    assert not (cfg.data_dir / "users").exists()


def test_account_gets_own_db(cfg, app):
    with client_for(app, 1) as c:
        item = add_basics(c)
        assert [h["id"] for h in c.get("/api/search", params={"q": "rename"}).json()["hits"]] == [item["id"]]
        assert item["created_by"] == "user"
    assert tenant.db_path_for(cfg.data_dir, 1).exists()
    assert not cfg.db_path.exists()


def test_accounts_are_isolated(app):
    with client_for(app, 1) as a:
        item = add_basics(a)
        cat = a.get("/api/categories").json()["categories"][0]["id"]
    with client_for(app, 2) as b:
        assert b.get("/api/search", params={"q": "rename"}).json()["hits"] == []
        assert b.get("/api/categories").json()["categories"] == []
        assert b.get(f"/api/qa/{item['id']}").status_code == 404
        assert b.get("/api/export", params={"category": cat}).status_code == 404
        assert b.patch(f"/api/qa/{item['id']}", json={"version": 1, "answer": "덮어쓰기"}).status_code == 404
    with client_for(app, 1) as a:
        assert a.get(f"/api/qa/{item['id']}").json()["answer"] == "다 쓴 뒤 rename 으로 확정한다."
        assert a.get("/api/export", params={"category": cat}).status_code == 200


def test_remote_host_and_origin_check(app):
    with client_for(app, 1, base_url="http://127.0.0.1:8765") as c:
        r = c.get("/api/categories")
        assert r.status_code == 403 and "fq.example.com" in r.json()["error"]
    with client_for(app, 1) as c:
        assert c.get("/api/categories", headers={"Origin": "https://evil.example"}).status_code == 403
        assert c.get("/api/categories", headers={"Origin": URL}).status_code == 200


def test_remote_mode_runs_per_user_jobs_only(cfg, analyzer):
    """원격 모드는 사용자 DB 마다 주기 작업을 돌리고 단일 DB(cfg.db_path)·cfg.backup_dir 와 Kiwi 미리 띄우기는 하지 않는다."""
    from kb.db import connect

    connect(tenant.db_path_for(cfg.data_dir, 1)).close()
    before = set(threading.enumerate())
    app = create_app(cfg, run_jobs=True, analyzer=analyzer)
    user_backups = tenant.user_dir(cfg.data_dir, 1) / "backup"
    with client_for(app, 1) as c:
        assert c.get("/api/categories").status_code == 200
        names = {t.name for t in set(threading.enumerate()) - before}
        for _ in range(50):
            if list(user_backups.glob("kb-*.sqlite3")) and list((cfg.data_dir / "backup").glob("control-*.sqlite3")):
                break
            threading.Event().wait(0.1)
    assert "jobs" in names and "kiwi-warmup" not in names
    assert list(user_backups.glob("kb-*.sqlite3")), "web 이 뜨면 사용자 DB 백업이 돌아야 한다"
    assert list((cfg.data_dir / "backup").glob("control-*.sqlite3")), "제어 DB 도 백업한다"
    assert not cfg.db_path.exists() and not cfg.backup_dir.exists()


def test_seed_guide_skipped_in_remote_mode(cfg):
    import logging

    import run_web

    run_web.seed_guide(cfg, logging.getLogger("test"))

    assert not cfg.db_path.exists()


def test_projects_for_hook(app):
    with client_for(app, 1) as c:
        add_basics(c)
        assert c.get("/api/projects").json() == {"names": ["이관"], "roots": []}
