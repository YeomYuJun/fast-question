"""fast-question 기본 사용 가이드(kb/default_guide.json)를 DB 에 넣는다.

사용: .venv\\Scripts\\python.exe scripts\\seed_guide.py [--config <config.json>]
install.py 와 run_web.py 도 같은 일을 하므로 보통은 직접 부를 일이 없다. 동작은 kb/guide.py 에 있다.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kb import guide  # noqa: E402
from kb.config import load_config  # noqa: E402
from kb.db import connect  # noqa: E402
from kb.store import Store  # noqa: E402


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="fast-question 기본 사용 가이드를 넣는다")
    ap.add_argument("--config", type=Path)
    cfg = load_config(ap.parse_args(argv).config)
    conn = connect(cfg.db_path)
    try:
        print(guide.seed(Store(conn, "system")))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
