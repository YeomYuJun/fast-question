"""원격 모드 근거 판정: 근거를 등록한 PC 가 자기 파일로 판정해 서버에 보고한다."""
import io
import json
import shutil
import sqlite3
import time

import httpx2
import pytest
from starlette.testclient import TestClient

from kb import remote as remote_mod
from kb import tenant
from kb.db import connect
from kb.store import Store
from scripts import autostart, local_check
from tests.test_auth import URL, analyzer, app, cfg, idp, secrets_env  # noqa: F401 - 픽스처
from tests.test_remote_mcp import new_token
from mcp_server.proxy import Remote, build_proxy


def call(app, token, device, name, args=None):
    async def work(c):
        return await c.call_tool(name, args or {})

    async def go():
        async with app.router.lifespan_context(app):
            remote = Remote(URL, token, device, http_client=lambda: httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url=URL, headers={"Authorization": f"Bearer {token}"}))
            from mcp.client import Client
            async with Client(build_proxy(remote)) as c:
                return await work(c)

    import asyncio
    r = asyncio.run(go())
    text = r.content[0].text
    return r.is_error, (text if r.is_error else json.loads(text))


def setup_item(app, token, device, src):
    call(app, token, device, "kb_add_category", {"name": "이관"})
    err, res = call(app, token, device, "kb_add", {"category": "이관", "question": "근거 질문", "answer": "근거 답이다.",
                                                   "evidence": [{"kind": "file", "path": str(src)}]})
    assert not err, res
    return res["item"]


def local_file(tmp_path, name="a.txt"):
    p = tmp_path / "내 PC" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text("값", encoding="utf-8")
    return p


def user_conn(cfg, acc):
    return sqlite3.connect(tenant.db_path_for(cfg.data_dir, acc))


def test_this_pc_judges_its_own_evidence(cfg, app, tmp_path):
    acc, token = new_token(cfg)
    src = local_file(tmp_path)
    item = setup_item(app, token, "pc-a", src)
    conn = user_conn(cfg, acc)
    origin = json.loads(conn.execute("SELECT captured_json FROM evidence WHERE qa_id = ?",
                                     (item["id"],)).fetchone()[0])["origin"]
    conn.close()
    assert origin == "pc-a"

    err, res = call(app, token, "pc-a", "kb_recheck")
    assert not err and res["results"][0]["verdict"] == "same" and res["results"][0]["changed"] is False

    src.write_text("바뀐 값", encoding="utf-8")
    err, res = call(app, token, "pc-a", "kb_recheck", {"id": item["id"]})
    assert not err, res
    assert res["results"][0]["verdict"] == "changed" and res["results"][0]["changed"] is True
    assert res["items"] == [{"id": item["id"], "question": "근거 질문", "evidence_state": "stale"}]


def test_other_pc_does_not_judge(cfg, app, tmp_path):
    _, token = new_token(cfg)
    src = local_file(tmp_path)
    setup_item(app, token, "pc-a", src)
    src.unlink()  # 다른 PC 에는 이 파일이 없다
    err, res = call(app, token, "pc-b", "kb_recheck")
    assert not err and res == {"results": [], "items": []}
    err, found = call(app, token, "pc-b", "kb_search", {"query": "근거"})
    assert found["hits"][0]["evidence_state"] == "trusted"


def test_verdicts_match_local_recheck(tmp_path):
    """같은 DB 두 벌: 하나는 로컬 recheck, 하나는 judge + apply_verdicts. 판정과 항목 상태가 같아야 한다."""
    files = [local_file(tmp_path, f"{n}.txt") for n in ("same", "changed", "gone")]
    a = tmp_path / "a.sqlite3"
    s = Store(connect(a), "user")
    s.add_category("이관")
    for f in files:
        s.add_qa("이관", f"{f.stem} 질문", "답이다.", evidence=[{"kind": "file", "path": str(f)}], force=True)
    s.conn.close()
    b = tmp_path / "b.sqlite3"
    shutil.copy(a, b)
    files[1].write_text("다름", encoding="utf-8")
    files[2].unlink()

    local = Store(connect(a), "system")
    local.recheck()
    remote = Store(connect(b), "user", remote=True)
    rows = [dict(r) for r in remote.conn.execute(f"SELECT {Store.EV_COLS} FROM evidence")]
    remote.apply_verdicts(remote_mod.judge(rows))

    def snapshot(st):
        return [tuple(r) for r in st.conn.execute(
            "SELECT e.verdict, e.verdict_detail, q.evidence_state FROM evidence e JOIN qa q ON q.id = e.qa_id ORDER BY e.id")]
    assert snapshot(local) == snapshot(remote)
    assert [v for v, _, _ in snapshot(remote)] == ["same", "changed", "changed"]
    local.conn.close()
    remote.conn.close()


