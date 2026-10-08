import time

import pytest

from kb.db import connect, get_meta
from kb.morph import Analyzer, compute_version, pending_count, reindex
from kb.store import Store
from tests.conftest import load_sample
from tests.test_search import CASES as BASE_CASES
from tests.test_search import synthetic

# 설계 문서(docs/specs/2026-10-08-qa-kb-design.md) 부록 A.2 의 형태소 계층 기대값. (E·Y 는 형태소 계층이 없을 때와 같아야 한다)
MORPH = {
    "볼륨이": {1, 3},
    "재 암호화": {6},
    "옮긴": {1, 8},
    "읽혀": {2},
    "정해진 용량": {3},
    "암호화했나": {6, 8},
    "붙어": {10},
}
# 형태소 계층이 있을 때 E·Y·M 이 0건이라 F 가 채워지는 질의
SIMILAR = {"원자젹": {1}, "다른 Pod 가 쓰다 만 파일을 읽음": {2}}


@pytest.fixture(scope="module")
def shared_analyzer():
    return Analyzer()


@pytest.fixture
def store(conn, shared_analyzer):
    s = Store(conn, "user", analyzer=shared_analyzer)
    load_sample(s)
    return s


def tiers(res, tier):
    return {h["id"] for h in res["hits"] if h["tier"] == tier}


@pytest.mark.parametrize("query, exact, synonym, _similar", BASE_CASES, ids=[c[0] for c in BASE_CASES])
def test_regression_with_morph(store, query, exact, synonym, _similar):
    res = store.search(query)
    assert res["morph"] == "on"
    assert tiers(res, "exact") == exact
    assert tiers(res, "synonym") == synonym
    if query == "썼을 때":  # 조정 목표: 불용어 '때' 를 빼면 2번이 형태소 계층에 들어온다
        assert 2 in tiers(res, "morph")
    else:
        assert tiers(res, "morph") == MORPH.get(query, set())
    assert {h["id"] for h in res["similar"]} == SIMILAR.get(query, set())


def test_morph_spans_point_to_inflected_form(store):
    hit = next(h for h in store.search("옮긴")["hits"] if h["id"] == 8)
    marked = [hit["answer"][s:e] for s, e in hit["spans"]["answer"]]
    assert marked == ["옮기"]


def test_xsn_compound_indexed(shared_analyzer, conn):
    shared_analyzer.ready(conn)
    assert shared_analyzer.index_text("재암호화한다").split() == ["암호", "암호화"]


def test_user_word_change_reaches_other_process(tmp_path):
    path = tmp_path / "kb.sqlite3"
    conn_a, conn_b = connect(path), connect(path)
    a, b = Analyzer(), Analyzer()
    web = Store(conn_a, "user", analyzer=a)
    mcp = Store(conn_b, "claude", analyzer=b)
    web.add_category("이관")
    web.add_qa("이관", "망연계 정책은 어디서 받나", "보안팀에서 엑셀로 받는다.")
    assert pending_count(conn_a) == 0
    before = a.version

    res = mcp.dict_action("user_word_add", word="망연계")
    assert res["reindex_needed"] is True
    assert get_meta(conn_a, "analyzer_version") == compute_version([("망연계", "NNP")])
    assert pending_count(conn_a) == 1

    assert a.ready(conn_a) is True and a.version != before  # 다른 프로세스가 바꾼 사전을 다시 읽는다
    assert reindex(conn_a, a) == 1
    assert pending_count(conn_a) == 0
    assert 1 in tiers(web.search("망연계"), "exact")
    conn_a.close()
    conn_b.close()


def test_search_without_kiwi_falls_back(conn):
    def broken():
        raise ImportError("kiwipiepy 를 불러오지 못했다")

    s = Store(conn, "user", analyzer=Analyzer(loader=broken))
    load_sample(s)
    res = s.search("용량")
    assert res["morph"] == "off"
    assert tiers(res, "exact") == {3}
    assert s.analyzer.error and "kiwipiepy" in s.analyzer.error
    assert pending_count(conn) == 11  # Kiwi 가 없는 동안 쓴 행은 재색인 대상


def test_empty_query_does_not_load_kiwi(conn):
    calls = []

    def counting():
        calls.append(1)
        raise ImportError("불러오면 안 된다")

    s = Store(conn, "user", analyzer=Analyzer(loader=counting))
    s.add_category("이관")
    s.search("")
    assert calls == []


def test_update_rewrites_morph_index(store):
    item = store.get_qa(10)
    store.update_qa(10, item["version"], answer="소스에 박힌 주소는 설정을 고쳐도 구 주소로 연결된다.")
    assert 10 not in tiers(store.search("붙어"), "morph")
    assert pending_count(store.conn) == 0


@pytest.mark.perf
def test_search_10k_with_morph_under_100ms(conn, shared_analyzer):
    synthetic(conn)
    s = Store(conn, "user", analyzer=shared_analyzer)
    reindex(conn, shared_analyzer)
    for q in ["용량", "원자적", "정해진 용량", "옮긴 파일", "없는단어"]:
        s.search(q)
        t0 = time.perf_counter()
        for _ in range(3):
            res = s.search(q)
        ms = (time.perf_counter() - t0) / 3 * 1000
        assert ms < 100, f"{q}: {ms:.1f} ms, {res['total']}건"
