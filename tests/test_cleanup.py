import asyncio
import json
import random
import time
from datetime import datetime, timedelta, timezone

import pytest
from mcp.client import Client

from kb import cleanup, rules
from kb.db import connect, get_meta, set_meta
from kb.errors import KbError
from kb.store import Store
from kb.text import bigrams, dice
from mcp_server.server import build_server, store_factory

WEB = "http://127.0.0.1:9999"


@pytest.fixture
def claude(conn):
    s = Store(conn, "claude")
    s.add_category("이관")
    s.add_category("NAS", parent="이관")
    s.add_category("일반 지식")
    return s


def findings(conn, rule=None, open_only=True):
    sql = "SELECT * FROM cleanup_finding WHERE 1 = 1"
    params = []
    if open_only:
        sql += " AND resolved_at IS NULL"
    if rule:
        sql += " AND rule = ?"
        params.append(rule)
    return [dict(r) for r in conn.execute(sql + " ORDER BY id", params)]


def add(s, q, a="rename 으로 확정한다.", cat="이관 > NAS", **kw):
    return s.add_qa(cat, q, a, force=True, **kw)["item"]


# ---------- 스키마 ----------

def test_schema_4_creates_cleanup_table(conn):
    assert get_meta(conn, "schema_version") == "4"
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cleanup_finding)")}
    assert {"qa_id", "other_qa_id", "rule", "detail", "qa_version", "content_hash", "found_at",
            "resolved_at", "resolution", "resolved_by", "note"} <= cols


def test_schema_3_db_gets_cleanup_table_on_connect(tmp_path):
    path = tmp_path / "kb.sqlite3"
    c = connect(path)
    c.execute("DROP TABLE cleanup_finding")
    set_meta(c, "schema_version", "3")
    c.close()
    c = connect(path)
    assert get_meta(c, "schema_version") == "4"
    assert c.execute("SELECT count(*) FROM cleanup_finding").fetchone()[0] == 0
    c.close()


# ---------- 중복 ----------

def test_duplicate_found_in_same_top_category_only(claude, conn):
    old = add(claude, ".part 확장자에 먼저 쓰는 이유는?")
    new = add(claude, ".part 확장자에 먼저 쓰는 이유는 무엇인가?")
    add(claude, ".part 확장자에 먼저 쓰는 이유는 무엇인가요?", cat="일반 지식")  # 다른 대분류
    out = cleanup.scan(conn)
    dup = findings(conn, "duplicate")
    pairs = {(f["qa_id"], f["other_qa_id"]) for f in dup}
    assert (new["id"], old["id"]) in pairs
    assert all(f["qa_id"] != 3 and f["other_qa_id"] != 3 for f in dup)
    assert out["duplicate"] == len(dup)
    assert "유사도" in dup[0]["detail"]


def test_duplicate_with_shared_evidence_uses_lower_threshold(claude, conn, tmp_path):
    f = tmp_path / "NasFileStorage.java"
    f.write_text("class NasFileStorage { void write() {} void other() {} }", encoding="utf-8")
    q1, q2 = "NasFileStorage 쓰기 전 여유 공간 검사 이유", "NasFileStorage 임시 파일 경로 규칙"
    score = dice(bigrams(q1), bigrams(q2))
    assert 0.4 <= score < 0.6, score
    ev = [{"kind": "file", "path": str(f), "anchor": "write"}]
    a = add(claude, q1, evidence=ev)
    b = add(claude, q2, evidence=ev)
    add(claude, "NasFileStorage 임시 파일 경로 규칙은?", evidence=[{"kind": "file", "path": str(f), "anchor": "other"}],
        cat="일반 지식")
    cleanup.scan(conn)
    pairs = {(x["qa_id"], x["other_qa_id"]) for x in findings(conn, "duplicate")}
    assert (b["id"], a["id"]) in pairs
    assert "같은 근거" in findings(conn, "duplicate")[0]["detail"]


def test_scan_is_idempotent_and_closes_gone_findings(claude, conn):
    a = add(claude, ".part 확장자에 먼저 쓰는 이유는?")
    b = add(claude, ".part 확장자에 먼저 쓰는 이유는 무엇인가?")
    cleanup.scan(conn)
    cleanup.scan(conn)
    assert len(findings(conn, "duplicate")) == 1
    claude.archive_qa(b["id"], claude.get_qa(b["id"])["version"])
    out = cleanup.scan(conn)
    assert findings(conn, "duplicate") == []
    closed = findings(conn, "duplicate", open_only=False)[0]
    assert closed["resolution"] == "gone" and closed["resolved_by"] == "system"
    assert out["closed"] == 1
    assert a["id"]


