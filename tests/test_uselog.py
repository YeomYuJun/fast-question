"""이 PC 의 KB 사용 기록(kb/uselog.py)과 그것을 쓰는 곳(훅의 등록 안내, MCP 프록시)."""
import json

import mcp_types as types

from hooks import ask_kb, stop_harvest
from kb import uselog
from mcp_server import proxy


def test_record_and_read_roundtrip(tmp_path):
    p = tmp_path / "use.jsonl"
    uselog.record("search", [(3, "질문 셋"), (None, "버림")], r"D:\PaaS\cnd-res", p, "2026-11-20T01:00:00Z")
    uselog.record("search", [], "d:/paas", p)  # 빈 목록은 적지 않는다
    p.write_text(p.read_text(encoding="utf-8") + "{broken\n", encoding="utf-8")
    assert uselog.read(p) == [{"at": "2026-11-20T01:00:00Z", "kind": "search", "cwd": "d:/paas/cnd-res",
                               "items": [[3, "질문 셋"]], "v": 2}]
    assert uselog.read(tmp_path / "none.jsonl") == []


def test_examples_count_distinct_days_and_skip_the_day_of_adding(tmp_path):
    p = tmp_path / "use.jsonl"
    def rec(kind, i, day, cwd="d:/paas"):
        uselog.record(kind, [(i, f"질문 {i}")], cwd, p, f"{day}T01:00:00Z")
    rec("add", 1, "2026-11-01"); rec("search", 1, "2026-11-01")  # 등록한 날의 중복 확인 검색은 세지 않는다
    rec("search", 1, "2026-11-05"); rec("get", 1, "2026-11-05")  # 같은 날은 한 번
    rec("hook_hit", 1, "2026-11-09")
    rec("search", 2, "2026-11-05")  # 하루뿐이면 예로 들지 않는다
    rec("search", 3, "2026-11-05", "d:/other"); rec("search", 3, "2026-11-06", "d:/other")
    assert uselog.retrieved_days(uselog.read(p))[1] == {"question": "질문 1", "days": 2}
    assert uselog.examples("D:\\PaaS", path=p) == [("질문 1", 2)]
    assert uselog.examples("d:/paas/cnd-res", path=p) == [("질문 1", 2)]  # 상위 폴더에서 남긴 기록도 본다
    assert uselog.examples(None, path=p) == [("질문 1", 2), ("질문 3", 2)]


def test_record_keeps_the_file_under_the_size_cap(tmp_path, monkeypatch):
    p = tmp_path / "use.jsonl"
    monkeypatch.setattr(uselog, "MAX_BYTES", 2000)
    for i in range(200):
        uselog.record("search", [(i, "질문")], "d:/x", p)
    assert p.stat().st_size < 4000 and uselog.read(p)[-1]["items"] == [[199, "질문"]]


def test_store_conditions_show_retrieved_items_as_examples():
    plain = ask_kb.store_conditions("이관")
    used = ask_kb.store_conditions("이관", [("관리자 API 가 일부만 401 이 되는 원인은?", 3)])
    assert len(used) == len(plain) + 1 and "401" in used[-1] and "3일" in used[-1]
    assert "401" in stop_harvest.context("이관", used=[("관리자 API 가 일부만 401 이 되는 원인은?", 3)])


