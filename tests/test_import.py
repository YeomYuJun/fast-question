"""로컬 DB → 원격 사용자 DB 이전(POST /api/import/db)과 scripts/push_local.py."""
import sqlite3

import httpx
import pytest

from kb import control, guide, tenant
from kb.db import connect, set_meta
from kb.dbcopy import prune
from kb.store import Store
from web.importer import validate
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
        assert c.post("/api/import/db", content=body, headers=OCTET).json() == {"mode": "replaced", "items": 2}
        names = [x["name"] for x in c.get("/api/categories").json()["categories"]]
        assert "이관" in names and "fast-question" in names, "통째로 바꾼 뒤 원격 기본 가이드를 다시 넣는다"
        assert c.get("/api/search", params={"q": "기본 가이드 질문"}).json()["hits"] == []
    s = user_store(cfg, 1)
    guide_n = s.conn.execute("SELECT count(*) FROM qa WHERE created_by = 'system' AND deleted_at IS NULL").fetchone()[0]
    s.conn.close()
    assert guide_n == len(guide.load_remote(URL)["items"])


def with_local_guide(conn):
    """로컬 기본 가이드(system 이 만든 항목)가 든 로컬 DB."""
    s = Store(conn, "system")
    s.add_category("fast-question")
    s.add_category("화면", parent="fast-question")
    s.add_qa("fast-question > 화면", "로컬 가이드 질문 127.0.0.1?", "로컬 화면 주소 안내다.")
    return conn


def test_import_never_takes_system_items_from_upload(cfg, app, tmp_path):
    body = snapshot(with_local_guide(local_db(tmp_path)), tmp_path / "upload.sqlite3")
    with client_for(app, 1) as c:
        assert c.post("/api/import/db", content=body, headers=OCTET).json() == {"mode": "replaced", "items": 2}
        assert c.get("/api/search", params={"q": "로컬 가이드 질문"}).json()["hits"] == []
    s = user_store(cfg, 2)
    s.add_category("기존")
    s.add_qa("기존", "전혀 다른 기존 질문", "이미 있는 답이다.")
    s.conn.close()
    with client_for(app, 2) as c:
        res = c.post("/api/import/db", content=body, headers=OCTET).json()
        assert res["mode"] == "merged" and res["added"] == 2 and res["skipped"] == []
        assert c.get("/api/search", params={"q": "로컬 가이드 질문"}).json()["hits"] == []


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
        before = c.get("/api/categories").json()["categories"]
        r = c.post("/api/import/db", content=make(tmp_path), headers=OCTET)
        assert r.status_code == 400 and r.json()["code"] == "invalid_db", r.text
        assert needle in r.json()["error"]
        assert c.get("/api/categories").json()["categories"] == before
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


def test_replaced_db_is_backed_up_on_next_job_run(cfg, app, tmp_path):
    from kb.jobs import run_due_all

    local = local_db(tmp_path)
    set_meta(local, "last_backup", "2099-01-01T00:00:00.000Z")  # 로컬에서 방금 백업한 DB 를 올린다
    body = snapshot(local, tmp_path / "upload.sqlite3")
    with client_for(app, 1) as c:
        assert c.post("/api/import/db", content=body, headers=OCTET).json()["mode"] == "replaced"
    assert "path" in run_due_all(cfg)["users"][1]["backup"], "서버 백업 기한은 로컬 기록을 따르지 않는다"


SECRET = "사내전용비밀문구"


def with_excluded_category(conn):
    """올리지 않을 분류 `사내`(하위 분류, 보관한 항목, 다른 분류 항목과의 연결 포함)."""
    s = Store(conn, "user")
    s.add_category("사내")
    s.add_category("하위", parent="사내")
    a = s.add_qa("사내", f"{SECRET} 가 든 질문", f"{SECRET} 가 든 답이다.")["item"]
    s.add_qa("사내 > 하위", "하위 분류 질문", f"하위 답 {SECRET}.")
    gone = s.add_qa("사내", "보관한 사내 질문", f"보관한 답 {SECRET}.")["item"]
    s.archive_qa(gone["id"], gone["version"])
    kept = conn.execute("SELECT id FROM qa WHERE question = '체크섬은 어디서 계산하나'").fetchone()[0]
    s.link(a["id"], kept)
    return conn


def triggers(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}


def test_prune_removes_excluded_category_and_system_items_from_copy(tmp_path):
    local = with_excluded_category(with_local_guide(local_db(tmp_path)))
    path = tmp_path / "upload.sqlite3"
    snapshot(local, path)
    local.close()
    assert SECRET.encode() in path.read_bytes()

    assert prune(path, ["사내"]) == {"system": 1, "excluded": 3}
    data = path.read_bytes()
    assert SECRET.encode() not in data, "제외한 분류의 글자가 파일 어디에도(이력·검색 색인·빈 페이지) 남지 않는다"
    assert "로컬 가이드 질문".encode() not in data
    validate(path)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT count(*) FROM qa_fts WHERE qa_fts MATCH ?", (f'"{SECRET}"',)).fetchone()[0] == 0
        assert {r[0] for r in conn.execute("SELECT name FROM category")} == {"이관", "네트워크"}
        assert conn.execute("SELECT count(*) FROM qa_link").fetchone()[0] == 0
        fresh = connect(":memory:")
        assert triggers(conn) == triggers(fresh), "지운 뒤 트리거를 되살린다(통째로 바꾸면 서버 DB 로 복사된다)"
        fresh.close()
    finally:
        conn.close()


