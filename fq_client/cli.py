"""fast-question PC 클라이언트 명령. 원격 서버를 개인 토큰으로 쓴다.

사용: fast-question <명령> [인자]
  login [--url <주소>]   브라우저 승인으로 개인 토큰을 받아 credentials.json 에 저장(fq_client/login.py)
  mcp                   Claude Code 가 띄우는 로컬 프록시 MCP(stdio, mcp_server/proxy.py)
  hook                   즉석 질문 훅(UserPromptSubmit, hooks/ask_kb.py). 표준 입력으로 훅 JSON 을 받는다
  stop-hook              조사 턴 수확 훅(Stop, hooks/stop_harvest.py). 표준 입력으로 훅 JSON 을 받는다
  inventory <폴더> ... [--since <json>] [--out <json>]  qa-harvest 대상 폴더 목록(fq_client/inventory.py)
  lint-draft <draft.json>  qa-harvest 초안 검사(fq_client/lint_draft.py)
  stats [--days N | --since <날짜>] [--until <날짜>] [--here] [--json]  이 PC 사용 기록의 세 지표(kb/uselog.py measure)
서버 주소·개인 토큰·device_id 는 %USERPROFILE%\\.fast-question\\credentials.json(kb/remote.py)에서 읽는다.
저장소에서는 .venv\\Scripts\\python.exe fq_client\\cli.py <명령> 으로 같은 명령을 쓴다.
이 모듈과 이 모듈이 부르는 모듈은 서버 모듈(mcp_server.server, kb.store, kb.morph, kb.control)을 import 하지 않는다
(tests/test_client_package.py 가 확인한다).
"""
import argparse
import sys
from pathlib import Path


def _mcp(_args) -> int:
    import anyio

    from kb.remote import credentials_path, load_credentials
    from mcp_server.proxy import Remote, run_stdio
    cred = load_credentials()
    if cred is None:
        # stdout 은 MCP 프로토콜이 쓴다. 안내는 stderr 로만 낸다
        sys.stderr.write(f"원격 자격 증명이 없거나 형식이 틀렸습니다: {credentials_path()}. "
                         "fast-question login 으로 로그인하세요.\n")
        return 1
    anyio.run(run_stdio, Remote(cred["url"], cred["token"], cred["device_id"]))
    return 0


def _hook(_args) -> int:
    from hooks.ask_kb import main
    return main(remote_only=True)


def _stop_hook(_args) -> int:
    from hooks.stop_harvest import main
    return main(remote_only=True)


def _login(args) -> int:
    from fq_client.login import main
    return main(args.url)


def _inventory(args) -> int:
    from fq_client.inventory import main
    return main(args.rest)


def _lint_draft(args) -> int:
    from fq_client.lint_draft import main
    return main([args.draft])


def _share(part: int, whole: int) -> str:
    return f"{part}/{whole}" + (f"({round(100 * part / whole)}%)" if whole else "")


def stats_text(m: dict, span: str, where: str, idle: dict | None = None, days: int = 0) -> str:
    """measure() 결과(와 unused() 결과 idle)를 사람이 읽는 몇 줄로. 비율의 분모가 0 이면 비율을 붙이지 않는다."""
    r, a, e = m["retrieval"], m["application"], m["evolution"]
    lines = [
        f"fast-question 사용 지표 ({span}, {where}, 이 PC 기록)",
        f"찾기: kb_search 중 결과가 있던 호출 {_share(r['found'], r['searches'])}, 즉석 질문 훅이 넣은 맞는 항목 {r['hook_hits']}회",
        f"쓰기: 찾아진 항목 중 그날 응답에 주소가 붙은 것 {_share(a['cited'], a['retrieved'])} (날·항목 쌍)",
        f"고치기: 등록 {e['added']}건, 고친 항목 {e['revised']}건(찾아진 뒤 고친 것 {e['revised_after_use']}건), "
        f"0건이던 검색어 중 뒤의 등록으로 메워진 것 {_share(e['closed'], e['missed'])}",
    ]
    n = m["nudges"]
    names = {"investigation": "조사 턴", "missed": "0건 검색이 있던 턴", "asked": "답이 없던 질문으로 시작한 턴"}
    by = ", ".join(f"{names.get(k, k)} {v}" for k, v in n["by"].items())
    lines.append(f"등록 안내: {n['total']}회" + (f"({by})" if by else "")
                 + f", 안내 뒤 10분 안에 등록이 있었던 것 {_share(n['followed_by_add'], n['total'])}")
    if not (r["searches"] or r["hook_hits"] or a["retrieved"] or e["added"] or e["revised"]):
        lines.append("이 기간에 셀 기록이 없습니다. 0건 검색어와 응답 주소를 적기 시작한 뒤의 기록만 셉니다.")
    if e["open"]:
        shown = ", ".join(f"'{q}'({d}일)" if d > 1 else f"'{q}'" for q, d in e["open"][:20])
        lines.append(f"찾았으나 아직 없는 검색어 {len(e['open'])}개: {shown}")
    if idle is not None:
        lines.append(f"등록분: 등록한 지 {days}일이 지난 것 중 한 번도 다시 찾아지지 않은 것 "
                     f"{_share(len(idle['unused']), idle['aged'])} (이 PC 에서 등록하고 찾은 것만 센다)")
        lines += [f"  #{i} {q}" for i, q in idle["unused"][:20]]
    return "\n".join(lines)