def test_proxy_logs_search_get_and_add(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(uselog, "record", lambda kind, items, *a, **k: seen.append((kind, items)))
    def result(data, is_error=False):
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(data))], is_error=is_error)
    hits = [{"id": i, "question": f"q{i}"} for i in range(1, 6)]
    proxy.log_use("kb_search", {"query": "q"}, result({"hits": hits}))
    proxy.log_use("kb_get", {"id": 9}, result({"id": 9, "question": "q9"}))
    proxy.log_use("kb_add", {}, result({"status": "created", "item": {"id": 10, "question": "q10"}}))
    proxy.log_use("kb_add", {}, result({"status": "similar_found", "similar": []}))
    proxy.log_use("kb_search", {"query": "q"}, result("boom", is_error=True))
    proxy.log_use("kb_update", {"id": 9}, result({"id": 9, "question": "q9"}))
    proxy.log_use("kb_archive", {"id": 9}, result({"id": 9, "question": "q9"}))
    proxy.log_use("kb_evidence", {"action": "recapture"}, result({"id": 9, "question": "q9"}))
    proxy.log_use("kb_categories", {}, result({"categories": []}))
    proxy.log_use("kb_get", {}, types.CallToolResult(content=[], is_error=False))  # 형식이 달라도 올리지 않는다
    assert seen == [("search", [(1, "q1"), (2, "q2"), (3, "q3")]), ("get", [(9, "q9")]), ("add", [(10, "q10")]),
                    ("add", []), ("update", [(9, "q9")]), ("archive", [(9, "q9")]), ("evidence", [(9, "q9")])]


def test_proxy_sets_study_categories_apart_from_search_results(tmp_path, monkeypatch):
    from kb import remote
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert remote.study_categories() == []
    remote.settings_path().parent.mkdir(parents=True)
    remote.settings_path().write_text('﻿{"study_categories": ["Kubernetes", " "]}', encoding="utf-8")  # BOM 이 붙어 와도 읽는다
    study = remote.study_categories()
    assert study == ["kubernetes"]

    def result(data):
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(data))])
    k8s = {"id": 1, "question": "Pod 와 Deployment 의 차이는?", "category_path": "Kubernetes > 워크로드"}
    paas = {"id": 2, "question": "lea Pod 가 마운트한 NAS 폴더를 확인하려면?", "category_path": "D-Cloud PaaS 이관 사업 > NAS 스토리지"}
    out = json.loads(proxy.set_apart({"query": "Pod"}, result({"total": 2, "hits": [k8s, paas], "similar": [k8s]}),
                                     study).content[0].text)
    assert out["hits"] == [paas] and out["similar"] == [] and out["total"] == 2
    assert out["study"]["hits"] == [k8s] and out["study"]["similar"] == [k8s] and "not facts" in out["study"]["note"]
    # 분류를 준 검색, 학습용 항목이 없는 결과, 오류, 설정이 없는 PC 는 그대로다
    same = result({"hits": [k8s], "similar": []})
    assert proxy.set_apart({"query": "Pod", "category": "Kubernetes"}, same, study) is same
    only = result({"hits": [paas], "similar": []})
    assert proxy.set_apart({"query": "NAS"}, only, study) is only
    assert proxy.set_apart({"query": "Pod"}, same, []) is same
    broken = types.CallToolResult(content=[], is_error=False)
    assert proxy.set_apart({"query": "Pod"}, broken, study) is broken
    # 학습용 항목만 맞은 검색은 프로젝트의 사실이 없던 것이라 0건 검색으로 적는다
    p = tmp_path / "use.jsonl"
    monkeypatch.setenv("FQ_USELOG", str(p))
    proxy.log_use("kb_search", {"query": "Pod"}, proxy.set_apart({"query": "Pod"}, same, study))
    assert [(r["kind"], r["queries"]) for r in uselog.read(p)] == [("miss", ["Pod"])]


def test_proxy_logs_the_terms_of_a_search_without_hits(tmp_path, monkeypatch):
    p = tmp_path / "use.jsonl"
    monkeypatch.setenv("FQ_USELOG", str(p))
    def empty():
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps({"hits": []}))])
    proxy.log_use("kb_search", {"queries": ["RateLimitFilter", " ", "ErrCode=1111"]}, empty())
    proxy.log_use("kb_search", {"query": "넷퍼넬 호출처"}, empty())
    proxy.log_use("kb_search", {"query": r"\d{4}", "regex": True}, empty())  # 글자 패턴 검색은 적지 않는다
    proxy.log_use("kb_search", {"query": "지운 항목", "include_archived": True}, empty())
    rows = uselog.read(p)
    assert [(r["kind"], r["items"], r["queries"]) for r in rows] == [
        ("miss", [], ["RateLimitFilter", "ErrCode=1111"]), ("miss", [], ["넷퍼넬 호출처"])]