def test_edited_question_closes_duplicate(claude, conn):
    add(claude, ".part 확장자에 먼저 쓰는 이유는?")
    b = add(claude, ".part 확장자에 먼저 쓰는 이유는 무엇인가?")
    cleanup.scan(conn)
    claude.update_qa(b["id"], b["version"], question="NAS 볼륨 여유 공간을 검사하는 시점은?")
    cleanup.scan(conn)
    assert findings(conn, "duplicate") == []


# ---------- 양식 위반 ----------

def test_rule_violation_on_claude_items_only(claude, conn):
    bad = "임시 파일을 쓰는 것 같다."
    c_item = add(claude, "임시 파일을 쓰는 이유는?", a=bad)
    user = Store(conn, "user")
    u_item = add(user, "사용자가 쓴 질문은?", a=bad)
    edited = add(claude, "사람이 고친 질문은?", a="rename 한다.")
    user.update_qa(edited["id"], edited["version"], answer=bad)
    later = add(user, "Claude 가 나중에 고친 질문은?", a="rename 한다.")
    claude.update_qa(later["id"], later["version"], answer=bad)
    cleanup.scan(conn)
    got = {f["qa_id"]: f for f in findings(conn, "rule_violation")}
    assert set(got) == {c_item["id"], later["id"]}
    assert "추정 표현" in got[c_item["id"]]["detail"]
    assert u_item["id"] not in got and edited["id"] not in got


def test_rules_change_triggers_full_rescan(claude, conn, monkeypatch):
    item = add(claude, "백업 주기는?", a="하루에 한 번 돈다.")
    cleanup.scan(conn)
    assert findings(conn, "rule_violation") == []
    cleanup.scan(conn)  # 바뀐 항목이 없다
    monkeypatch.setattr(rules, "BANNED", rules.BANNED + [
        ("새 금지어", __import__("re").compile("하루에"), "쓰지 않는다.")])
    out = cleanup.scan(conn)
    assert [f["qa_id"] for f in findings(conn, "rule_violation")] == [item["id"]]
    assert out["full"] == ["rule_violation"]  # 중복 전수 비교는 다시 하지 않는다


# ---------- 처리 ----------

def test_kept_finding_is_not_recreated_until_content_changes(claude, conn):
    item = add(claude, "임시 파일을 쓰는 이유는?", a="임시 파일을 쓰는 것 같다.")
    cleanup.scan(conn)
    fid = findings(conn, "rule_violation")[0]["id"]
    cleanup.resolve(claude, fid, "kept", note="원문 인용이다")
    set_meta(conn, "cleanup_hash:rule_violation", "old")  # 전수 재검사여도
    cleanup.scan(conn)
    assert findings(conn, "rule_violation") == []
    claude.update_qa(item["id"], item["version"], answer="임시 파일을 쓰는 듯하다.")
    cleanup.scan(conn)
    assert len(findings(conn, "rule_violation")) == 1


def test_resolve_fixed_rechecks_rule(claude, conn):
    item = add(claude, "임시 파일을 쓰는 이유는?", a="임시 파일을 쓰는 것 같다.")
    cleanup.scan(conn)
    fid = findings(conn, "rule_violation")[0]["id"]
    with pytest.raises(KbError, match="아직"):
        cleanup.resolve(claude, fid, "fixed")
    claude.update_qa(item["id"], item["version"], answer="다른 Pod 가 쓰는 중인 파일을 읽지 않게 한다.")
    res = cleanup.resolve(claude, fid, "fixed")
    assert res["resolution"] == "fixed" and res["resolved_by"] == "claude"
    with pytest.raises(KbError, match="이미"):
        cleanup.resolve(claude, fid, "kept")
    with pytest.raises(KbError):
        cleanup.resolve(claude, 999, "kept")


def test_resolve_fixed_duplicate_needs_one_side_archived_or_changed(claude, conn):
    add(claude, ".part 확장자에 먼저 쓰는 이유는?")
    b = add(claude, ".part 확장자에 먼저 쓰는 이유는 무엇인가?")
    cleanup.scan(conn)
    fid = findings(conn, "duplicate")[0]["id"]
    with pytest.raises(KbError, match="아직"):
        cleanup.resolve(claude, fid, "fixed")
    claude.archive_qa(b["id"], b["version"])
    assert cleanup.resolve(claude, fid, "fixed")["resolution"] == "fixed"


