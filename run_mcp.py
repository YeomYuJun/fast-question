"""Claude Code 가 띄우는 MCP 진입점(stdio). 쓰기 주체는 claude 다.

stdout 은 MCP 프로토콜이 쓴다. 이 프로세스에서 print 하지 않는다.
사용: <venv python> run_mcp.py [--config <config.json>]
      <venv python> run_mcp.py --remote   원격 서버를 쓰는 로컬 프록시(mcp_server/proxy.py). 주소와 개인 토큰은
                                          %USERPROFILE%\\.fast-question\\credentials.json(kb/remote.py)에서 읽는다
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from kb.config import load_config  # noqa: E402
from kb.db import connect  # noqa: E402
from kb.morph import Analyzer  # noqa: E402
from mcp_server.server import build_server, store_factory  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--remote", action="store_true")
    args = ap.parse_args()
    if args.remote:
        import anyio

        from kb.remote import credentials_path, load_credentials
        from mcp_server.proxy import Remote, run_stdio
        cred = load_credentials()
        if cred is None:
            sys.exit(f"원격 자격 증명이 없거나 형식이 틀렸다: {credentials_path()} (scripts/install.py --remote 로 만든다)")
        anyio.run(run_stdio, Remote(cred["url"], cred["token"]))
        return
    cfg = load_config(args.config)
    connect(cfg.db_path).close()  # 스키마 준비
    build_server(store_factory(cfg.db_path, Analyzer())).run("stdio")


if __name__ == "__main__":
    main()