def _stats(args) -> int:
    import json
    import os
    from datetime import datetime, timedelta, timezone

    from hooks.ask_kb import grounded
    from kb import uselog
    since = args.since or (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    under = os.getcwd() if args.here else None
    rows = uselog.read()
    m = uselog.measure(rows, grounded, since, args.until, under)
    # 등록분은 --since 를 줬을 때만 그 기간으로 자른다. 기본 기간(최근 30일)으로 자르면 14일이 지난 등록이 거의 남지 않는다
    idle = uselog.unused(rows, args.since, args.until, under)
    if args.json:
        sys.stdout.write(json.dumps({**m, "registered": idle}, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(stats_text(m, f"{since} ~ {args.until or '지금'}", under or "모든 폴더", idle,
                                    uselog.UNUSED_DAYS) + "\n")
    return 0


def parser() -> argparse.ArgumentParser:
    from fq_client import __version__
    ap = argparse.ArgumentParser(prog="fast-question", description="fast-question PC 클라이언트")
    ap.add_argument("--version", action="version", version=f"fast-question {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="브라우저 승인으로 개인 토큰을 받아 credentials.json 에 저장")
    login.add_argument("--url", help="서버 주소(기본: credentials.json 의 주소, 없으면 https://fast-question.com)")
    login.set_defaults(run=_login)
    sub.add_parser("mcp", help="로컬 프록시 MCP(stdio)").set_defaults(run=_mcp)
    sub.add_parser("hook", help="즉석 질문 훅(UserPromptSubmit)").set_defaults(run=_hook)
    sub.add_parser("stop-hook", help="조사 턴 수확 훅(Stop)").set_defaults(run=_stop_hook)
    inv = sub.add_parser("inventory", help="qa-harvest 대상 폴더 목록(fq_client/inventory.py 와 같은 인자)")
    inv.add_argument("rest", nargs=argparse.REMAINDER)
    inv.set_defaults(run=_inventory)
    lint = sub.add_parser("lint-draft", help="qa-harvest 초안 검사")
    lint.add_argument("draft")
    lint.set_defaults(run=_lint_draft)
    st = sub.add_parser("stats", help="이 PC 사용 기록의 세 지표(찾기·쓰기·고치기)")
    st.add_argument("--days", type=int, default=30, help="최근 며칠(기본 30). --since 를 주면 쓰지 않는다")
    st.add_argument("--since", help="이 날짜부터(UTC, YYYY-MM-DD)")
    st.add_argument("--until", help="이 날짜 전까지(UTC, YYYY-MM-DD)")
    st.add_argument("--here", action="store_true", help="지금 폴더와 그 하위·상위에서 남긴 기록만")
    st.add_argument("--json", action="store_true", help="계산값을 JSON 한 줄로")
    st.set_defaults(run=_stats)
    return ap


def main(argv: list[str] | None = None) -> int:
    # 파이프로 나갈 때 CP949 가 아니라 UTF-8 로 낸다(Claude Code 가 MCP stderr 를 로그로 읽는다). 콘솔에는 영향이 없다
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    ROOT = Path(__file__).resolve().parent.parent  # 저장소에서 스크립트로 실행할 때
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
