"""Claude Code 가 띄우는 MCP 진입점(stdio). 쓰기 주체는 claude 다.

stdout 은 MCP 프로토콜이 쓴다. 이 프로세스에서 print 하지 않는다.
사용: <venv python> run_mcp.py [--config <config.json>]
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from kb.config import load_config  # noqa: E402
from kb.db import connect  # noqa: E402
from kb.morph import Analyzer  # noqa: E402
from kb.store import Store  # noqa: E402
from mcp_server.server import build_server  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=None)
    cfg = load_config(ap.parse_args().config)
    store = Store(connect(cfg.db_path), "claude", analyzer=Analyzer())
    build_server(lambda: store).run("stdio")


if __name__ == "__main__":
    main()
