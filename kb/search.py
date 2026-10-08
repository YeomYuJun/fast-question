"""검색. E 정확히 일치 → Y 동의어 → M 형태소 계층을 합치고, 합친 결과가 0건이면 F 비슷한 결과를
따로 돌려준다(spec §6.1). 강조 위치는 서버가 원문 기준으로 계산한다.
"""
import re
import time
import unicodedata
from typing import TYPE_CHECKING

from kb.dictionary import load_stopwords, load_synonyms
from kb.errors import ValidationError

if TYPE_CHECKING:
    from kb.store import Store

TIER_RANK = {"exact": 0, "synonym": 1, "morph": 2}
SIMILAR_MIN = 0.5
SIMILAR_LIMIT = 20
RECENT_LIMIT = 50
CHUNK = 30000  # SQLite 변수 개수 상한(32766) 아래


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _term_ids(conn, term: str) -> set[int]:
    if len(term) >= 3:  # trigram 색인은 3글자 이상만 찾는다
        phrase = '"' + term.replace('"', '""') + '"'
        return {r[0] for r in conn.execute("SELECT rowid FROM qa_fts WHERE qa_fts MATCH ?", (phrase,))}
    rows = conn.execute(
        "SELECT rowid FROM qa_fts WHERE question LIKE ?1 ESCAPE '\\' OR answer LIKE ?1 ESCAPE '\\' "
        "OR question_ns LIKE ?1 ESCAPE '\\' OR answer_ns LIKE ?1 ESCAPE '\\'", (_like(term),))
    return {r[0] for r in rows}


def _all_of(groups: list[list[str]], conn) -> set[int]:
    """단어 그룹마다 대안의 합집합을 구하고, 그룹끼리는 교집합."""
    ids: set[int] | None = None
    for alts in groups:
        found: set[int] = set()
        for a in alts:
            found |= _term_ids(conn, a)
        ids = found if ids is None else ids & found
        if not ids:
            return set()
    return ids or set()


def _filters(store: "Store", include_archived: bool, category_id: int | None, only: str | None):
    where, params = [], []
    if not include_archived:
        where.append("deleted_at IS NULL")
    if category_id is not None:
        cat_ids = [category_id] + [r[0] for r in store.conn.execute(
            "SELECT id FROM category WHERE parent_id = ?", (category_id,))]
        where.append(f"category_id IN ({','.join('?' * len(cat_ids))})")
        params += cat_ids
    if only == "unreviewed":
        where.append("reviewed_at IS NULL")
    elif only == "stale":
        where.append("evidence_state IN ('stale', 'unknown')")
    elif only is not None:
        raise ValidationError(f"알 수 없는 필터다: {only}")
    return where, params


