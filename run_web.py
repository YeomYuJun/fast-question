"""웹 서버 진입점. 쓰기 주체는 user 다.

사용: <venv python> run_web.py [--config <config.json>]
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from kb.config import load_config  # noqa: E402
from web.app import create_app  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=None)
    cfg = load_config(ap.parse_args().config)
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
