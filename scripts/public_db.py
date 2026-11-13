"""원격 서버의 공개 지식 층(kb/public_kb.py)을 적재·확인·제거하는 운영자 명령. <data_dir>/public/kb.sqlite3 를 바꾼다.

사용: python scripts/public_db.py --config <config.json> set <sqlite 파일> | info | remove
- set: 파일을 web/importer.validate 로 검사(무결성·스키마 버전·표 목록·뷰 없음)하고 system 이 만든 항목(기본 가이드)을
  kb.dbcopy.prune 으로 뺀 사본을 공개 DB 자리에 넣는다. 원본 파일은 바꾸지 않는다. 적재할 파일은 어느 계정의
  "저장소 파일 받기"(/api/export/db)나 로컬 모드의 data/kb.sqlite3 다.
- 공개 항목의 주소(#pub/<id>)는 적재한 파일의 항목 id 다. 같은 저장소를 고쳐 다시 내보내면 id 가 유지된다. 다른 저장소로
  바꿔 적재하면 기존 주소가 없어지거나 다른 질문을 가리키므로, set 이 그 건수(없어짐·다른 질문)를 알린다.
- 사용자는 공개 DB 에 쓰지 못한다. 바꾸려면 이 명령으로 다시 적재한다. 주기 작업이 분석기 버전에 맞춰 재색인한다.
원격 모드(mode: remote) 설정만 받는다.
"""
import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kb import public_kb  # noqa: E402
from kb.config import load_config  # noqa: E402
from kb.dbcopy import prune  # noqa: E402


def _questions(conn: sqlite3.Connection) -> dict[int, str]:
    return dict(conn.execute("SELECT id, question FROM qa WHERE deleted_at IS NULL"))


def set_public(data_dir: Path, src: Path) -> dict:
    """검사·정리한 사본을 공개 DB 자리에 둔다. 돌려주는 값: prune 결과, 살아 있는 항목 수(items),
    이전 공개 DB 와 견줘 주소가 없어지는 항목 수(gone)와 같은 id 가 다른 질문이 되는 항목 수(repointed)."""
    from web.importer import ImportRefused, validate
    dst = public_kb.public_path(data_dir)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="public-", suffix=".sqlite3", dir=dst.parent)
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        try:
            validate(Path(tmp))
        except ImportRefused as e:
            raise ValueError(e.message) from None
        out = prune(Path(tmp))
        conn = sqlite3.connect(tmp)
        try:
            new = _questions(conn)
        finally:
            conn.close()
        old_conn = public_kb.open_public(data_dir)
        try:
            old = _questions(old_conn) if old_conn is not None else {}
        finally:
            if old_conn is not None:
                old_conn.close()
        out["items"] = len(new)
        out["gone"] = sum(i not in new for i in old)
        out["repointed"] = sum(i in new and new[i] != q for i, q in old.items())
        for suffix in ("-wal", "-shm"):  # 이전 파일의 WAL 잔재가 새 파일과 섞이지 않게
            Path(str(dst) + suffix).unlink(missing_ok=True)
        os.replace(tmp, dst)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return out


def info(data_dir: Path) -> dict | None:
    conn = public_kb.open_public(data_dir)
    if conn is None:
        return None
    try:
        items = conn.execute("SELECT count(*) FROM qa WHERE deleted_at IS NULL").fetchone()[0]
        cats = conn.execute("SELECT count(*) FROM category WHERE deleted_at IS NULL").fetchone()[0]
        return {"path": str(public_kb.public_path(data_dir)), "items": items, "categories": cats}
    finally:
        conn.close()


def remove(data_dir: Path) -> bool:
    path = public_kb.public_path(data_dir)
    if not path.exists():
        return False
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    return True


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="원격 서버의 공개 지식 층")
    ap.add_argument("--config", type=Path, required=True)
    sub = ap.add_subparsers(dest="action", required=True)
    s = sub.add_parser("set")
    s.add_argument("file", type=Path)
    sub.add_parser("info")
    sub.add_parser("remove")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if cfg.mode != "remote":
        print("원격 모드(mode: remote) 설정만 받는다.", file=sys.stderr)
        return 2
    if args.action == "set":
        if not args.file.is_file():
            print(f"파일이 없다: {args.file}", file=sys.stderr)
            return 1
        try:
            out = set_public(cfg.data_dir, args.file)
        except ValueError as e:
            print(f"적재 거부: {e}", file=sys.stderr)
            return 1
        print(f"적재했다: 항목 {out['items']}건 (기본 가이드 {out['system']}건 제외) → {public_kb.public_path(cfg.data_dir)}")
        if out["gone"] or out["repointed"]:
            print(f"이전 공개 주소(#pub/<id>) 중 {out['gone']}개는 없어졌고 {out['repointed']}개는 다른 질문을 가리킨다.")
        return 0
    if args.action == "info":
        got = info(cfg.data_dir)
        print("공개 DB 없음" if got is None else f"{got['path']}: 항목 {got['items']}건, 분류 {got['categories']}개")
        return 0
    print("지웠다" if remove(cfg.data_dir) else "공개 DB 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