def _rows(store: "Store", ids, where: list[str], params: list) -> dict[int, tuple]:
    ids = list(ids)
    out = {}
    for i in range(0, len(ids), CHUNK):
        part = ids[i:i + CHUNK]
        cond = " AND ".join(where + [f"id IN ({','.join('?' * len(part))})"])
        for r in store.conn.execute(
                f"SELECT id, question, answer, updated_at, category_id FROM qa WHERE {cond}", params + part):
            out[r[0]] = tuple(r)
    return out


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
    s = "".join(text.lower().split())
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def search(store: "Store", query: str, *, analyzer=None, category_id: int | None = None,
           include_archived: bool = False, case_sensitive: bool = False, regex: bool = False,
           only: str | None = None, limit: int = 200) -> dict:
    t0 = time.perf_counter()
    conn = store.conn
    q = unicodedata.normalize("NFC", query or "").strip()
    where, params = _filters(store, include_archived, category_id, only)
    morph_on = bool(analyzer is not None and analyzer.ready(conn))
    result = {"query": q, "total": 0, "groups": [], "hits": [], "similar": [],
              "morph": "on" if morph_on else "off", "elapsed_ms": 0.0}

    if not q:
        cond = " AND ".join(where) or "1"
        ids = [r[0] for r in conn.execute(
            f"SELECT id FROM qa WHERE {cond} ORDER BY updated_at DESC, id DESC LIMIT ?",
            params + [limit if only else RECENT_LIMIT])]
        result["hits"] = [dict(i, tier="recent", in_question=False, spans={"question": [], "answer": []})
                          for i in store.items(ids)]
        result["total"] = len(ids)
        result["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return result

    tier_of: dict[int, str] = {}
    patterns: dict[str, list[re.Pattern]] = {}
    morph_terms: list[str] = []
    terms = q.split()
    if regex:
        try:
            rx = re.compile(q, 0 if case_sensitive else re.IGNORECASE)
        except re.error as e:
            raise ValidationError(f"정규식이 올바르지 않다: {e}") from e
        cond = " AND ".join(where) or "1"
        for r in conn.execute(f"SELECT id, question, answer FROM qa WHERE {cond}", params):
            if rx.search(r[1]) or rx.search(r[2]):
                tier_of[r[0]] = "exact"
        patterns["exact"] = [rx]
    else:
        patterns["exact"] = [p for t in terms if (p := _pattern(t, case_sensitive))]
        for i in _all_of([[t] for t in terms], conn):
            tier_of[i] = "exact"
        syn = load_synonyms(conn)
        groups = [list(dict.fromkeys([t] + syn.get(t.lower(), []))) for t in terms]
        if any(len(g) > 1 for g in groups):
            patterns["synonym"] = [p for g in groups for a in g if (p := _pattern(a, False))]
            for i in _all_of(groups, conn):
                tier_of.setdefault(i, "synonym")
        if morph_on:
            stop = load_stopwords(conn)
            morph_terms = [m for m in analyzer.query_morphs(q) if m not in stop]
            for i in analyzer.match_all(conn, morph_terms):
                tier_of.setdefault(i, "morph")

    rows = _rows(store, tier_of, where, params)
    if case_sensitive and not regex:
        def has_all(r):
            text = r[1] + "\n" + r[2]
            flat = "".join(text.split())
            return all(t in text or t in flat for t in terms)
        rows = {i: r for i, r in rows.items() if tier_of[i] != "exact" or has_all(r)}

    def in_question(i: int) -> bool:
        tier = tier_of[i]
        if tier == "morph":
            return analyzer.contains_any(rows[i][1], morph_terms)
        return any(p.search(rows[i][1]) for p in patterns.get(tier, []))

    order = sorted(rows, key=lambda i: (rows[i][3], i), reverse=True)
    flags = {i: in_question(i) for i in order}
    order.sort(key=lambda i: (TIER_RANK[tier_of[i]], 0 if flags[i] else 1))

    paths = store._paths()
    counts: dict[int, int] = {}
    for i in order:
        counts[rows[i][4]] = counts.get(rows[i][4], 0) + 1
    result["groups"] = [{"category_id": c, "category_path": paths.get(c, ""), "count": n} for c, n in counts.items()]
    result["total"] = len(order)

    for item in store.items(order[:limit]):
        tier = tier_of[item["id"]]
        if tier == "morph":
            spans = {k: analyzer.spans(item[k], morph_terms) for k in ("question", "answer")}
        else:
            spans = {k: _spans(item[k], patterns.get(tier, [])) for k in ("question", "answer")}
        result["hits"].append(dict(item, tier=tier, in_question=flags[item["id"]], spans=spans))

    if not order and not regex:
        result["similar"] = _similar(store, q, where, params, analyzer if morph_on else None)
    result["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return result


def _similar(store: "Store", q: str, where: list[str], params: list, analyzer) -> list[dict]:
    grams = _bigrams(q)
    scores: dict[int, float] = {}
    cond = " AND ".join(where) or "1"
    for r in store.conn.execute(f"SELECT id, question, answer FROM qa WHERE {cond}", params):
        flat = "".join((r[1] + r[2]).lower().split())
        ratio = sum(g in flat for g in grams) / len(grams) if grams else 0.0
        if ratio >= SIMILAR_MIN:
            scores[r[0]] = ratio
    if analyzer is not None:
        for i, ratio in analyzer.match_ratio(store.conn, q).items():
            if ratio >= SIMILAR_MIN:
                scores[i] = max(scores.get(i, 0.0), ratio)
        allowed = set(_rows(store, scores, where, params))
        scores = {i: s for i, s in scores.items() if i in allowed}
    top = sorted(scores, key=lambda i: (-scores[i], -i))[:SIMILAR_LIMIT]
    return [dict(item, tier="similar", score=round(scores[item["id"]], 2), in_question=False,
                 spans={"question": [], "answer": []}) for item in store.items(top)]
