"""공개 지식 층(kb/public_kb.py, scripts/public_db.py): 운영자가 적재한 공개 DB 를 모든 계정이 읽기 전용으로 검색한다."""
import json
import sqlite3

import pytest
from starlette.testclient import TestClient

from kb import public_kb, tenant
from kb.db import connect
from kb.store import Store
from scripts import public_db
from tests.test_auth import URL, analyzer, app, cfg, idp, login, secrets_env  # noqa: F401 - 픽스처
from tests.test_remote_mcp import call, new_token


def build_source(tmp_path):
    """운영자가 어느 계정에서 내려받은 저장소 파일: 공개할 항목 2건 + 로컬 기본 가이드 흔적(system) 1건."""
    conn = connect(tmp_path / "source.sqlite3")
    s = Store(conn, "claude")
    s.add_category("Kubernetes", description="일반 개념")
    s.add_category("워크로드", parent="Kubernetes", description="Pod·Deployment")
    s.add_qa("Kubernetes > 워크로드", "Deployment 롤링 업데이트는 언제 일어나나?",
             "Pod 템플릿이 바뀔 때만 일어난다.\n- replicas 만 바꾸면 개수 조절이다",
             evidence=[{"kind": "url", "url": "https://kubernetes.io/docs/concepts/workloads/controllers/deployment/"}])
    s.add_qa("Kubernetes > 워크로드", "Pod 가 CrashLoopBackOff 이면 무엇을 보나?", "kubectl logs --previous 로 직전 로그를 본다.")
    Store(conn, "system").add_category("fast-question")
    Store(conn, "system").add_qa("fast-question", "fast-question 기본 가이드 항목인가?", "로컬 가이드라 공개 DB 에서 뺀다.")
    path = tmp_path / "upload.sqlite3"
    dst = sqlite3.connect(path)
    conn.backup(dst)
    dst.execute("PRAGMA journal_mode = DELETE")
    dst.close()
    conn.close()
    return path


@pytest.fixture
def public(cfg, tmp_path):
    out = public_db.set_public(cfg.data_dir, build_source(tmp_path))
    assert out["items"] == 2 and out["system"] == 1
    return public_kb.public_path(cfg.data_dir)


def test_set_public_validates_and_drops_guide(cfg, tmp_path, public):
    info = public_db.info(cfg.data_dir)
    assert info["items"] == 2 and info["categories"] == 2  # system 분류는 비어서 빠진다
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"not sqlite" * 20)
    with pytest.raises(ValueError, match="손상"):
        public_db.set_public(cfg.data_dir, bad)
    assert public_db.info(cfg.data_dir)["items"] == 2, "거부된 적재는 기존 공개 DB 를 건드리지 않는다"
    assert public_db.remove(cfg.data_dir) and public_db.info(cfg.data_dir) is None


def test_web_search_merges_public_hits_read_only(app, cfg, public):
    with TestClient(app, base_url=URL) as c:
        login(c)
        c.post("/api/categories", json={"name": "이관"})
        c.post("/api/qa", json={"category": "이관", "question": "내 롤링 배포 메모", "answer": "내 것이다."})
        r = c.get("/api/search", params={"q": "롤링", "public": "1"}).json()
        assert [h["question"] for h in r["hits"]] == ["내 롤링 배포 메모"]
        (ph,) = r["public"]["hits"]
        assert ph["scope"] == "public" and ph["url"] == f"{URL}/#pub/{ph['id']}" and "롤링" in ph["question"]
        assert "public" not in c.get("/api/search", params={"q": "롤링"}).json(), "public=1 을 주지 않으면 합치지 않는다"
        assert "public" not in c.get("/api/search", params={"q": "", "public": "1"}).json(), "빈 질의(최근 목록)에는 합치지 않는다"
        d = c.get(f"/api/public/qa/{ph['id']}").json()
        assert d["scope"] == "public" and d["evidence"][0]["kind"] == "url" and "revisions" not in d
        assert c.get("/api/public/qa/999").status_code == 404
        # 내 DB 의 쓰기 경로는 번호가 겹쳐도 내 DB 의 그 번호만 연다(아래에서 공개 DB 가 그대로인지 본다)
        c.patch(f"/api/qa/{ph['id']}", json={"version": 1, "answer": "고침"})
    conn = public_kb.open_public(cfg.data_dir)
    assert conn.execute("SELECT count(*) FROM qa WHERE deleted_at IS NULL").fetchone()[0] == 2, "공개 DB 는 바뀌지 않는다"
    assert conn.execute("SELECT count(*) FROM qa WHERE answer = '고침'").fetchone()[0] == 0
    conn.close()


