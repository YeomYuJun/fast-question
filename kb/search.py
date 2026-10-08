"""검색. E 정확히 일치 → Y 동의어 → M 형태소 계층을 합치고, 합친 결과가 0건이면 F 비슷한 결과를
따로 돌려준다. 강조 위치는 서버가 원문 기준으로 계산한다.

검색에 쓰는 열은 프로세스 메모리 사본(_corpus)에서 읽는다. 사본은 revision 최대 id 와 qa 최대 id 가
바뀌면 다시 읽는다. 모든 쓰기가 revision 을 남기므로 다른 프로세스가 쓴 변경도 다음 검색에서 반영된다.
3글자 이상 단어는 trigram 색인(qa_fts MATCH)으로, 2글자 이하와 필터·비슷한 결과는 사본에서 찾는다.
"""
import re
import threading
import time
import unicodedata
from typing import TYPE_CHECKING, NamedTuple

from kb.dictionary import load_stopwords, load_synonyms
from kb.errors import ValidationError

if TYPE_CHECKING:
    from kb.store import Store

TIER_RANK = {"exact": 0, "synonym": 1, "morph": 2}
SIMILAR_MIN = 0.5
SIMILAR_LIMIT = 20
RECENT_LIMIT = 50


class Doc(NamedTuple):
    question: str
    answer: str
    flat: str  # 공백을 지우고 소문자로 바꾼 질문 + \0 + 답변
    updated_at: str
    category_id: int
    archived: bool


_CACHE: dict[str, tuple[tuple, dict[int, Doc]]] = {}
_LOCK = threading.Lock()


def _flat(text: str) -> str:
    return "".join(text.split()).lower()


def _corpus(conn) -> dict[int, Doc]:
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    stamp = tuple(conn.execute(
        "SELECT (SELECT ifnull(max(id), 0) FROM revision), (SELECT ifnull(max(id), 0) FROM qa)").fetchone())
    cached = _CACHE.get(db)
    if cached and cached[0] == stamp:
        return cached[1]
    with _LOCK:
        docs = {
            r[0]: Doc(r[1], r[2], _flat(r[1]) + "\0" + _flat(r[2]), r[3], r[4], bool(r[5]))
            for r in conn.execute(
                "SELECT id, question, answer, updated_at, category_id, deleted_at IS NOT NULL FROM qa")
        }
        _CACHE[db] = (stamp, docs)
    return docs


def _match_ids(conn, term: str) -> set[int]:
    phrase = '"' + term.replace('"', '""') + '"'
    return {r[0] for r in conn.execute("SELECT rowid FROM qa_fts WHERE qa_fts MATCH ?", (phrase,))}


def _term_ids(conn, docs: dict[int, Doc], term: str) -> set[int]:
    if len(term) >= 3:  # trigram 색인은 3글자 이상만 찾는다
        return _match_ids(conn, term)
    t = term.lower()
    return {i for i, d in docs.items() if t in d.flat}


def _all_of(conn, docs, groups: list[list[str]]) -> set[int]:
    """단어 그룹마다 대안의 합집합을 구하고, 그룹끼리는 교집합."""
    ids: set[int] | None = None
    for alts in groups:
        found: set[int] = set()
        for a in alts:
            found |= _term_ids(conn, docs, a)
        ids = found if ids is None else ids & found
        if not ids:
            return set()
    return ids or set()


def _filter(store: "Store", include_archived: bool, category_id: int | None):
    cats = None
    if category_id is not None:
        cats = {category_id} | {r[0] for r in store.conn.execute(
            "SELECT id FROM category WHERE parent_id = ?", (category_id,))}

    def ok(d: Doc) -> bool:
        if d.archived and not include_archived:
            return False
        return cats is None or d.category_id in cats
    return ok


def _pattern(term: str, case_sensitive: bool) -> re.Pattern | None:
    chars = [c for c in term if not c.isspace()]
    if not chars:
        return None
    # 띄어쓰기 무시로 일치한 경우("여유공간" ↔ "여유 공간")도 원문 위치를 잡는다
    return re.compile(r"\s*".join(map(re.escape, chars)), 0 if case_sensitive else re.IGNORECASE)


