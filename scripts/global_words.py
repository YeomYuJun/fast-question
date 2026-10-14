"""원격 서버의 형태소 사용자 단어(서버 전역 사전)를 보고 바꾸는 운영자 명령. 제어 DB(data_dir/control.sqlite3)에 쓴다.

사용: python scripts/global_words.py --config <config.json> list | add <단어> [품사] | remove <단어>
품사는 NNP(기본)·NNG·SL·XR·VV·VA 다. 원격 모드(mode: remote) 설정만 받는다. 로컬 모드의 사용자 단어는 MCP kb_dict 로 바꾼다.
바꾸면 분석기는 다음 검색·쓰기 때 Kiwi 를 한 번 다시 띄우고, 주기 작업(10분)이 모든 사용자 DB 를 재색인한다.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kb import control, tenant  # noqa: E402
from kb.config import load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="원격 서버의 형태소 사용자 단어(전역 사전)")
    ap.add_argument("--config", type=Path, required=True)
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    add = sub.add_parser("add")
    add.add_argument("word")
    add.add_argument("tag", nargs="?", default="NNP")
    rm = sub.add_parser("remove")
    rm.add_argument("word")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if cfg.mode != "remote":
        print("원격 모드(mode: remote) 설정만 받는다.", file=sys.stderr)
        return 2
    conn = control.connect_control(tenant.control_path(cfg.data_dir))
    try:
        if args.action == "list":
            for word, tag in control.global_words(conn):
                print(f"{word}\t{tag}")
            return 0
        if args.action == "add":
            try:
                control.add_global_word(conn, args.word, args.tag)
            except ValueError as e:
                print(e, file=sys.stderr)
                return 1
            print(f"넣었다: {args.word}\t{args.tag}")
            return 0
        if not control.remove_global_word(conn, args.word):
            print(f"없는 단어다: {args.word}", file=sys.stderr)
            return 1
        print(f"뺐다: {args.word}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