def test_prune_keeps_system_category_that_holds_user_items(tmp_path):
    local = with_local_guide(local_db(tmp_path))
    Store(local, "user").add_qa("fast-question > 화면", "내가 쓴 화면 질문", "내가 쓴 답이다.")
    path = tmp_path / "upload.sqlite3"
    snapshot(local, path)
    local.close()
    assert prune(path) == {"system": 1, "excluded": 0}
    conn = sqlite3.connect(path)
    try:
        assert {r[0] for r in conn.execute("SELECT name FROM category")} == {"이관", "네트워크", "fast-question", "화면"}
    finally:
        conn.close()


def test_prune_rejects_unknown_category(tmp_path):
    path = tmp_path / "upload.sqlite3"
    snapshot(local_db(tmp_path), path)
    with pytest.raises(ValueError, match="없는 분류"):
        prune(path, ["없는 분류"])


def test_plan_reports_replace_without_creating_db(cfg, app, tmp_path):
    body = snapshot(with_local_guide(local_db(tmp_path)), tmp_path / "upload.sqlite3")
    with client_for(app, 1) as c:
        r = c.post("/api/import/db/plan", content=body, headers=OCTET)
        assert r.status_code == 200, r.text
        assert r.json() == {"mode": "replaced", "items": 2, "dry_run": True}
    assert not tenant.db_path_for(cfg.data_dir, 1).exists(), "미리보기는 계정 DB 를 만들지 않는다"


def test_plan_matches_merge_and_writes_nothing(cfg, app, tmp_path):
    s = user_store(cfg, 1)
    s.add_category("기존")
    s.add_qa("기존", "체크섬은 어디서 계산하나요", "이미 있는 답이다.")
    s.conn.close()
    db = tenant.db_path_for(cfg.data_dir, 1)
    body = snapshot(local_db(tmp_path), tmp_path / "upload.sqlite3")
    with client_for(app, 1) as c:
        before = c.get("/api/search").json()["total"]
        plan = c.post("/api/import/db/plan", content=body, headers=OCTET).json()
        assert c.get("/api/search").json()["total"] == before
        real = c.post("/api/import/db", content=body, headers=OCTET).json()
    assert plan["dry_run"] is True and plan["mode"] == "merged"
    assert plan["added"] == real["added"] == 1
    assert plan["skipped"] == real["skipped"]
    assert db.exists()


def test_push_local_dry_run_uses_plan_and_writes_nothing(cfg, app, tmp_path, capsys):
    import scripts.push_local as push

    local_db(tmp_path).close()
    acc, token = _token(cfg)
    with client_for(app, None) as c:
        assert push.main(["--db", str(tmp_path / "local.sqlite3"), "--dry-run"],
                         creds={"url": URL, "token": token}, http=c) == 0
    out = capsys.readouterr().out
    assert "미리보기" in out and "통째로 바꾼다" in out and "2" in out
    assert not tenant.db_path_for(cfg.data_dir, acc).exists()


def test_push_local_dry_run_stops_when_server_has_no_plan(tmp_path, capsys):
    import scripts.push_local as push

    local_db(tmp_path).close()
    seen = []

    def handle(request):
        seen.append(request.url.path)
        return httpx.Response(404, json={"error": "없는 경로"})

    http = httpx.Client(transport=httpx.MockTransport(handle))
    assert push.main(["--db", str(tmp_path / "local.sqlite3"), "--dry-run"],
                     creds={"url": URL, "token": "fq_pat_x"}, http=http) == 1
    assert seen == ["/api/import/db/plan"], "옛 서버에 실제 가져오기를 보내지 않는다"
    assert "미리보기를 지원하지 않는다" in capsys.readouterr().err


def test_push_local_excludes_category(cfg, app, tmp_path, capsys):
    import scripts.push_local as push

    with_excluded_category(with_local_guide(local_db(tmp_path))).close()
    acc, token = _token(cfg)
    auth = {"Authorization": f"Bearer {token}"}
    with client_for(app, None) as c:
        assert push.main(["--db", str(tmp_path / "local.sqlite3"), "--exclude-category", "사내"],
                         creds={"url": URL, "token": token}, http=c) == 0
        out = capsys.readouterr().out
        assert "기본 가이드 1건" in out and "3건" in out
        names = [x["name"] for x in c.get("/api/categories", headers=auth).json()["categories"]]
    assert "사내" not in names and "이관" in names
    assert SECRET.encode() not in tenant.db_path_for(cfg.data_dir, acc).read_bytes()


def test_push_local_unknown_category_uploads_nothing(tmp_path, capsys):
    import scripts.push_local as push

    local_db(tmp_path).close()
    seen = []
    http = httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(500)))
    assert push.main(["--db", str(tmp_path / "local.sqlite3"), "--exclude-category", "없는 분류"],
                     creds={"url": URL, "token": "fq_pat_x"}, http=http) == 1
    assert seen == [] and "없는 분류" in capsys.readouterr().err


def test_plan_accepts_db_larger_than_default_body_limit(cfg, app):
    with client_for(app, 1) as c:
        r = c.post("/api/import/db/plan", content=b"x" * (2 * 1024 * 1024), headers=OCTET)
    assert r.status_code == 400 and r.json()["code"] == "invalid_db", "1 MB 기본 상한이 아니라 가져오기 상한을 쓴다"
