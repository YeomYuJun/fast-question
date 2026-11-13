"""원격 서버의 유입·사용 집계를 출력하는 운영자 명령(읽기 전용, kb/stats.py).

사용: python scripts/stats.py --config <config.json> [--days 14] [--json]
날짜(UTC)별 가입 수, 등록한 계정 수, 등록 건수(화면 / Claude)와 현재 계정 수, 최근 7일 사용 계정 수를 낸다.
원격 모드(mode: remote) 설정만 받는다.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kb import stats  # noqa: E402
from kb.config import load_config  # noqa: E402


def render(out: dict) -> str:
    a, act = out["accounts"], out["active"]
    lines = [f"계정 {a['alive']}개 (탈퇴 {a['closed']}개), 항목 {out['items']}건",
             f"최근 {act['days']}일 사용 계정: 웹 {act['web']}개, PC 연결 {act['pc']}개",
             "", "날짜(UTC)   가입  등록계정  화면등록  Claude등록"]
    lines += [f"{d['day']}  {d['signups']:>4}  {d['writers']:>8}  {d['added_user']:>8}  {d['added_claude']:>10}"
              for d in out["days"]]
    if out["unreadable"]:
        lines += ["", f"열지 못한 사용자 DB: 계정 {', '.join(map(str, out['unreadable']))}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="원격 서버의 유입·사용 집계")
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if cfg.mode != "remote":
        print("원격 모드(mode: remote) 설정만 받는다.", file=sys.stderr)
        return 2
    if not 1 <= args.days <= 366:
        print("--days 는 1~366 이다.", file=sys.stderr)
        return 2
    out = stats.inflow(cfg.data_dir, args.days)
    print(json.dumps(out, ensure_ascii=False) if args.json else render(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
