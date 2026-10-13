"""검색. E 정확히 일치 → Y 동의어 → M 형태소 계층을 합치고, 합친 결과가 0건이면 F 비슷한 결과를
따로 돌려준다. 강조 위치는 서버가 원문 기준으로 계산한다.

검색에 쓰는 열은 프로세스 메모리 사본(_corpus)에서 읽는다. 사본은 revision 최대 id 와 qa 최대 id 가
바뀌면 다시 읽는다. 모든 쓰기가 revision 을 남기므로 다른 프로세스가 쓴 변경도 다음 검색에서 반영된다.
3글자 이상 단어는 trigram 색인(qa_fts MATCH)으로, 2글자 이하와 필터·비슷한 결과는 사본에서 찾는다.
질의 문법(OR, 제외, 구절, 질문·답변 한정, 근거 경로)은 kb/query.py 가 해석한다. 정규식은 regex=True 로만 쓴다.
"""
import re
import threading
import time
import unicodedata
from typing import TYPE_CHECKING, NamedTuple

from kb.dictionary import load_stopwords, load_synonyms
from kb.errors import ValidationError
from kb.query import Query, Term, _tokens, parse

if TYPE_CHECKING:
    from kb.store import Store

TIER_RANK = {"exact": 0, "synonym": 1, "morph": 2}
SIMILAR_MIN = 0.5
SIMILAR_LIMIT = 20
RECENT_LIMIT = 50
REGEX_MAX = 200
# (a+)+ 처럼 반복 안에 반복이 든 패턴은 역추적으로 검색 시간이 끝없이 늘 수 있다
_NESTED_REPEAT = re.compile(r"\([^()]*[+*][^()]*\)\s*[+*{]")
FTS_COLUMNS = {"q": "{question question_ns}", "a": "{answer answer_ns}"}


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


def _field_flat(d: Doc, field: str | None) -> str:
    if field is None:
        return d.flat
    q, a = d.flat.split("\0", 1)
    return q if field == "q" else a


def _field_text(d: Doc, field: str | None, sep: str = "\0") -> str:
    return d.question if field == "q" else d.answer if field == "a" else d.question + sep + d.answer


def _match_ids(conn, term: str, field: str | None = None) -> set[int]:
    phrase = '"' + term.replace('"', '""') + '"'
    if field:
        phrase = f"{FTS_COLUMNS[field]} : {phrase}"
    return {r[0] for r in conn.execute("SELECT rowid FROM qa_fts WHERE qa_fts MATCH ?", (phrase,))}


def _term_ids(conn, docs: dict[int, Doc], term: Term) -> set[int]:
    if len(term.text) >= 3:  # trigram 색인은 3글자 이상만 찾는다
        return _match_ids(conn, term.text, term.field)
    t = _flat(term.text)
    return {i for i, d in docs.items() if t in _field_flat(d, term.field)}


def _all_of(conn, docs, groups: list[list[Term]]) -> set[int]:
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


def _path_ids(conn, alts: list[str]) -> set[int]:
    """보관되지 않은 근거의 경로·저장소·경로 목록에 대안 중 하나가 든 Q&A. 구분자는 / 로 맞춰 비교한다."""
    found: set[int] = set()
    for a in alts:
        like = "%" + a.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        found |= {r[0] for r in conn.execute(
            "SELECT qa_id FROM evidence WHERE deleted_at IS NULL AND ("
            "lower(replace(ifnull(path, ''), char(92), '/')) LIKE ?1 ESCAPE '!' OR "
            "lower(replace(ifnull(repo, ''), char(92), '/')) LIKE ?1 ESCAPE '!' OR "
            "lower(ifnull(paths_json, '')) LIKE ?1 ESCAPE '!')", (like,))}
    return found


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


def _regex(q: str, case_sensitive: bool) -> re.Pattern:
    if len(q) > REGEX_MAX:
        raise ValidationError(f"정규식은 {REGEX_MAX}자까지 쓸 수 있다. 지금 {len(q)}자다.")
    if _NESTED_REPEAT.search(q):
        raise ValidationError("정규식에 중첩 반복((a+)+ 같은 형태)을 쓸 수 없다. 검색 시간이 끝없이 늘 수 있다.")
    try:
        return re.compile(q, 0 if case_sensitive else re.IGNORECASE)
    except re.error as e:
        raise ValidationError(f"정규식이 올바르지 않다: {e}") from e


