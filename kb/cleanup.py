"""정리 축: 쌓인 Q&A 에서 고칠 것을 찾아 cleanup_finding 표에 둔다. 고치는 것은 Claude 다(MCP kb_cleanup_list·kb_cleanup_resolve).

탐지(scan)는 주기 작업(kb/jobs.py)이 하루 한 번 돌린다. 로컬 모드는 그 DB, 원격 모드는 사용자 DB 마다 돈다.
DB 내용만 보므로 두 모드의 동작이 같다.
- duplicate: 살아 있는 항목끼리 같은 대분류에서 질문 2글자 조각 겹침(dice)이 SIMILAR_MIN 이상, 또는 같은 근거
  (파일·저장소 경로와 anchor)를 단 두 항목이 SHARED_EVIDENCE_MIN 이상. qa_id 는 나중에 등록한 쪽이다.
- rule_violation: 질문·답을 마지막으로 쓴 주체가 claude 인 항목에 kb.rules.problems 를 다시 적용한다.
  사람이 웹에서 고친 항목은 형식 규칙 대상이 아니라서 뺀다.
마지막 탐지 뒤 바뀐 항목(qa 또는 그 근거의 수정 시각)만 다시 본다. 첫 탐지와 탐지 기준(형식 규칙은 kb/rules.py,
중복은 유사도 기준값)이 바뀐 규칙은 전부 본다.
조건이 사라진 열린 건은 system 이 gone 으로 닫는다. Claude 가 kept 로 닫은 건은 질문·답이 그대로면 다시 만들지 않는다.
자동 작업은 Q&A 를 고치거나 보관하지 않는다.
근거 상태(stale·unknown)는 qa.evidence_state 에 이미 있어 표에 두지 않고 open_list 가 함께 돌려준다.
"""
import hashlib
import inspect
import json
import sqlite3
from collections import Counter, defaultdict
from fractions import Fraction
from itertools import combinations

from kb import rules
from kb.db import NOW, get_meta, set_meta, tx
from kb.errors import NotFoundError, ValidationError
from kb.text import SIMILAR_MIN, bigrams, dice

SHARED_EVIDENCE_MIN = 0.4  # 같은 근거·anchor 를 단 두 항목은 이 값부터 중복 후보다
EVIDENCE_RULES = {"stale": ("evidence_stale", "근거 파일이 기록한 뒤에 바뀌었다. 근거를 다시 읽고 답을 확인한다."),
                  "unknown": ("evidence_unknown", "근거를 판정하지 못했다(경로가 없어졌거나 git 을 실행하지 못했다).")}
RESOLUTIONS = ("fixed", "kept")
_CHUNK = 500  # IN (...) 자리표시자 수


def _fingerprint(parts: list) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def fingerprints() -> dict[str, str]:
    """규칙마다 탐지 기준의 지문. 바뀐 규칙만 다음 탐지가 전부를 다시 본다(중복 전수 비교는 비싸다).
    rule_violation 은 kb/rules.py 의 소스와 지금 쓰는 패턴, duplicate 는 유사도 기준값이다."""
    return {"rule_violation": _fingerprint([inspect.getsource(rules), [(k, rx.pattern) for k, rx, _ in rules.BANNED],
                                            [(k, rx.pattern) for k, rx in rules.SENSITIVE]]),
            "duplicate": _fingerprint([SIMILAR_MIN, SHARED_EVIDENCE_MIN])}


def _hash(*texts: str) -> str:
    return hashlib.sha256("\x00".join(texts).encode("utf-8")).hexdigest()[:16]


def _chunks(ids) -> list[list[int]]:
    ids = sorted(ids)
    return [ids[i:i + _CHUNK] for i in range(0, len(ids), _CHUNK)]


def _live(conn: sqlite3.Connection, ids=None) -> dict[int, sqlite3.Row]:
    sql = ("SELECT q.id, q.question, q.answer, q.version, ifnull(c.parent_id, c.id) AS top "
           "FROM qa q JOIN category c ON c.id = q.category_id WHERE q.deleted_at IS NULL")
    if ids is None:
        return {r["id"]: r for r in conn.execute(sql)}
    out = {}
    for part in _chunks(ids):
        out.update({r["id"]: r for r in conn.execute(f"{sql} AND q.id IN ({','.join('?' * len(part))})", part)})
    return out


def _changed(conn: sqlite3.Connection, since: str) -> set[int]:
    ids = {r[0] for r in conn.execute("SELECT id FROM qa WHERE updated_at >= ?", (since,))}
    return ids | {r[0] for r in conn.execute("SELECT qa_id FROM evidence WHERE updated_at >= ?", (since,))}