def test_stale_version_and_bad_verdicts(tmp_path):
    src = local_file(tmp_path)
    s = Store(connect(tmp_path / "kb.sqlite3"), "user", remote=False)
    s.add_category("이관")
    item = s.add_qa("이관", "질문", "답이다.", evidence=[{"kind": "file", "path": str(src)},
                                                    {"kind": "note", "text": "사용자 확인"}])["item"]
    file_ev, note_ev = item["evidence"]
    r = s.apply_verdicts([{"evidence_id": file_ev["id"], "version": file_ev["version"] + 1, "verdict": "changed"},
                          {"evidence_id": note_ev["id"], "version": note_ev["version"], "verdict": "changed"}])
    assert r == []  # 그사이 재기록된 근거, 판정 대상이 아닌 근거는 쓰지 않는다
    with pytest.raises(Exception, match="판정"):
        s.apply_verdicts([{"evidence_id": file_ev["id"], "version": file_ev["version"], "verdict": "external"}])
    s.conn.close()


def test_unchecked_evidence_turns_unknown(tmp_path):
    src = local_file(tmp_path)
    s = Store(connect(tmp_path / "kb.sqlite3"), "user", remote=True)
    s.add_category("이관")
    row = {"kind": "file", "path": str(src), "anchor": None, "repo": None, "commit_sha": None, "paths_json": None,
           "captured_json": json.dumps({"sha256": "x", "origin": "pc-a"})}
    item = s.add_qa("이관", "질문", "답이다.", evidence=[{"kind": "file", "captured": row}])["item"]
    assert s.expire_verdicts(days=30) == 0
    time.sleep(0.01)
    assert s.expire_verdicts(days=0) == 1
    got = s.get_qa(item["id"])
    assert got["evidence_state"] == "unknown" and "판정하지 않았다" in got["evidence"][0]["verdict_detail"]
    s.conn.close()


def test_web_form_keeps_existing_path_evidence(cfg, app, tmp_path):
    acc, token = new_token(cfg)
    src = local_file(tmp_path)
    item = setup_item(app, token, "pc-a", src)
    ev = item["evidence"][0]
    with TestClient(app, base_url=URL, headers={"Authorization": f"Bearer {token}"}) as c:
        cur = c.get(f"/api/qa/{item['id']}").json()
        line = cur["evidence_line"] + " ; https://example.com/doc"
        r = c.patch(f"/api/qa/{item['id']}", json={"version": cur["version"], "evidence_line": line})
        assert r.status_code == 200, r.text
        kept = r.json()["evidence"]
        assert [e["id"] for e in kept if e["kind"] == "file"] == [ev["id"]] and kept[-1]["kind"] == "url"

        bad = c.patch(f"/api/qa/{item['id']}", json={"version": r.json()["version"],
                                                    "evidence_line": str(tmp_path / "새 파일.txt")})
        assert bad.status_code == 400 and "로컬 프록시" in bad.json()["error"]


def test_local_check_script(cfg, app, tmp_path):
    acc, token = new_token(cfg)
    src = local_file(tmp_path)
    setup_item(app, token, "pc-a", src)
    src.write_text("바뀐 값", encoding="utf-8")

    with TestClient(app, base_url=URL) as c:
        def opener(req, timeout):
            method = "POST" if req.data is not None else "GET"
            r = c.request(method, req.full_url, headers=dict(req.header_items()), content=req.data)
            assert r.status_code == 200, r.text
            return io.BytesIO(r.content)

        res = local_check.check({"url": URL, "token": token, "device_id": "pc-a"}, opener=opener)
    assert local_check.summary(res) == "판정 1건, 판정이 바뀐 근거 1건 (항목 상태: stale 1)"


def test_credentials_get_device_id(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    monkeypatch.setenv("FQ_CREDENTIALS", str(path))
    path.write_text(json.dumps({"url": URL, "token": "fq_pat_x"}), encoding="utf-8")  # device_id 없는 옛 파일
    first = remote_mod.load_credentials()["device_id"]
    assert first and json.loads(path.read_text(encoding="utf-8"))["device_id"] == first
    remote_mod.save_credentials(URL, "fq_pat_new")  # 토큰을 바꿔도 PC 식별자는 그대로
    assert remote_mod.load_credentials()["device_id"] == first


def test_check_task_command():
    args = autostart.check_install_args()
    assert args[:5] == ["schtasks", "/Create", "/TN", "fast-question-check", "/SC"] and "DAILY" in args
    assert "local_check.py" in args[args.index("/TR") + 1]