def search(store: "Store", query: str, *, analyzer=None, category_id: int | None = None,
           include_archived: bool = False, case_sensitive: bool = False, regex: bool = False,
           limit: int = 200, relax: bool = False) -> dict:
    """relax 면 결과에 relaxed 를 붙인다. 0건이고 단어가 둘 이상일 때 단어를 하나씩 뺀 질의 중
    결과가 있는 것의 건수다(화면의 0건 안내가 쓴다)."""
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
    patterns: dict[str, list[tuple[str | None, re.Pattern]]] = {}  # 계층별 (범위, 강조 패턴)
    morph_terms: dict[str | None, list[str]] = {}  # 범위별 강조할 형태소
    parsed = Query() if regex else parse(q)
    groups = parsed.groups
    if regex:
        rx = _regex(q, case_sensitive)
        tier_of = {i: "exact" for i, d in docs.items() if rx.search(d.question) or rx.search(d.answer)}
        patterns["exact"] = [(None, rx)]
    elif groups or parsed.paths:
        base = set(docs)
        for alts in parsed.paths:
            base &= _path_ids(conn, alts)
        excluded: set[int] = set()
        for t in parsed.excludes:
            excluded |= _term_ids(conn, docs, t)

        def narrow(ids) -> set[int]:
            return (set(ids) & base) - excluded

        patterns["exact"] = [(t.field, p) for g in groups for t in g if (p := _pattern(t.text, case_sensitive))]
        tier_of = dict.fromkeys(narrow(_all_of(conn, docs, groups) if groups else base), "exact")
        syn = load_synonyms(conn)
        wide = [list(dict.fromkeys(Term(x, t.field) for t in g for x in [t.text] + syn.get(t.text.lower(), [])))
                for g in groups]
        if any(len(w) > len(g) for w, g in zip(wide, groups)):
            patterns["synonym"] = [(t.field, p) for g in wide for t in g if (p := _pattern(t.text, False))]
            for i in narrow(_all_of(conn, docs, wide)):
                tier_of.setdefault(i, "synonym")
        if morph_on and groups:
            for i in narrow(_morph_ids(conn, analyzer, docs, q, parsed, morph_terms)):
                tier_of.setdefault(i, "morph")

    hits = [i for i in tier_of if i in docs and ok(docs[i])]
    if case_sensitive and not regex:
        def has_all(d: Doc) -> bool:
            def has(t: Term) -> bool:
                text = _field_text(d, t.field)
                return t.text in text or t.text in "".join(text.split())
            return all(any(has(t) for t in g) for g in groups)
        hits = [i for i in hits if tier_of[i] != "exact" or has_all(docs[i])]

    def scoped(pats, field: str) -> list[re.Pattern]:
        return [p for f, p in pats if f in (None, field)]

    def morphs_for(field: str) -> list[str]:
        return morph_terms.get(None, []) + morph_terms.get(field, [])

    def in_question(i: int) -> bool:
        tier = tier_of[i]
        if tier == "morph":
            return analyzer.contains_any(docs[i].question, morphs_for("q"))
        return any(p.search(docs[i].question) for p in scoped(patterns.get(tier, []), "q"))

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
        spans = {}
        for k, f in (("question", "q"), ("answer", "a")):
            if tier == "morph":
                spans[k] = analyzer.spans(item[k], morphs_for(f))
            else:
                spans[k] = _spans(item[k], scoped(patterns.get(tier, []), f))
        result["hits"].append(dict(item, tier=tier, in_question=flags[item["id"]], spans=spans))

    if not hits and groups:
        result["similar"] = _similar(store, docs, ok, " ".join(parsed.words()), analyzer if morph_on else None)
    if relax:
        result["relaxed"] = [] if hits or regex else _relaxed(
            store, q, analyzer=analyzer, category_id=category_id, include_archived=include_archived,
            case_sensitive=case_sensitive)
    return done()


def _relaxed(store: "Store", q: str, **opts) -> list[dict]:
    """질의 토큰(단독 | 는 빼고)을 하나씩 뺀 질의마다 건수를 센다. 건수가 0 인 것은 버린다."""
    tokens = [t for t, _ in _tokens(q)]
    words = [i for i, t in enumerate(tokens) if t != "|"]
    if len(words) < 2:
        return []
    out = []
    for i in words:
        rest = " ".join(t for j, t in enumerate(tokens) if j != i)
        total = search(store, rest, limit=0, **opts)["total"]
        if total:
            out.append({"dropped": tokens[i], "query": rest, "total": total})
    return out