def _evidence_keys(conn: sqlite3.Connection, ids=None) -> dict[tuple, set[int]]:
    """(저장소, 경로, anchor) → 그 근거를 단 qa id. anchor 없는 근거는 파일 전체라 중복 판정에 쓰지 않는다."""
    sql = ("SELECT qa_id, ifnull(repo, '') AS repo, ifnull(paths_json, ifnull(path, '')) AS path, anchor "
           "FROM evidence WHERE deleted_at IS NULL AND kind NOT IN ('url', 'note') AND ifnull(anchor, '') != ''")
    rows = conn.execute(sql).fetchall() if ids is None else [
        r for part in _chunks(ids) for r in conn.execute(f"{sql} AND qa_id IN ({','.join('?' * len(part))})", part)]
    groups: dict[tuple, set[int]] = defaultdict(set)
    for r in rows:
        groups[(r["repo"], r["path"], r["anchor"])].add(r["qa_id"])
    return groups


def _pair(i: int, j: int) -> tuple[int, int]:
    return max(i, j), min(i, j)


def _shared_detail(key: tuple, score: float) -> str:
    return f"같은 근거(anchor '{key[2]}')를 단 항목과 질문 유사도 {score:.2f}"


def _duplicates(conn: sqlite3.Connection, live: dict, targets: set[int]) -> dict[tuple[int, int], str]:
    """targets 의 각 항목과 같은 대분류의 모든 항목을 비교한다. 쌍마다 dice 를 다 계산하지 않도록
    접두어 거르기를 쓴다: dice(A,B) >= t 이면 겹침 >= t|A|/(2-t) 이므로, 조각을 전체 빈도 순으로 늘어놓은
    앞부분(|A| - 최소 겹침 + 1개)끼리는 반드시 겹친다. 그 앞부분을 색인해 후보만 계산한다."""
    t = Fraction(SIMILAR_MIN).limit_denominator(1000)
    num, den = t.numerator, t.denominator  # 두 집합 크기의 비가 t/(2-t) 보다 작으면 dice 가 t 에 못 미친다
    grams = {i: bigrams(r["question"]) for i, r in live.items()}
    size = {i: len(s) for i, s in grams.items()}
    freq = Counter(g for s in grams.values() for g in s)
    index: dict[tuple, list[int]] = defaultdict(list)
    prefix: dict[int, list[str]] = {}
    for i in sorted(grams):
        toks = sorted(grams[i], key=lambda g: (freq[g], g))
        need = -(-(t * len(toks)) // (2 - t))  # 올림
        prefix[i] = toks[:len(toks) - int(need) + 1]
        for g in prefix[i]:
            index[(live[i]["top"], g)].append(i)
    out: dict[tuple[int, int], str] = {}
    every = targets >= live.keys()  # 전수면 쌍을 한 번만(작은 id 쪽에서) 본다
    for i in targets:
        if i not in live:
            continue
        a, la = grams[i], size[i]
        cands = {j for g in prefix[i] for j in index[(live[i]["top"], g)]}
        for j in cands:
            if j == i or (every and j > i):
                continue
            lb = size[j]
            if min(la, lb) * (2 * den - num) < num * max(la, lb):
                continue
            score = 2 * len(a & grams[j]) / (la + lb)
            if score >= SIMILAR_MIN:
                out[_pair(i, j)] = f"같은 대분류의 항목과 질문 유사도 {score:.2f}"
    for key, ids in _evidence_keys(conn).items():
        ids = sorted(ids & live.keys())
        for i, j in combinations(ids, 2):
            if (i in targets or j in targets) and _pair(i, j) not in out:
                score = dice(grams[i], grams[j])
                if score >= SHARED_EVIDENCE_MIN:
                    out[_pair(i, j)] = _shared_detail(key, score)
    return out


def _content_writers(conn: sqlite3.Connection, ids) -> dict[int, str]:
    """항목마다 질문·답을 마지막으로 쓴 주체(revision 기준)."""
    sql = ("SELECT row_id, actor FROM revision WHERE table_name = 'qa' AND (op = 'insert' "
           "OR json_extract(before_json, '$.question') IS NOT json_extract(after_json, '$.question') "
           "OR json_extract(before_json, '$.answer') IS NOT json_extract(after_json, '$.answer'))")
    out: dict[int, str] = {}
    for part in _chunks(ids):
        for r in conn.execute(f"{sql} AND row_id IN ({','.join('?' * len(part))}) ORDER BY id", part):
            out[r["row_id"]] = r["actor"]
    return out


def _violations(conn: sqlite3.Connection, live: dict, targets: set[int]) -> dict[int, str]:
    ids = targets & live.keys()
    writers = _content_writers(conn, ids)
    out = {}
    for i in ids:
        if writers.get(i) != "claude":
            continue
        found = rules.problems(live[i]["question"], live[i]["answer"])
        if found:
            out[i] = " / ".join(found)
    return out


def _content(live: dict, qa_id: int, other: int | None) -> str:
    parts = [live[qa_id]["question"], live[qa_id]["answer"]]
    if other is not None:
        parts += [live[other]["question"], live[other]["answer"]]
    return _hash(*parts)


def scan(conn: sqlite3.Connection) -> dict:
    """탐지를 한 번 돌리고 열린 건 수와 이번에 새로 연 건·닫은 건 수를 돌려준다. 쓰기 주체는 system 이다."""
    at = conn.execute(f"SELECT {NOW}").fetchone()[0]
    since = get_meta(conn, "cleanup_scan_at")
    prints = fingerprints()
    full = [rule for rule, fp in prints.items() if since is None or get_meta(conn, f"cleanup_hash:{rule}") != fp]
    live = _live(conn)
    changed = set() if since is None else _changed(conn, since)
    targets = {rule: set(live) if rule in full else changed for rule in prints}
    found: dict[tuple, str] = {}
    for (i, j), detail in _duplicates(conn, live, targets["duplicate"]).items():
        found[(i, "duplicate", j)] = detail
    for i, detail in _violations(conn, live, targets["rule_violation"]).items():
        found[(i, "rule_violation", None)] = detail
    new = closed = 0
    with tx(conn) as c:
        for r in c.execute("SELECT id, qa_id, rule, other_qa_id FROM cleanup_finding WHERE resolved_at IS NULL").fetchall():
            key = (r["qa_id"], r["rule"], r["other_qa_id"])
            gone = r["qa_id"] not in live or (r["other_qa_id"] is not None and r["other_qa_id"] not in live)
            rechecked = r["qa_id"] in targets[r["rule"]] or r["other_qa_id"] in targets[r["rule"]]
            if gone or (rechecked and key not in found):
                c.execute(f"UPDATE cleanup_finding SET resolved_at = {NOW}, resolution = 'gone', resolved_by = 'system' "
                          "WHERE id = ?", (r["id"],))
                closed += 1
            elif key in found:
                qa_id, _, other = key
                c.execute("UPDATE cleanup_finding SET detail = ?, qa_version = ?, content_hash = ? WHERE id = ?",
                          (found.pop(key), live[qa_id]["version"], _content(live, qa_id, other), r["id"]))
        for (qa_id, rule, other), detail in found.items():
            content = _content(live, qa_id, other)
            kept = c.execute("SELECT 1 FROM cleanup_finding WHERE qa_id = ? AND rule = ? AND ifnull(other_qa_id, 0) = ? "
                             "AND resolution = 'kept' AND content_hash = ?", (qa_id, rule, other or 0, content)).fetchone()
            if kept:
                continue
            c.execute("INSERT INTO cleanup_finding(qa_id, other_qa_id, rule, detail, qa_version, content_hash) "
                      "VALUES (?,?,?,?,?,?)", (qa_id, other, rule, detail, live[qa_id]["version"], content))
            new += 1
        set_meta(c, "cleanup_scan_at", at)
        for rule, fp in prints.items():
            set_meta(c, f"cleanup_hash:{rule}", fp)
    counts = dict(conn.execute("SELECT rule, count(*) FROM cleanup_finding WHERE resolved_at IS NULL GROUP BY rule"))
    return {"duplicate": counts.get("duplicate", 0), "rule_violation": counts.get("rule_violation", 0),
            "new": new, "closed": closed, "full": sorted(full)}


# ---------- Claude 가 쓰는 조회·처리 ----------

FINDING_COLS = "id, qa_id, other_qa_id, rule, detail, found_at, resolved_at, resolution, resolved_by, note"


def _finding(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in FINDING_COLS.split(", ")}


def _brief(item: dict) -> dict:
    return {k: item[k] for k in ("id", "category_path", "question", "answer", "evidence_state", "version")}


def open_list(store, rule: str | None = None, limit: int = 20) -> dict:
    """열린 정리 건(표)과 근거 상태가 stale·unknown 인 항목. 근거 상태 건은 표에 없어 id 가 None 이다.
    항목마다 item(고칠 항목)과 중복이면 other(먼저 등록된 항목)를 붙인다."""
    conn = store.conn
    rows = conn.execute(
        f"SELECT f.{FINDING_COLS.replace(', ', ', f.')} FROM cleanup_finding f JOIN qa q ON q.id = f.qa_id "
        "WHERE f.resolved_at IS NULL AND q.deleted_at IS NULL ORDER BY f.id").fetchall()
    entries = [_finding(r) for r in rows]
    for state, (name, detail) in EVIDENCE_RULES.items():
        entries += [{"id": None, "qa_id": r[0], "other_qa_id": None, "rule": name, "detail": detail}
                    for r in conn.execute("SELECT id FROM qa WHERE deleted_at IS NULL AND evidence_state = ? ORDER BY id",
                                          (state,))]
    if rule:
        entries = [e for e in entries if e["rule"] == rule]
    page = entries[:max(0, int(limit))]
    items = {i["id"]: _brief(i) for i in store.items(sorted(
        {e["qa_id"] for e in page} | {e["other_qa_id"] for e in page if e["other_qa_id"]}))}
    out = []
    for e in page:
        entry = {"id": e["id"], "rule": e["rule"], "detail": e["detail"], "item": items.get(e["qa_id"])}
        if e.get("found_at"):
            entry["found_at"] = e["found_at"]
        if e["other_qa_id"]:
            entry["other"] = items.get(e["other_qa_id"])
        out.append(entry)
    return {"total": len(entries), "findings": out}


def open_rules(conn: sqlite3.Connection, ids: list[int]) -> dict[int, list[str]]:
    """항목 id → 그 항목이 걸린 열린 정리 규칙(중복은 두 항목 모두). 검색 결과 표시에 쓴다."""
    marks: dict[int, set[str]] = defaultdict(set)
    wanted = set(ids)
    for part in _chunks(wanted):
        ph = ",".join("?" * len(part))
        for r in conn.execute(f"SELECT qa_id, other_qa_id, rule FROM cleanup_finding WHERE resolved_at IS NULL "
                              f"AND (qa_id IN ({ph}) OR other_qa_id IN ({ph}))", part * 2):
            for i in (r["qa_id"], r["other_qa_id"]):
                if i in wanted:
                    marks[i].add(r["rule"])
    return {i: sorted(rs) for i, rs in marks.items()}


def _still_found(conn: sqlite3.Connection, r: sqlite3.Row) -> str | None:
    """열린 건의 조건이 지금도 맞으면 그 설명."""
    ids = [r["qa_id"]] + ([r["other_qa_id"]] if r["other_qa_id"] else [])
    live = _live(conn, ids)
    if any(i not in live for i in ids):
        return None
    if r["rule"] == "rule_violation":
        return _violations(conn, live, {r["qa_id"]}).get(r["qa_id"])
    a, b = live[r["qa_id"]], live[r["other_qa_id"]]
    score = dice(bigrams(a["question"]), bigrams(b["question"]))
    if a["top"] == b["top"] and score >= SIMILAR_MIN:
        return f"같은 대분류의 항목과 질문 유사도 {score:.2f}"
    for key, group in _evidence_keys(conn, ids).items():
        if set(ids) <= group and score >= SHARED_EVIDENCE_MIN:
            return _shared_detail(key, score)
    return None


def resolve(store, finding_id: int, resolution: str, note: str | None = None) -> dict:
    """열린 정리 건을 닫는다. fixed 는 조건을 다시 확인해 아직 맞으면 거부한다. kept 는 문제가 아니라고 본 것이다."""
    if resolution not in RESOLUTIONS:
        raise ValidationError(f"resolution 은 {', '.join(RESOLUTIONS)} 중 하나다: {resolution}")
    conn = store.conn
    row = conn.execute(f"SELECT {FINDING_COLS} FROM cleanup_finding WHERE id = ?", (finding_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"정리 건을 찾지 못했다: {finding_id}")
    if row["resolved_at"] is not None:
        raise ValidationError(f"이미 닫힌 정리 건이다({row['resolution']}): {finding_id}")
    if resolution == "fixed":
        still = _still_found(conn, row)
        if still:
            raise ValidationError(f"아직 정리 조건에 걸린다: {still}. 항목을 고치거나 보관한 뒤 닫는다. "
                                  "문제가 아니라고 판단했으면 kept 로 닫는다.")
    with tx(conn) as c:
        c.execute(f"UPDATE cleanup_finding SET resolved_at = {NOW}, resolution = ?, resolved_by = ?, note = ? "
                  "WHERE id = ? AND resolved_at IS NULL", (resolution, store.actor, note, finding_id))
    return _finding(conn.execute(f"SELECT {FINDING_COLS} FROM cleanup_finding WHERE id = ?", (finding_id,)).fetchone())
