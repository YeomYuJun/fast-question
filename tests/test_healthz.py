"""/healthz: DB 연결, 데이터 디스크 여유 10% 이상, 형태소 분석기 켜짐. 하나라도 실패면 503. 인증 없이 열린다."""
import shutil
from collections import namedtuple

import pytest
from starlette.testclient import TestClient

from kb.config import Config
from kb.morph import Analyzer
from web.app import create_app

URL = "https://fq.example.com"
Usage = namedtuple("Usage", "total used free")


def _no_kiwi():
    raise RuntimeError("이 파일은 형태소 계층을 쓰지 않는다")


class FakeKiwi:
    def add_user_word(self, *_):
        return True

    def tokenize(self, _):
        return []


def make_cfg(tmp_path, **kw) -> Config:
    base = dict(db_path=tmp_path / "kb.sqlite3", host="127.0.0.1", port=8765, backup_dir=tmp_path / "backup",
                backup_keep=3, log_dir=tmp_path / "logs", log_keep_days=14, recheck_interval_hours=24)
    base.update(kw)
    return Config(**base)


@pytest.fixture
def remote_cfg(tmp_path):
    return make_cfg(tmp_path, host="0.0.0.0", mode="remote", public_url=URL, data_dir=tmp_path / "data")


def test_remote_healthz_ok_without_login_and_any_host(remote_cfg):
    app = create_app(remote_cfg, run_jobs=False, analyzer=Analyzer(loader=_no_kiwi))
    for base in (URL, "http://127.0.0.1:8765"):  # 컨테이너 안 점검은 Host 가 도메인이 아니다
        with TestClient(app, base_url=base) as c:
            r = c.get("/healthz")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"] is True and body["db"] == "ok" and body["morph"] is True
            assert 0 <= body["disk_free"] <= 1
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        assert c.get("/api/categories").status_code == 403  # 다른 경로의 Host 검사는 그대로다
    assert not (remote_cfg.data_dir / "users").exists()


def test_healthz_503_when_morph_is_off(remote_cfg):
    analyzer = Analyzer(loader=_no_kiwi)
    analyzer.enabled = False
    app = create_app(remote_cfg, run_jobs=False, analyzer=analyzer)
    with TestClient(app, base_url=URL) as c:
        r = c.get("/healthz")
    assert r.status_code == 503 and r.json()["morph"] is False and r.json()["ok"] is False


def test_healthz_503_when_disk_is_nearly_full(remote_cfg, monkeypatch):
    app = create_app(remote_cfg, run_jobs=False, analyzer=Analyzer(loader=_no_kiwi))
    monkeypatch.setattr(shutil, "disk_usage", lambda _: Usage(1000, 950, 50))
    with TestClient(app, base_url=URL) as c:
        r = c.get("/healthz")
    assert r.status_code == 503 and r.json()["disk_free"] == 0.05


def test_healthz_503_when_control_db_fails(remote_cfg, monkeypatch):
    import web.app

    app = create_app(remote_cfg, run_jobs=False, analyzer=Analyzer(loader=_no_kiwi))

    def broken(_):
        raise OSError("디스크를 읽지 못한다")
    monkeypatch.setattr(web.app, "connect_control", broken)
    with TestClient(app, base_url=URL) as c:
        r = c.get("/healthz")
    assert r.status_code == 503 and "디스크를 읽지 못한다" in r.json()["db"]


def test_local_healthz_checks_db_file(tmp_path):
    cfg = make_cfg(tmp_path)
    app = create_app(cfg, run_jobs=False, analyzer=Analyzer(loader=FakeKiwi))  # 로컬은 Kiwi 를 미리 띄운다
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        r = c.get("/healthz")
    assert r.status_code == 200 and r.json()["db"] == "ok"
