import random
import time
import unicodedata

import pytest

from kb.store import Store
from tests.conftest import load_sample

# 설계 문서(docs/specs/2026-10-08-qa-kb-design.md) 부록 A.2 의 질의. 형태소 계층이 없을 때(analyzer=None)의 기대값.
# E 정확히 일치, Y 동의어로 추가, F 비슷한 결과(2글자 조각 겹침만. 형태소 OR 은 tests/test_morph.py 에서 본다)
CASES = [
    ("nas-tmp", {1}, set(), set()),
    ("원자적", {1}, set(), set()),
    ("getUsable", {3}, set(), set()),
    ("typehandler", {7}, set(), set()),
    ("용량", {3}, set(), set()),
    ("IV", {6}, set(), set()),
    ("볼륨이", set(), set(), {1, 3}),
    ("원자적이", {1}, set(), set()),
    ("PETRA 복호화", {5, 8}, set(), set()),
    ("여유공간", {3}, set(), set()),
    ("재 암호화", {8}, set(), set()),
    ("옮긴", set(), set(), set()),
    ("썼을 때", set(), set(), set()),
    ("임시파일", set(), {1, 2}, set()),
    ("라우팅 규칙", set(), {9}, set()),
    ("디플로이먼트", set(), {11}, set()),
    ("원자젹", set(), set(), {1}),
    ("다른 Pod 가 쓰다 만 파일을 읽음", set(), set(), set()),
    ("읽혀", set(), set(), set()),
    ("정해진 용량", set(), set(), {3}),
    ("암호화했나", set(), set(), {6, 8}),
    ("붙어", set(), set(), set()),
    ("바꾼", set(), set(), set()),
    ("ATOMIC_MOVE", {2}, set(), set()),
    ("PKCS#5", {6}, set(), set()),
    (".part", {2}, {1}, set()),
]


@pytest.fixture
def store(conn):
    s = Store(conn, "user")
    load_sample(s)
    return s


def tiers(res, tier):
    return {h["id"] for h in res["hits"] if h["tier"] == tier}


@pytest.mark.parametrize("query, exact, synonym, similar", CASES, ids=[c[0] for c in CASES])
def test_regression_without_morph(store, query, exact, synonym, similar):
    res = store.search(query)
    assert tiers(res, "exact") == exact
    assert tiers(res, "synonym") == synonym
    assert {h["id"] for h in res["similar"]} == similar
    assert res["morph"] == "off"


@pytest.mark.parametrize(
    "query, expected",
    [('"', {6}), ('nas"tmp', set()), ("*", {7}), ("(", {3}), ("NEAR", set()), ("%", set()),
     ("_", {2, 7}), ("AND OR NOT", set()), ("a*b", set())],
)
def test_special_characters_are_literal(store, query, expected):
    assert {h["id"] for h in store.search(query)["hits"]} == expected


def test_nfd_query_matches_nfc_data(store):
    assert {h["id"] for h in store.search(unicodedata.normalize("NFD", "용량"))["hits"]} == {3}


def test_archived_hidden_unless_requested(store):
    item = store.get_qa(3)
    store.archive_qa(3, item["version"])
    assert store.search("용량")["hits"] == []
    assert [h["id"] for h in store.search("용량", include_archived=True)["hits"]] == [3]


def test_spans_cover_spaced_original(store):
    hit = store.search("여유공간")["hits"][0]
    q = hit["question"]
    (s, e), = hit["spans"]["question"]
    assert q[s:e] == "여유 공간"


def test_synonym_spans_point_to_alternative(store):
    hit = next(h for h in store.search("라우팅 규칙")["hits"] if h["id"] == 9)
    marked = [hit["question"][s:e] for s, e in hit["spans"]["question"]]
    assert marked == ["라우팅", "룰"]


def test_question_hits_first_and_groups(store):
    res = store.search("NAS")
    ids = [h["id"] for h in res["hits"]]
    assert set(ids[:2]) == {1, 4} and ids[2] == 3
    assert res["groups"] == [{"category_id": 2, "category_path": "이관 > NAS", "count": 3}]
    assert res["total"] == 3


def test_case_sensitive_option(store):
    assert {h["id"] for h in store.search("nas", case_sensitive=True)["hits"]} == {1}
    assert {h["id"] for h in store.search("NAS", case_sensitive=True)["hits"]} == {1, 3, 4}


def test_regex_option(store):
    res = store.search(r"\(\d{3}\)", regex=True)
    assert {h["id"] for h in res["hits"]} == {3}
    (s, e), = res["hits"][0]["spans"]["question"]
    assert res["hits"][0]["question"][s:e] == "(507)"


def test_bad_regex_is_validation_error(store):
    from kb.errors import ValidationError
    with pytest.raises(ValidationError, match="정규식"):
        store.search("(", regex=True)


def test_category_filter_includes_children(store):
    top = store.resolve_category("이관")
    nas = store.resolve_category("이관 > NAS")
    assert {h["id"] for h in store.search("이유", category_id=nas)["hits"]} == {1, 2, 3}
    assert {h["id"] for h in store.search("이유", category_id=top)["hits"]} == {1, 2, 3, 10}


def test_empty_query_lists_recent_and_filters(store, conn):
    res = store.search("")
    assert len(res["hits"]) == 11 and all(h["tier"] == "recent" for h in res["hits"])
    claude = Store(conn, "claude")
    claude.add_qa("일반 지식 > k8s", "Service 는 무엇인가", "Pod 집합에 고정 주소를 주는 객체다.")
    assert [h["question"] for h in store.search("", only="unreviewed")["hits"]] == ["Service 는 무엇인가"]


WORDS = ("NAS 볼륨 용량 rename 원자적 경로 암호화 ARIA PETRA 복호화 이관 방화벽 설정 Pod 쓰기 확정 임시 "
         "파일 삭제 검사 이유 원인 S3 Swift 오브젝트 스토리지 DB IV TypeHandler 매퍼 게이트웨이 라우팅 "
         "도메인 CORS 토픽 Kafka 빌드 기동 마운트").split()


def synthetic(conn, n=10_000):
    rnd = random.Random(1)
    sent = lambda k: " ".join(rnd.choice(WORDS) for _ in range(k))  # noqa: E731
    conn.execute("INSERT INTO category(name, created_by, updated_by) VALUES ('합성', 'system', 'system')")
    conn.execute("BEGIN")
    conn.executemany(
        "INSERT INTO qa(category_id, question, answer, created_by, updated_by) VALUES (1, ?, ?, 'system', 'system')",
        ((sent(8)[:150], sent(30)[:400]) for _ in range(n)))
    conn.execute("COMMIT")


@pytest.mark.perf
def test_search_10k_under_100ms(conn):
    synthetic(conn)
    s = Store(conn, "user")
    s.dict_action("synonym_add", terms=["임시파일", "임시"])
    for q in ["용량", "원자적", "여유공간", "PETRA 복호화", "임시파일", "IV DB"]:
        s.search(q)
        t0 = time.perf_counter()
        for _ in range(3):
            res = s.search(q)
        ms = (time.perf_counter() - t0) / 3 * 1000
        assert ms < 100, f"{q}: {ms:.1f} ms, {res['total']}건"
