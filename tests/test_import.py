"""로컬 DB → 원격 사용자 DB 이전(POST /api/import/db)과 scripts/push_local.py."""
import sqlite3

import pytest

from kb import control, tenant
from kb.db import connect, set_meta
from kb.store import Store
from tests.test_remote_web import URL, analyzer, app, cfg, client_for  # noqa: F401 - 픽스처

OCTET = {"Content-Type": "application/octet-stream"}


def local_db(tmp_path, name="local.sqlite3"):
    """사용자 PC 의 로컬 DB: 파일·url·메모 근거가 달린 항목 2개와 보관한 항목 1개."""
    src = tmp_path / "근거 폴더" / "a.txt"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("근거 내용", encoding="utf-8")
    conn = connect(tmp_path / name)
    s = Store(conn, "user")
    s.add_category("이관")
    s.add_category("네트워크", parent="이관", description="전송 경로")
    s.add_qa("이관 > 네트워크", ".part 를 쓰는 이유", "다 쓴 뒤 rename 으로 확정한다.",
             evidence=[{"kind": "auto", "path": str(src), "anchor": "근거"},
                       {"kind": "url", "url": "https://example.com/doc"},
                       {"kind": "note", "text": "직접 확인했다"}])
    s.add_qa("이관", "체크섬은 어디서 계산하나", "보내는 쪽에서 sha256 으로 계산한다.")
    gone = s.add_qa("이관", "보관할 질문", "보관할 답이다.")["item"]
    s.archive_qa(gone["id"], gone["version"])
    return conn


def snapshot(conn, path):
    """push_local 처럼 온라인 백업 사본을 만든다."""
    dst = sqlite3.connect(path)
    conn.backup(dst)
    dst.execute("PRAGMA journal_mode = DELETE")
    dst.close()
    return path.read_bytes()


def user_store(cfg, account_id, actor="user"):
    return Store(connect(tenant.db_path_for(cfg.data_dir, account_id)), actor)


def test_import_into_new_account_replaces_file(cfg, app, tmp_path):
    body = snapshot(local_db(tmp_path), tmp_path / "upload.sqlite3")
    with client_for(app, 1) as c:
        r = c.post("/api/import/db", content=body, headers=OCTET)
        assert r.status_code == 200, r.text
        assert r.json() == {"mode": "replaced", "items": 2}
        hits = c.get("/api/search", params={"q": "rename"}).json()["hits"]
        assert [h["question"] for h in hits] == [".part 를 쓰는 이유"]
        item = c.get(f"/api/qa/{hits[0]['id']}").json()
    assert [e["kind"] for e in item["evidence"]] == ["file", "url", "note"]
    assert item["revisions"], "revision 이력이 그대로 옮겨진다"
    s = user_store(cfg, 1)
    assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    s.conn.close()


def test_import_replaces_db_that_has_only_system_rows(cfg, app, tmp_path):
    s = user_store(cfg, 1, "system")
    s.add_category("fast-question")
    s.add_qa("fast-question", "기본 가이드 질문?", "기본 가이드 답이다.")
    s.conn.close()
    body = snapshot(local_db(tmp_path), tmp_path / "upload.sqlite3")
    with client_for(app, 1) as c:
        assert c.post("/api/import/db", content=body, headers=OCTET).json()["mode"] == "replaced"
        assert [x["name"] for x in c.get("/api/categories").json()["categories"]] == ["이관"]


def test_import_into_used_account_adds_items_and_skips_similar(cfg, app, tmp_path):
    s = user_store(cfg, 1)
    s.add_category("기존")
    s.add_qa("기존", "체크섬은 어디서 계산하나요", "이미 있는 답이다.")
    s.conn.close()
    local = local_db(tmp_path)
    Store(local, "user").add_qa("이관", ".part 를 쓰는 이유는?", "비슷한 질문끼리도 둘 다 들어간다.", force=True)
    body = snapshot(local, tmp_path / "upload.sqlite3")
    with client_for(app, 1) as c:
        r = c.post("/api/import/db", content=body, headers=OCTET)
        assert r.status_code == 200, r.text
        res = r.json()
        assert res["mode"] == "merged" and res["added"] == 2 and res["failed"] == []
        assert [x["question"] for x in res["skipped"]] == ["체크섬은 어디서 계산하나"]
        assert res["skipped"][0]["similar"][0]["question"] == "체크섬은 어디서 계산하나요"
        hit = c.get("/api/search", params={"q": "rename"}).json()["hits"][0]
        item = c.get(f"/api/qa/{hit['id']}").json()
    assert item["created_by"] == "user" and item["category_path"] == "이관 > 네트워크"
    ev = {e["kind"]: e for e in item["evidence"]}
    assert set(ev) == {"file", "url", "note"} and ev["file"]["verdict"] == "same"
    assert ev["file"]["path"].endswith("a.txt") and ev["note"]["text"] == "직접 확인했다"
    s = user_store(cfg, 1)
    row = s.conn.execute("SELECT captured_json FROM evidence WHERE kind = 'file'").fetchone()
    orig = local.execute("SELECT captured_json FROM evidence WHERE kind = 'file'").fetchone()
    assert row[0] == orig[0], "근거 캡처값을 그대로 넣는다"
    assert s.conn.execute("SELECT count(*) FROM qa WHERE question = '보관할 질문'").fetchone()[0] == 0
    s.conn.close()
    local.close()


def _schema_mismatch(tmp_path):
    conn = local_db(tmp_path, "old.sqlite3")
    set_meta(conn, "schema_version", "2")
    return snapshot(conn, tmp_path / "old-upload.sqlite3")