def test_resolve_rejects_unknown_resolution(claude, conn):
    add(claude, "임시 파일을 쓰는 이유는?", a="임시 파일을 쓰는 것 같다.")
    cleanup.scan(conn)
    with pytest.raises(KbError):
        cleanup.resolve(claude, findings(conn)[0]["id"], "gone")


def test_list_includes_findings_and_evidence_state(claude, conn, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("v1", encoding="utf-8")
    stale = add(claude, "근거가 바뀐 질문은?", evidence=[{"kind": "file", "path": str(f)}])
    f.write_text("v2", encoding="utf-8")
    claude.recheck()
    add(claude, ".part 확장자에 먼저 쓰는 이유는?")
    dup = add(claude, ".part 확장자에 먼저 쓰는 이유는 무엇인가?")
    cleanup.scan(conn)
    out = cleanup.open_list(claude)
    rules_seen = {x["rule"] for x in out["findings"]}
    assert rules_seen == {"duplicate", "evidence_stale"}
    assert out["total"] == 2
    d = next(x for x in out["findings"] if x["rule"] == "duplicate")
    assert d["item"]["id"] == dup["id"] and d["other"]["question"] == ".part 확장자에 먼저 쓰는 이유는?"
    assert "version" in d["item"] and "answer" in d["item"]
    s = next(x for x in out["findings"] if x["rule"] == "evidence_stale")
    assert s["item"]["id"] == stale["id"] and s["id"] is None
    only = cleanup.open_list(claude, rule="duplicate")
    assert [x["rule"] for x in only["findings"]] == ["duplicate"]
    assert cleanup.open_list(claude, limit=1)["total"] == 2
    assert len(cleanup.open_list(claude, limit=1)["findings"]) == 1


def test_open_rules_by_item(claude, conn):
    add(claude, ".part 확장자에 먼저 쓰는 이유는?")
    b = add(claude, ".part 확장자에 먼저 쓰는 이유는 무엇인가?", a="임시 파일을 쓰는 것 같다.")
    cleanup.scan(conn)
    marks = cleanup.open_rules(conn, [1, b["id"], 99])
    assert marks[b["id"]] == ["duplicate", "rule_violation"]
    assert marks[1] == ["duplicate"]
    assert 99 not in marks


# ---------- 주기 작업 ----------

def test_run_due_scans_once_a_day(tmp_path):
    from tests.test_jobs import make_cfg
    from kb.jobs import run_due

    cfg = make_cfg(tmp_path)
    conn = connect(cfg.db_path)
    s = Store(conn, "claude")
    s.add_category("이관")
    add(s, ".part 확장자에 먼저 쓰는 이유는?", cat="이관")
    add(s, ".part 확장자에 먼저 쓰는 이유는 무엇인가?", cat="이관")
    done = run_due(cfg)
    assert done["cleanup_scan"]["duplicate"] == 1
    assert "cleanup_scan" not in run_due(cfg)
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    assert "cleanup_scan" in run_due(cfg, now=later)
    conn.close()


def test_run_due_all_scans_each_user(tmp_path):
    from tests.test_jobs import remote_cfg, user_db
    from kb.jobs import run_due_all

    cfg = remote_cfg(tmp_path)
    user_db(cfg, 1).close()
    done = run_due_all(cfg)
    assert "duplicate" in done["users"][1]["cleanup_scan"]


# ---------- MCP ----------

@pytest.fixture
def server(tmp_path):
    path = tmp_path / "mcp.sqlite3"
    connect(path).close()
    return path, build_server(store_factory(path, None), web_url=WEB)


def call(server, name, args=None):
    async def go():
        async with Client(server) as c:
            return await c.call_tool(name, args or {})
    r = asyncio.run(go())
    text = r.content[0].text
    return r.is_error, (text if r.is_error else json.loads(text))


def test_mcp_cleanup_list_resolve_and_search_mark(server):
    path, srv = server
    call(srv, "kb_add_category", {"name": "이관"})
    for q in (".part 확장자에 먼저 쓰는 이유는?", ".part 확장자에 먼저 쓰는 이유는 무엇인가?"):
        assert call(srv, "kb_add", {"category": "이관", "question": q, "answer": "rename 으로 확정한다.",
                                    "force": True})[1]["status"] == "created"
    conn = connect(path)
    cleanup.scan(conn)
    conn.close()

    err, res = call(srv, "kb_cleanup_list", {})
    assert not err and res["total"] == 1
    f = res["findings"][0]
    assert f["rule"] == "duplicate" and f["item"]["url"] == f"{WEB}/#qa/{f['item']['id']}"
    assert f["other"]["url"].startswith(WEB)

    err, found = call(srv, "kb_search", {"query": "확장자"})
    assert {h["id"]: h.get("cleanup") for h in found["hits"]} == {1: ["duplicate"], 2: ["duplicate"]}

    err, msg = call(srv, "kb_cleanup_resolve", {"finding_id": f["id"], "resolution": "fixed"})
    assert err and "아직" in msg
    err, res = call(srv, "kb_cleanup_resolve", {"finding_id": f["id"], "resolution": "kept",
                                                "note": "묻는 대상이 다르다"})
    assert not err and res["resolution"] == "kept"
    assert call(srv, "kb_cleanup_list", {})[1]["total"] == 0
    found = call(srv, "kb_search", {"query": "확장자"})[1]
    assert all("cleanup" not in h for h in found["hits"])


# ---------- 성능 ----------

WORDS = ("NAS 볼륨 용량 rename 원자적 경로 암호화 ARIA PETRA 복호화 이관 방화벽 설정 Pod 쓰기 확정 임시 "
         "파일 삭제 검사 이유 원인 S3 Swift 오브젝트 스토리지 DB IV TypeHandler 매퍼 게이트웨이 라우팅 "
         "도메인 CORS 토픽 Kafka 빌드 기동 마운트").split()


@pytest.mark.perf
def test_scan_10k_full_and_incremental(conn):
    rnd = random.Random(1)
    sent = lambda k: " ".join(rnd.choice(WORDS) for _ in range(k))  # noqa: E731
    conn.execute("INSERT INTO category(name, created_by, updated_by) VALUES ('합성', 'claude', 'claude')")
    conn.execute("BEGIN")
    conn.executemany(
        "INSERT INTO qa(category_id, question, answer, created_by, updated_by) VALUES (1, ?, ?, 'claude', 'claude')",
        ((sent(8)[:150], "합성 답변이다.") for _ in range(10_000)))
    conn.execute("COMMIT")
    t0 = time.perf_counter()
    cleanup.scan(conn)
    full = time.perf_counter() - t0
    assert full < 60, f"전수 {full:.1f}초"
    Store(conn, "claude").add_qa("합성", sent(8)[:150], "합성 답변이다.", force=True)
    t0 = time.perf_counter()
    cleanup.scan(conn)
    inc = time.perf_counter() - t0
    assert inc < 5, f"증분 {inc:.1f}초"


def test_prune_drops_findings_of_removed_items(tmp_path):
    from kb.dbcopy import copy_db, prune

    conn = connect(tmp_path / "kb.sqlite3")
    s = Store(conn, "claude")
    s.add_category("이관")
    s.add_category("뺄 분류")
    add(s, ".part 확장자에 먼저 쓰는 이유는?", cat="이관")
    add(s, ".part 확장자에 먼저 쓰는 이유는 무엇인가?", cat="이관")
    gone = add(s, "임시 파일을 쓰는 이유는?", a="임시 파일을 쓰는 것 같다.", cat="뺄 분류")
    cleanup.scan(conn)
    copy = copy_db(conn, tmp_path / "copy.sqlite3")
    conn.close()
    prune(copy, ["뺄 분류"])
    c = connect(copy)
    assert [(r[0], r[1]) for r in c.execute("SELECT qa_id, rule FROM cleanup_finding")] == [(2, "duplicate")]
    assert gone["id"] == 3
    c.close()


def test_list_evidence_detail_carries_verdict_detail(claude, conn, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("v1", encoding="utf-8")
    item = add(claude, "판정 못 한 근거의 질문은?", evidence=[{"kind": "file", "path": str(f)}])
    ev = claude.get_qa(item["id"])["evidence"][0]
    claude.apply_verdicts([{"evidence_id": ev["id"], "version": ev["version"], "verdict": "unknown",
                            "detail": "30일 넘게 사용자 PC 에서 판정하지 않았다"}])
    out = cleanup.open_list(claude, rule="evidence_unknown")
    assert "30일 넘게 사용자 PC 에서 판정하지 않았다" in out["findings"][0]["detail"]