def _morph_ids(conn, analyzer, docs: dict[int, Doc], q: str, parsed: Query,
               morph_terms: dict[str | None, list[str]]) -> set[int]:
    """형태소 계층. 연산자 없는 질의는 질의 전체를 한 번에 분석해 내용 형태소를 모두 AND 로 찾는다.
    연산자가 있으면 대안마다 분석해 그룹은 AND, 대안은 OR, 제외 단어는 NOT 으로 찾는다.
    질문·답변 한정 대안은 색인에 범위가 없으므로 후보를 그 범위의 형태소로 다시 확인한다.
    morph_terms 에 범위별 강조할 형태소를 채운다."""
    stop = load_stopwords(conn)

    def content(text: str) -> list[str]:
        return [m for m in analyzer.query_morphs(text) if m not in stop]

    if parsed.plain:
        groups = [[(None, ms)] for ms in [content(q)] if ms]
    else:
        groups = [alts for g in parsed.groups if (alts := [(t.field, ms) for t in g if (ms := content(t.text))])]
    if not groups:
        return set()
    for g in groups:
        for f, ms in g:
            have = morph_terms.setdefault(f, [])
            have += [m for m in ms if m not in have]
    excludes = [ms for t in parsed.excludes if (ms := content(t.text))]
    found = analyzer.match_expr(conn, [[ms for _, ms in g] for g in groups], excludes)
    if not any(f for g in groups for f, _ in g):
        return found

    cache: dict[tuple[int, str | None], set[str]] = {}

    def morphs_of(i: int, f: str | None) -> set[str]:
        if (i, f) not in cache:
            cache[(i, f)] = set(analyzer.morphs(_field_text(docs[i], f, "\n")))
        return cache[(i, f)]

    return {i for i in found if i in docs and all(any(set(ms) <= morphs_of(i, f) for f, ms in g) for g in groups)}


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


def search_many(store: "Store", queries: list[str], *, analyzer=None, limit: int = 200, **opts) -> dict:
    """여러 표현을 한 번에 찾는다. 항목마다 걸린 질의(matched)를 붙이고, 계층 → 걸린 질의 수 →
    질문에서 일치 → 최근 수정 순으로 정렬한다. 모두 0건이면 비슷한 결과를 합쳐 돌려준다."""
    t0 = time.perf_counter()
    qs = [q for q in dict.fromkeys(unicodedata.normalize("NFC", q or "").strip() for q in queries) if q]
    if not qs:
        raise ValidationError("queries 에 검색어가 하나 이상 필요하다.")
    best: dict[int, dict] = {}
    matched: dict[int, list[str]] = {}
    similar: dict[int, dict] = {}
    morph = "off"
    for q in qs:
        res = search(store, q, analyzer=analyzer, limit=10**9, **opts)
        morph = "on" if res["morph"] == "on" else morph
        for h in res["hits"]:
            matched.setdefault(h["id"], []).append(q)
            cur = best.get(h["id"])
            if cur is None or (TIER_RANK[h["tier"]], not h["in_question"]) < (
                    TIER_RANK[cur["tier"]], not cur["in_question"]):
                best[h["id"]] = h
        for h in res["similar"]:
            if h["id"] not in similar or h["score"] > similar[h["id"]]["score"]:
                similar[h["id"]] = h
    order = sorted(best, key=lambda i: (best[i]["updated_at"], i), reverse=True)
    order.sort(key=lambda i: (TIER_RANK[best[i]["tier"]], -len(matched[i]), 0 if best[i]["in_question"] else 1))
    counts: dict[int, list] = {}
    for i in order:
        counts.setdefault(best[i]["category_id"], [best[i]["category_path"], 0])[1] += 1
    sim = [] if best else sorted(similar.values(), key=lambda h: (-h["score"], -h["id"]))[:SIMILAR_LIMIT]
    return {"queries": qs, "total": len(order),
            "groups": [{"category_id": c, "category_path": p, "count": n} for c, (p, n) in counts.items()],
            "hits": [dict(best[i], matched=matched[i]) for i in order[:limit]], "similar": sim,
            "morph": morph, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}
