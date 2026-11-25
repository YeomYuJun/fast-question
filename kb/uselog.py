"""이 PC 의 KB 사용 기록. 찾기·쓰기·고치기를 한 줄씩 쌓고, 그 기록으로 세 지표(measure)를 계산한다.

Q&A 를 등록하는 것은 "이 사실이 다시 필요해진다" 는 예측이다. 이 기록은 그 예측이 맞았는지를 남긴다. 쓰는 곳은 셋이다.
- 훅의 등록 안내(hooks/ask_kb.py store_conditions)가 그 프로젝트에서 여러 날 다시 찾아진 질문(examples)과
  찾았으나 없었던 검색어(open_misses)를 판단 근거로 든다.
- `fast-question stats`(fq_client/cli.py)가 세 지표(measure)와 다시 찾아지지 않은 등록분(unused)을 낸다.
- 한 번도 다시 찾아지지 않은 등록분을 가려낸다(docs/probe/hook_audit.py).

위치: %USERPROFILE%\\.fast-question\\use.jsonl (kb.remote.home). 환경 변수 FQ_USELOG 로 바꾼다(테스트가 쓴다).
한 줄은 {"at": "<UTC ISO 시각>", "kind": ..., "cwd": ..., "items": [[id, 질문], ...], "v": 2, "queries": [...]} 이다.

| kind | 적는 곳 | 뜻 |
|---|---|---|
| hook_hit | 즉석 질문 훅 | 사용자 질문에 맞는 항목으로 넣었다 |
| search | 프록시 kb_search | 결과가 있었다(앞쪽 SEARCH_TOP 건) |
| miss | 프록시 kb_search | 결과가 0건이었다. items 는 비고 queries 에 Claude 가 넣은 검색어를 적는다 |
| get | 프록시 kb_get | 항목을 읽었다 |
| cite | 조사 턴 수확 훅(Stop) | 그 턴의 응답 글에 항목 주소(#qa/<id>)가 있었다 |
| add | 프록시 kb_add | 등록했다 |
| nudge | 조사 턴 수확 훅(Stop) | 등록 조건을 Claude 에게 넣었다. tag 가 넣은 경우다: investigation(조사 턴), missed(0건 검색이 있던 턴), asked(답이 없던 질문으로 시작한 턴) |
| update·archive·evidence | 프록시 kb_update·kb_archive·kb_evidence | 항목을 고쳤다 / 지웠다 / 근거를 다시 기록했다 |

사용자의 프롬프트는 적지 않는다. 검색어는 0건일 때 Claude 가 kb_search 에 넣은 것만 적는다(대상 이름·오류 문구).
기록은 이 PC 에만 있고 서버로 보내지 않는다. 쓰기·읽기의 어떤 실패도 호출한 쪽에 올리지 않는다(훅과 MCP 응답을 막지 않는다).
PC 클라이언트 패키지(pyproject.toml)에 들어가는 모듈이라 표준 라이브러리만 쓴다.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from kb.remote import home

MAX_BYTES = 1024 * 1024  # 넘으면 뒤쪽 절반만 남긴다
SEARCH_TOP = 3  # kb_search 결과에서 다시 찾아진 것으로 적는 앞쪽 항목 수
RETRIEVED = ("hook_hit", "search", "get")  # 다시 찾아진 것으로 세는 kind
REVISED = ("update", "archive", "evidence")  # 항목을 고친 것으로 세는 kind
MIN_DAYS = 2  # 예로 드는 항목이 다시 찾아진 날 수의 하한. 등록한 날의 검색 한 번은 우연일 수 있다
QUERY_MAX = 120  # 적는 검색어 한 개의 글자 수
NUDGE_WINDOW_S = 600  # 등록 안내 뒤 이 시간 안의 등록을 그 안내에서 나온 것으로 센다
UNUSED_DAYS = 14  # 등록 뒤 이만큼 지나야 "다시 찾아지지 않았다" 를 따진다
MISS_KEEP_DAYS = 30  # 훅이 "찾았으나 없었던 검색어" 로 드는 기록의 나이 상한
# 줄 형식 번호. 2 부터 miss·cite·update·archive·evidence 를 적는다. v 가 없는 줄(1)은 그것들을 적지 않던 때의 것이라
# measure 가 세지 않는다. 섞어 세면 0건 검색이 없던 것처럼, 주소가 한 번도 붙지 않은 것처럼 계산된다
VERSION = 2

# (검색어, {"question": 질문}) → 그 질문이 검색어의 대상을 담고 있는가. 훅과 같은 기준(hooks/ask_kb.py grounded)을 받는다
Match = Callable[[str, dict], bool]


def log_path() -> Path:
    env = os.environ.get("FQ_USELOG")
    return Path(env) if env else home() / "use.jsonl"


def _norm(cwd: str) -> str:
    return cwd.replace("\\", "/").rstrip("/").lower()


def _seconds(row: dict) -> float:
    """줄의 시각을 초로. 형식이 다르면 0."""
    try:
        return datetime.strptime(str(row.get("at", "")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(kind: str, items: list, cwd: str | None = None, path: Path | None = None, at: str | None = None,
           queries: list | None = None, version: int = VERSION, tag: str | None = None) -> None:
    """items 는 [(id, 질문), ...], queries 는 검색어 목록, tag 는 그 줄의 세부 종류다. 셋 다 비어 있으면 적지 않는다.
    version 은 줄 형식 번호다. 전사에서 옛 기록을 옮길 때(docs/probe/hook_audit.py backfill)만 1 을 준다."""
    try:
        rows = [[int(i), str(q)[:200]] for i, q in items if i is not None]
        terms = [str(q).strip()[:QUERY_MAX] for q in queries or [] if str(q or "").strip()]
        if not rows and not terms and not tag:
            return
        path = path or log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text("\n".join(lines[len(lines) // 2:]) + "\n", encoding="utf-8")
        line = {"at": at or _now(), "kind": kind, "cwd": _norm(cwd if cwd is not None else os.getcwd()), "items": rows,
                "v": version}
        if terms:
            line["queries"] = terms
        if tag:
            line["tag"] = tag
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - 기록 실패가 훅·MCP 응답을 막지 않게 한다
        return


def read(path: Path | None = None) -> list[dict]:
    try:
        text = (path or log_path()).read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for ln in text.splitlines():
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("items"), list):
            rows.append(row)
    return rows


def _under(rows: list[dict], under: str | None) -> list[dict]:
    """그 폴더, 그 하위, 그 상위에서 남긴 기록. under 가 없으면 전부."""
    if not under:
        return rows
    root = _norm(under)
    return [r for r in rows if (c := str(r.get("cwd", ""))) == root or c.startswith(root + "/")
            or root.startswith(c + "/")]


def _added_day(rows: list[dict]) -> dict[int, str]:
    added: dict[int, str] = {}
    for r in rows:
        if r.get("kind") == "add":
            for i, _ in r["items"]:
                added.setdefault(i, str(r.get("at", ""))[:10])
    return added


def _pairs(rows: list[dict], kinds: tuple, added: dict[int, str]) -> dict[tuple[str, int], str]:
    """{(날, id): 질문}. 등록(add)한 날의 기록은 뺀다. 등록하면서 중복을 확인하느라 찾고 보고하느라 주소를 붙인 것이다."""
    out: dict[tuple[str, int], str] = {}
    for r in rows:
        if r.get("kind") not in kinds:
            continue
        day = str(r.get("at", ""))[:10]
        for i, q in r["items"]:
            if added.get(i) != day:
                out[(day, i)] = q or out.get((day, i), "")
    return out


def retrieved_days(rows: list[dict], under: str | None = None) -> dict[int, dict]:
    """항목별로 다시 찾아진 날 수. {id: {"question", "days"}}. under 를 주면 그 폴더(와 하위·상위)에서 남긴 기록만 센다."""
    out: dict[int, dict] = {}
    for (_, i), q in _pairs(_under(rows, under), RETRIEVED, _added_day(rows)).items():
        e = out.setdefault(i, {"question": q, "days": 0})
        e["days"] += 1
        e["question"] = q
    return out


def examples(under: str | None, n: int = 2, path: Path | None = None) -> list[tuple[str, int]]:
    """그 폴더에서 MIN_DAYS 일 이상 다시 찾아진 질문을 날 수가 많은 순으로 n 개. 없으면 빈 목록."""
    try:
        got = retrieved_days(read(path), under)
    except Exception:  # noqa: BLE001
        return []
    top = sorted((e for e in got.values() if e["days"] >= MIN_DAYS), key=lambda e: -e["days"])
    return [(e["question"], e["days"]) for e in top[:n]]


def misses(rows: list[dict], match: Match, under: str | None = None) -> list[dict]:
    """kb_search 가 0건이던 검색어. [{"query", "days", "first", "last", "closed"}] 를 마지막 시각이 늦은 순으로.

    closed 는 그 검색어를 처음 놓친 뒤에 등록(add)된 질문이 그 검색어의 대상을 담고 있는 것이다(match). 놓친 것이
    등록으로 메워졌다는 뜻이다. 같은 검색어는 대소문자를 가리지 않고 하나로 묶는다."""
    scope = _under(rows, under)
    found: dict[str, dict] = {}
    for r in scope:
        if r.get("kind") != "miss":
            continue
        at = str(r.get("at", ""))
        for q in r.get("queries") or []:
            e = found.setdefault(str(q).lower(), {"query": str(q), "days": set(), "first": at, "last": at})
            e["days"].add(at[:10])
            e["first"], e["last"] = min(e["first"], at), max(e["last"], at)
    adds = [(str(r.get("at", "")), q) for r in scope if r.get("kind") == "add" for _, q in r["items"]]
    out = []
    for e in found.values():
        closed = any(at >= e["first"] and match(e["query"], {"question": q}) for at, q in adds)
        out.append({**e, "days": len(e["days"]), "closed": closed})
    return sorted(out, key=lambda e: e["last"], reverse=True)


def open_misses(under: str | None, match: Match, since: str | None = None, min_days: int = 1, n: int = 3,
                path: Path | None = None, now: datetime | None = None) -> list[tuple[str, int]]:
    """그 폴더에서 찾았으나 없었고 아직 등록으로 메워지지 않은 검색어와 놓친 날 수. 마지막 시각이 늦은 순으로 n 개.
    since(UTC ISO 시각) 를 주면 그 뒤에 놓친 것만, min_days 를 주면 그 날 수 이상 놓친 것만 고른다."""
    try:
        floor = ((now or datetime.now(timezone.utc)) - timedelta(days=MISS_KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        got = [e for e in misses(read(path), match, under)
               if not e["closed"] and e["last"] >= max(floor, since or "") and e["days"] >= min_days]
    except Exception:  # noqa: BLE001
        return []
    return [(e["query"], e["days"]) for e in got[:n]]


def unused(rows: list[dict], since: str | None = None, until: str | None = None, under: str | None = None,
           now: datetime | None = None) -> dict:
    """등록한 지 UNUSED_DAYS 일이 지난 등록분(aged)과 그중 등록한 날 뒤로 한 번도 다시 찾아지지 않은 것(unused).
    {"aged": 건수, "unused": [(id, 질문), ...]}. 등록과 찾기는 옛 형식의 줄에도 있어 형식 번호를 가리지 않고 센다.
    다시 찾아지지 않은 등록은 "다시 필요해진다" 는 예측이 아직 맞지 않은 것이다. 지우라는 뜻은 아니다(드물게 필요한 사실도 있다)."""
    cut = ((now or datetime.now(timezone.utc)) - timedelta(days=UNUSED_DAYS)).strftime("%Y-%m-%d")
    scope = _under(rows, under)
    added: dict[int, tuple[str, str]] = {}
    for r in scope:
        if r.get("kind") == "add":
            for i, q in r["items"]:
                added.setdefault(i, (str(r.get("at", ""))[:10], q))
    aged = {i: q for i, (day, q) in added.items()
            if day <= cut and not ((since and day < since) or (until and day >= until))}
    found = {i for _, i in _pairs(rows, RETRIEVED, _added_day(rows))}
    return {"aged": len(aged), "unused": [(i, q) for i, q in aged.items() if i not in found]}


def measure(rows: list[dict], match: Match, since: str | None = None, until: str | None = None,
            under: str | None = None) -> dict:
    """세 지표. since·until 은 UTC 날짜(YYYY-MM-DD, until 은 그 날 미포함)다. 형식 번호가 VERSION 이상인 줄만 센다.

    retrieval(찾기): kb_search 호출 수(searches)와 그중 결과가 있던 수(found), 즉석 질문 훅이 맞는 항목을 넣은 수(hook_hits).
    application(쓰기): 찾아진 (날, 항목) 수(retrieved)와 그중 같은 날 응답에 주소가 붙은 수(cited).
    evolution(고치기): 고치거나 지우거나 근거를 다시 기록한 항목 수(revised), 그중 그 전에 찾아진 적이 있는 수
      (revised_after_use), 0건이던 검색어 수(missed)와 그중 뒤의 등록으로 메워진 수(closed), 아직 열린 검색어(open).
    nudges(등록 안내): 수확 훅이 등록 조건을 넣은 횟수(total, 경우별 by)와 그 뒤 NUDGE_WINDOW_S 안에 같은 폴더에서
      등록이 있었던 횟수(followed_by_add). 안내가 얼마나 자주 붙고 그중 얼마가 등록으로 이어지는지를 본다.
    """
    scope = [r for r in _under(rows, under)
             if r.get("v", 1) >= VERSION and not ((since and str(r.get("at", ""))[:10] < since) or (until and str(r.get("at", ""))[:10] >= until))]
    count = {k: sum(r.get("kind") == k for r in scope) for k in ("search", "miss", "hook_hit", "add")}
    added = _added_day(rows)
    got, cited = _pairs(scope, RETRIEVED, added), _pairs(scope, ("cite",), added)
    first_use: dict[int, str] = {}
    for day, i in sorted(got):
        first_use.setdefault(i, day)
    revised: dict[int, str] = {}  # id → 마지막으로 고친 날
    for day, i in _pairs(scope, REVISED, added):  # 등록한 날 근거를 붙이거나 다듬은 것은 고친 것으로 세지 않는다
        revised[i] = max(revised.get(i, ""), day)
    missed = misses(scope, match)
    nudges = [r for r in scope if r.get("kind") == "nudge"]
    by_tag: dict[str, int] = {}
    for r in nudges:
        by_tag[str(r.get("tag", ""))] = by_tag.get(str(r.get("tag", "")), 0) + 1
    add_rows = [(_seconds(r), r.get("cwd")) for r in scope if r.get("kind") == "add"]
    followed = sum(any(cwd == r.get("cwd") and 0 <= at - _seconds(r) <= NUDGE_WINDOW_S for at, cwd in add_rows)
                   for r in nudges)
    return {
        "nudges": {"total": len(nudges), "by": by_tag, "followed_by_add": followed},
        "retrieval": {"searches": count["search"] + count["miss"], "found": count["search"],
                      "hook_hits": count["hook_hit"]},
        "application": {"retrieved": len(got), "cited": len(set(got) & set(cited))},
        "evolution": {"added": count["add"], "revised": len(revised),
                      "revised_after_use": sum(i in first_use and first_use[i] <= day for i, day in revised.items()),
                      "missed": len(missed), "closed": sum(e["closed"] for e in missed),
                      "open": [(e["query"], e["days"]) for e in missed if not e["closed"]]},
    }