def _log(p, kind, day, items=(), queries=None, cwd="d:/paas", hour="01"):
    uselog.record(kind, list(items), cwd, p, f"{day}T{hour}:00:00Z", queries)


def test_misses_close_when_a_later_added_question_holds_the_term(tmp_path):
    p = tmp_path / "use.jsonl"
    _log(p, "add", "2026-11-01", [(1, "RateLimitFilter 가 429 를 내는 조건은?")])  # 놓치기 전의 등록은 메운 것이 아니다
    _log(p, "miss", "2026-11-02", queries=["RateLimitFilter"])
    _log(p, "miss", "2026-11-03", queries=["ratelimitfilter", "ErrCode=1111"])
    _log(p, "miss", "2026-11-03", queries=["다른 폴더 검색어"], cwd="d:/other")
    _log(p, "add", "2026-11-04", [(2, "health 가 503 이고 ErrCode=1111 이 나오면?")])
    got = {e["query"]: e for e in uselog.misses(uselog.read(p), ask_kb.grounded, "d:/paas")}
    assert set(got) == {"RateLimitFilter", "ErrCode=1111"}
    assert (got["RateLimitFilter"]["days"], got["RateLimitFilter"]["closed"]) == (2, False)
    assert got["ErrCode=1111"]["closed"] is True
    now = uselog.datetime(2026, 11, 5, tzinfo=uselog.timezone.utc)
    assert uselog.open_misses("d:/paas", ask_kb.grounded, path=p, now=now) == [("RateLimitFilter", 2)]
    assert uselog.open_misses("d:/paas", ask_kb.grounded, since="2026-11-03T02:00:00Z", path=p, now=now) == []
    assert uselog.open_misses(None, ask_kb.grounded, min_days=2, path=p, now=now) == [("RateLimitFilter", 2)]
    late = uselog.datetime(2027, 1, 5, tzinfo=uselog.timezone.utc)  # MISS_KEEP_DAYS 가 지난 기록은 들지 않는다
    assert uselog.open_misses("d:/paas", ask_kb.grounded, path=p, now=late) == []


def test_measure_gives_the_three_indicators(tmp_path):
    p = tmp_path / "use.jsonl"
    _log(p, "add", "2026-11-01", [(1, "질문 1")])
    _log(p, "search", "2026-11-01", [(1, "질문 1")])  # 등록한 날의 검색과 주소는 세지 않는다
    _log(p, "cite", "2026-11-01", [(1, "")])
    _log(p, "evidence", "2026-11-01", [(1, "질문 1")])
    _log(p, "search", "2026-11-05", [(1, "질문 1"), (2, "질문 2")])
    _log(p, "hook_hit", "2026-11-05", [(3, "질문 3")])
    _log(p, "cite", "2026-11-05", [(1, ""), (9, "")])  # 찾아지지 않은 항목의 주소는 세지 않는다
    _log(p, "miss", "2026-11-05", queries=["ErrCode=1111", "넷퍼넬 호출처"])
    _log(p, "add", "2026-11-06", [(4, "ErrCode=1111 이 나오면?")])
    _log(p, "update", "2026-11-07", [(2, "질문 2")])
    _log(p, "archive", "2026-11-07", [(8, "질문 8")])  # 찾아진 적 없이 지운 항목
    _log(p, "search", "2026-11-20", [(1, "질문 1")])
    m = uselog.measure(uselog.read(p), ask_kb.grounded, "2026-11-01", "2026-11-10")
    assert m["retrieval"] == {"searches": 3, "found": 2, "hook_hits": 1}
    assert m["application"] == {"retrieved": 3, "cited": 1}
    assert m["evolution"] == {"added": 2, "revised": 2, "revised_after_use": 1, "missed": 2, "closed": 1,
                              "open": [("넷퍼넬 호출처", 1)]}
    # 등록 안내: 10분 안에 같은 폴더에서 등록이 있었던 것만 이어진 것으로 센다
    uselog.record("nudge", [], "d:/paas", p, "2026-11-06T00:55:00Z", tag="missed")  # 5분 뒤 #4 등록
    uselog.record("nudge", [], "d:/paas", p, "2026-11-08T01:00:00Z", tag="investigation")
    uselog.record("nudge", [], "d:/other", p, "2026-11-06T00:58:00Z", tag="asked")
    m = uselog.measure(uselog.read(p), ask_kb.grounded, "2026-11-01", "2026-11-10")
    assert m["nudges"] == {"total": 3, "by": {"missed": 1, "investigation": 1, "asked": 1}, "followed_by_add": 1}
    assert uselog.measure(uselog.read(p), ask_kb.grounded, under="d:/other")["retrieval"]["searches"] == 0
    # 0건 검색과 주소를 적지 않던 때의 줄(v 없음)은 세지 않는다
    old = [{k: v for k, v in r.items() if k != "v"} for r in uselog.read(p)]
    assert uselog.measure(old, ask_kb.grounded)["retrieval"] == {"searches": 0, "found": 0, "hook_hits": 0}