def test_copy_public_item_into_own_db_creates_category_path(app, cfg, public):
    with TestClient(app, base_url=URL) as c:
        login(c)
        ph = c.get("/api/search", params={"q": "CrashLoopBackOff", "public": "1"}).json()["public"]["hits"][0]
        r = c.post(f"/api/public/qa/{ph['id']}/copy", json={})
        assert r.status_code == 200 and r.json()["status"] == "created", r.text
        it = r.json()["item"]
        assert it["category_path"] == "Kubernetes > 워크로드" and it["question"] == ph["question"]
        cats = {x["path"]: x for t in c.get("/api/categories").json()["categories"] for x in [t, *t["children"]]}
        assert cats["Kubernetes"]["description"] == "일반 개념" and cats["Kubernetes > 워크로드"]["description"] == "Pod·Deployment"
        # 두 번째 복사는 비슷한 질문으로 막히고, force 로 넣을 수 있다
        again = c.post(f"/api/public/qa/{ph['id']}/copy", json={}).json()
        assert again["status"] == "similar_found" and again["similar"][0]["id"] == it["id"]
        assert c.post(f"/api/public/qa/{ph['id']}/copy", json={"force": True, "category": "이관"}).json()["item"]["category_path"] == "이관"
        # 내 항목은 내 DB 에만 생겼다
        assert c.get(f"/api/qa/{it['id']}").json()["created_by"] == "user"


def test_no_public_db_means_no_public_block_and_404(app, cfg):
    with TestClient(app, base_url=URL) as c:
        login(c)
        assert "public" not in c.get("/api/search", params={"q": "롤링", "public": "1"}).json()
        assert c.get("/api/public/qa/1").status_code == 404
        assert c.post("/api/public/qa/1/copy", json={}).status_code == 404


def test_public_requires_login(app, cfg, public):
    with TestClient(app, base_url=URL) as c:
        assert c.get("/api/public/qa/1").status_code == 401


def test_mcp_search_carries_public_hits_and_get_reads_them(cfg, app, public):
    _, tok = new_token(cfg)
    err, res = call(app, tok, "kb_search", {"query": "CrashLoopBackOff"})
    assert not err and res["hits"] == [] and res["public"]["total"] == 1
    (ph,) = res["public"]["hits"]
    assert ph["scope"] == "public" and ph["url"].endswith(f"/#pub/{ph['id']}") and "version" in ph
    err, got = call(app, tok, "kb_get", {"id": ph["id"], "public": True})
    assert not err and got["scope"] == "public" and got["question"] == ph["question"]
    err, msg = call(app, tok, "kb_get", {"id": 999, "public": True})
    assert err and "찾지 못" in msg
    # 공개 항목을 내 DB 에 두는 길은 kb_add 다
    err, added = call(app, tok, "kb_add", {"category": "fast-question", "question": got["question"], "answer": got["answer"]})
    assert not err and added["status"] == "created"


def test_run_due_all_reindexes_public_db(cfg, public, analyzer):
    from kb.jobs import run_due_all
    out = run_due_all(cfg, analyzer)
    assert "public" not in out or "error" not in json.dumps(out["public"])