def _extra_table(tmp_path):
    conn = local_db(tmp_path, "extra.sqlite3")
    conn.execute("CREATE TABLE other (x)")
    return snapshot(conn, tmp_path / "extra-upload.sqlite3")


def _truncated(tmp_path):
    conn = local_db(tmp_path, "cut.sqlite3")
    for i in range(300):
        Store(conn, "user").add_qa("이관", f"질문 {i} 번째 내용", "답변이다. " * 30, force=True)
    data = snapshot(conn, tmp_path / "cut-upload.sqlite3")
    return data[: len(data) // 2]


@pytest.mark.parametrize("make, needle", [
    (lambda tmp: b"not a sqlite file" * 64, "손상"),
    (_truncated, "손상"),
    (_schema_mismatch, "스키마 버전"),
    (_extra_table, "표 목록"),
])
def test_import_rejects_bad_files_and_keeps_db(cfg, app, tmp_path, make, needle):
    with client_for(app, 1) as c:
        c.post("/api/categories", json={"name": "그대로"})
        r = c.post("/api/import/db", content=make(tmp_path), headers=OCTET)
        assert r.status_code == 400 and r.json()["code"] == "invalid_db", r.text
        assert needle in r.json()["error"]
        assert [x["name"] for x in c.get("/api/categories").json()["categories"]] == ["그대로"]
    assert not list((cfg.data_dir / "tmp").glob("*")), "받은 임시 파일을 지운다"


def test_import_size_limit(cfg, app, monkeypatch, tmp_path):
    import web.importer

    monkeypatch.setattr(web.importer, "IMPORT_MAX_BYTES", 1024)
    with client_for(app, 1) as c:
        r = c.post("/api/import/db", content=b"x" * 2048, headers=OCTET)
    assert r.status_code == 413 and r.json()["code"] == "too_large"
    assert not tenant.db_path_for(cfg.data_dir, 1).exists()


def test_import_needs_login(cfg, app, tmp_path):
    with client_for(app, None) as c:
        assert c.post("/api/import/db", content=b"x", headers=OCTET).status_code == 401
        assert c.post("/api/import/db", content=b"x",
                      headers=dict(OCTET, Authorization="Bearer fq_pat_garbage")).status_code == 401


def _token(cfg):
    conn = control.connect_control(tenant.control_path(cfg.data_dir))
    try:
        acc = control.create_account(conn)
        return acc, control.create_personal_token(conn, acc, "테스트 PC")["token"]
    finally:
        conn.close()


def test_push_local_uploads_snapshot_with_personal_token(cfg, app, tmp_path, capsys):
    import scripts.push_local as push

    local = local_db(tmp_path)
    local.close()
    acc, token = _token(cfg)
    creds = {"url": URL, "token": token}
    with client_for(app, None) as c:
        assert push.main(["--db", str(tmp_path / "local.sqlite3")], creds=creds, http=c) == 0
        out = capsys.readouterr().out
        assert "통째로 바꿨다" in out and "2" in out
        hits = c.get("/api/search", params={"q": "rename"}, headers={"Authorization": f"Bearer {token}"})
        assert [h["question"] for h in hits.json()["hits"]] == [".part 를 쓰는 이유"]
    assert tenant.db_path_for(cfg.data_dir, acc).exists()
    assert not list(tmp_path.glob("*.tmp")), "로컬 쪽 임시 사본을 남기지 않는다"


def test_push_local_without_credentials_fails_cleanly(tmp_path, capsys):
    import scripts.push_local as push

    local_db(tmp_path).close()
    assert push.main(["--db", str(tmp_path / "local.sqlite3")], creds=None) == 1
    assert "credentials.json" in capsys.readouterr().err


def _origins(cfg, account_id):
    s = user_store(cfg, account_id)
    try:
        return {r[0]: r[1] for r in s.conn.execute(
            "SELECT kind, json_extract(captured_json, '$.origin') FROM evidence WHERE deleted_at IS NULL")}
    finally:
        s.conn.close()


def test_import_marks_file_evidence_with_uploading_pc(cfg, app, tmp_path):
    body = snapshot(local_db(tmp_path), tmp_path / "upload.sqlite3")
    with client_for(app, 1) as c:
        assert c.post("/api/import/db?origin=pc-1", content=body, headers=OCTET).json()["mode"] == "replaced"
    assert _origins(cfg, 1) == {"file": "pc-1", "url": None, "note": None}

    s = user_store(cfg, 2)
    s.add_category("기존")
    s.add_qa("기존", "전혀 다른 기존 질문", "이미 있는 답이다.")
    s.conn.close()
    with client_for(app, 2) as c:
        assert c.post("/api/import/db?origin=pc-2", content=body, headers=OCTET).json()["mode"] == "merged"
        assert c.post("/api/import/db?origin=../x", content=body, headers=OCTET).status_code == 400
    assert _origins(cfg, 2) == {"file": "pc-2", "url": None, "note": None}


def test_push_local_sends_device_id_as_origin(cfg, app, tmp_path):
    import scripts.push_local as push

    local_db(tmp_path).close()
    acc, token = _token(cfg)
    with client_for(app, None) as c:
        creds = {"url": URL, "token": token, "device_id": "pc-abc"}
        assert push.main(["--db", str(tmp_path / "local.sqlite3")], creds=creds, http=c) == 0
    assert _origins(cfg, acc)["file"] == "pc-abc"
