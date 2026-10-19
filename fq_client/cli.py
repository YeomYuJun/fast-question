"""fast-question PC 클라이언트 명령. 원격 서버를 개인 토큰으로 쓴다.

사용: fast-question <명령> [인자]
  login [--url <주소>]   브라우저 승인으로 개인 토큰을 받아 credentials.json 에 저장(fq_client/login.py)
  mcp                   Claude Code 가 띄우는 로컬 프록시 MCP(stdio, mcp_server/proxy.py)
  hook                   즉석 질문 훅(UserPromptSubmit, hooks/ask_kb.py). 표준 입력으로 훅 JSON 을 받는다
  lint-draft <draft.json>  qa-harvest 초안 검사(fq_client/lint_draft.py)
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


def _login(args) -> int:
    from fq_client.login import main
    return main(args.url)


def _lint_draft(args) -> int:
    from fq_client.lint_draft import main
    return main([args.draft])


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
    lint = sub.add_parser("lint-draft", help="qa-harvest 초안 검사")
    lint.add_argument("draft")
    lint.set_defaults(run=_lint_draft)
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