def _spans(text: str, patterns: list[re.Pattern]) -> list[list[int]]:
    found = sorted((m.start(), m.end()) for p in patterns for m in p.finditer(text) if m.end() > m.start())
    merged: list[list[int]] = []
    for s, e in found:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def _bigrams(text: str) -> set[str]:
    s = _flat(text)
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def search(store: "Store", query: str, *, analyzer=None, category_id: int | None = None,
           include_archived: bool = False, case_sensitive: bool = False, regex: bool = False,
           limit: int = 200) -> dict:
    t0 = time.perf_counter()
    conn = store.conn
    q = unicodedata.normalize("NFC", query or "").strip()
    ok = _filter(store, include_archived, category_id)
    # 빈 검색(최근 목록)에는 형태소가 필요 없으므로 Kiwi 를 띄우지 않는다
    morph_on = bool(q and analyzer is not None and analyzer.ready(conn))
    result = {"query": q, "total": 0, "groups": [], "hits": [], "similar": [],
              "morph": "on" if morph_on else "off", "elapsed_ms": 0.0}
    docs = _corpus(conn)

    def done() -> dict:
        result["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return result

    if not q:
        ids = sorted((i for i, d in docs.items() if ok(d)), key=lambda i: (docs[i].updated_at, i), reverse=True)
        ids = ids[:min(limit, RECENT_LIMIT)]
        result["hits"] = [dict(i, tier="recent", in_question=False, spans={"question": [], "answer": []})
                          for i in store.items(ids)]
        result["total"] = len(ids)
        return done()

    tier_of: dict[int, str] = {}
    patterns: dict[str, list[re.Pattern]] = {}
    morph_terms: list[str] = []
    terms = q.split()
    if regex:
        try:
            rx = re.compile(q, 0 if case_sensitive else re.IGNORECASE)
        except re.error as e:
            raise ValidationError(f"정규식이 올바르지 않다: {e}") from e
        tier_of = {i: "exact" for i, d in docs.items() if rx.search(d.question) or rx.search(d.answer)}
        patterns["exact"] = [rx]
    else:
        patterns["exact"] = [p for t in terms if (p := _pattern(t, case_sensitive))]
        tier_of = dict.fromkeys(_all_of(conn, docs, [[t] for t in terms]), "exact")
        syn = load_synonyms(conn)
        groups = [list(dict.fromkeys([t] + syn.get(t.lower(), []))) for t in terms]
        if any(len(g) > 1 for g in groups):
            patterns["synonym"] = [p for g in groups for a in g if (p := _pattern(a, False))]
            for i in _all_of(conn, docs, groups):
                tier_of.setdefault(i, "synonym")
        if morph_on:
            stop = load_stopwords(conn)
            morph_terms = [m for m in analyzer.query_morphs(q) if m not in stop]
            for i in analyzer.match_all(conn, morph_terms):
                tier_of.setdefault(i, "morph")

    hits = [i for i in tier_of if i in docs and ok(docs[i])]
    if case_sensitive and not regex:
        def has_all(d: Doc) -> bool:
            text = d.question + "\0" + d.answer
            flat = "".join(text.split())
            return all(t in text or t in flat for t in terms)
        hits = [i for i in hits if tier_of[i] != "exact" or has_all(docs[i])]

    def in_question(i: int) -> bool:
        tier = tier_of[i]
        if tier == "morph":
            return analyzer.contains_any(docs[i].question, morph_terms)
        return any(p.search(docs[i].question) for p in patterns.get(tier, []))

    hits.sort(key=lambda i: (docs[i].updated_at, i), reverse=True)
    flags = {i: in_question(i) for i in hits}
    hits.sort(key=lambda i: (TIER_RANK[tier_of[i]], 0 if flags[i] else 1))

    paths = store._paths()
    counts: dict[int, int] = {}
    for i in hits:
        counts[docs[i].category_id] = counts.get(docs[i].category_id, 0) + 1
    result["groups"] = [{"category_id": c, "category_path": paths.get(c, ""), "count": n} for c, n in counts.items()]
    result["total"] = len(hits)

    for item in store.items(hits[:limit]):
        tier = tier_of[item["id"]]
        if tier == "morph":
            spans = {k: analyzer.spans(item[k], morph_terms) for k in ("question", "answer")}
        else:
            spans = {k: _spans(item[k], patterns.get(tier, [])) for k in ("question", "answer")}
        result["hits"].append(dict(item, tier=tier, in_question=flags[item["id"]], spans=spans))

    if not hits and not regex:
        result["similar"] = _similar(store, docs, ok, q, analyzer if morph_on else None)
    return done()


def _similar(store: "Store", docs: dict[int, Doc], ok, q: str, analyzer) -> list[dict]:
    """2글자 조각 겹침율과 형태소 OR 일치율 중 큰 값이 0.5 이상인 항목."""
    grams = list(_bigrams(q))
    scores: dict[int, float] = {}
    if grams:
        need = len(grams) * SIMILAR_MIN
        for i, d in docs.items():
            n = sum(g in d.flat for g in grams)
            if n >= need and ok(d):
                scores[i] = n / len(grams)
    if analyzer is not None:
        for i, r in analyzer.match_ratio(store.conn, q).items():
            if r >= SIMILAR_MIN and i in docs and ok(docs[i]):
                scores[i] = max(scores.get(i, 0.0), r)
    top = sorted(scores, key=lambda i: (-scores[i], -i))[:SIMILAR_LIMIT]
    return [dict(item, tier="similar", score=round(scores[item["id"]], 2), in_question=False,
                 spans={"question": [], "answer": []}) for item in store.items(top)]