def test_unused_lists_aged_additions_that_were_never_found_again(tmp_path):
    p = tmp_path / "use.jsonl"
    _log(p, "add", "2026-11-01", [(1, "질문 1")]); _log(p, "search", "2026-11-01", [(1, "질문 1")])  # 등록한 날의 검색뿐
    _log(p, "add", "2026-11-02", [(2, "질문 2")]); _log(p, "hook_hit", "2026-11-09", [(2, "질문 2")])
    _log(p, "add", "2026-11-20", [(3, "질문 3")])  # 아직 UNUSED_DAYS 가 지나지 않았다
    _log(p, "add", "2026-11-03", [(4, "질문 4")], cwd="d:/other")
    now = uselog.datetime(2026, 11, 25, tzinfo=uselog.timezone.utc)
    assert uselog.unused(uselog.read(p), now=now) == {"aged": 3, "unused": [(1, "질문 1"), (4, "질문 4")]}
    assert uselog.unused(uselog.read(p), under="d:/paas", now=now) == {"aged": 2, "unused": [(1, "질문 1")]}
    assert uselog.unused(uselog.read(p), since="2026-11-02", now=now)["aged"] == 2


def test_stats_command_prints_the_indicators(tmp_path, monkeypatch, capsys):
    from fq_client import cli
    p = tmp_path / "use.jsonl"
    monkeypatch.setenv("FQ_USELOG", str(p))
    _log(p, "search", "2026-11-05", [(1, "질문 1")])
    _log(p, "cite", "2026-11-05", [(1, "")])
    _log(p, "miss", "2026-11-05", queries=["넷퍼넬 호출처"])
    assert cli._stats(cli.parser().parse_args(["stats", "--since", "2026-11-01", "--until", "2026-11-10"])) == 0
    out = capsys.readouterr().out
    assert "1/2(50%)" in out and "1/1(100%)" in out and "0/1(0%)" in out and "'넷퍼넬 호출처'" in out
    assert cli._stats(cli.parser().parse_args(["stats", "--since", "2027-01-01", "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["retrieval"] == {"searches": 0, "found": 0, "hook_hits": 0}


def test_store_conditions_name_terms_that_were_searched_and_missing():
    plain = ask_kb.store_conditions("이관")
    missed = ask_kb.store_conditions("이관", None, [("RateLimitFilter", 3), ("ErrCode=1111", 1)])
    assert len(missed) == len(plain) + 1 and "'RateLimitFilter'(3일), 'ErrCode=1111'" in missed[-1]
    turn = stop_harvest.context("이관", turn_missed=[("넷퍼넬 호출처", 1)])
    assert "이 턴에서 kb_search 로 찾았으나 없었던 검색어: '넷퍼넬 호출처'" in turn and "여러 번 읽어" not in turn
