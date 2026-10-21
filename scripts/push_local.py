"""로컬 DB 를 원격 서버의 내 계정 DB 로 옮긴다.

사용: .venv\\Scripts\\python.exe scripts\\push_local.py [--db <kb.sqlite3>] [--config <config.json>]
      [--exclude-category <분류 경로>]... [--dry-run]
--db 가 없으면 config 의 db_path(기본 data/kb.sqlite3)를 쓴다. 서버 주소와 개인 토큰은 kb/remote.py 의
credentials.json(%USERPROFILE%\\.fast-question\\credentials.json)에서 읽는다.
SQLite 온라인 백업 API 로 사본 파일 하나를 임시 폴더에 만들고, 사본에서 로컬 기본 가이드(system 이 만든 항목)와
--exclude-category 분류(하위 분류 포함, "대분류" 또는 "대분류 > 중분류")를 행째 지운 뒤(kb.dbcopy.prune)
POST /api/import/db 로 올리고 사본을 지운다. 제외한 분류의 내용은 서버로 가지 않는다.
로컬 DB 는 바꾸지 않는다. 서버의 내 DB 가 비어 있으면(기본 가이드만 있으면) 통째로 바꾸고 원격 기본 가이드를
다시 넣는다. 아니면 항목을 하나씩 넣고 이미 있는 항목과 비슷한 질문은 건너뛴다(web/importer.py).
--dry-run 은 같은 사본을 POST /api/import/db/plan 으로 보내 처리 방식과 건수만 받는다. 서버에 쓰지 않는다.
그 경로가 없는 옛 서버면 실제 가져오기로 넘어가지 않고 실패로 끝난다. 자격 증명에 device_id 가 있으면
함께 보내 옮긴 파일·git 근거를 이 PC 의 근거로 표시한다(이 PC 의 로컬 검사기가 판정한다).
"""
import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from kb.config import load_config  # noqa: E402
from kb.dbcopy import copy_db, prune  # noqa: E402
from kb.remote import credentials_path, load_credentials  # noqa: E402

UPLOAD_TIMEOUT_S = 600
_LOAD = object()


def push(file: Path, creds: dict, http=None, dry_run: bool = False) -> httpx.Response:
    client = http or httpx.Client(timeout=UPLOAD_TIMEOUT_S)
    try:
        params = {"origin": creds["device_id"]} if creds.get("device_id") else None
        path = "/api/import/db/plan" if dry_run else "/api/import/db"
        return client.post(f"{creds['url']}{path}", params=params, content=file.read_bytes(),
                           headers={"Authorization": f"Bearer {creds['token']}",
                                    "Content-Type": "application/octet-stream"})
    finally:
        if http is None:
            client.close()


def report(result: dict) -> str:
    if result.get("dry_run"):
        return preview(result)
    if result.get("mode") == "replaced":
        return f"서버의 내 DB 가 비어 있어 통째로 바꿨다. 항목 {result['items']}건."
    lines = [f"서버의 내 DB 에 항목 {result['added']}건을 넣었다. 비슷한 질문이 있어 건너뛴 것 "
             f"{len(result['skipped'])}건, 넣지 못한 것 {len(result['failed'])}건."]
    lines += [f"- 건너뜀: {x['question']} (비슷한 항목 #{x['similar'][0]['id']})" for x in result["skipped"]]
    lines += [f"- 실패: {x['question']} ({x['error']})" for x in result["failed"]]
    return "\n".join(lines)


def preview(result: dict) -> str:
    if result.get("mode") == "replaced":
        return (f"미리보기: 서버의 내 DB 가 비어 있어 통째로 바꾼다. 항목 {result['items']}건. "
                "서버의 기본 가이드는 다시 넣는다. 아무것도 쓰지 않았다.")
    lines = [f"미리보기: 서버의 내 DB 에 항목 {result['added']}건을 하나씩 넣는다. 비슷한 질문이 있어 건너뛸 것 "
             f"{len(result['skipped'])}건. 아무것도 쓰지 않았다."]
    lines += [f"- 건너뜀: {x['question']} (비슷한 항목 #{x['similar'][0]['id']})" for x in result["skipped"]]
    return "\n".join(lines)


def excluded(counts: dict, categories: list[str]) -> str:
    text = f"올리지 않는 것: 로컬 기본 가이드 {counts['system']}건"
    if categories:
        text += f", 분류 {', '.join(categories)} 의 항목 {counts['excluded']}건(보관한 것 포함)"
    return text + "."


def main(argv: list[str] | None = None, *, creds=_LOAD, http=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="로컬 DB 를 원격 서버의 내 계정으로 옮긴다")
    ap.add_argument("--db", type=Path)
    ap.add_argument("--config", type=Path)
    ap.add_argument("--exclude-category", action="append", default=[], metavar="분류",
                    help='올리지 않을 분류. "대분류" 또는 "대분류 > 중분류". 여러 번 줄 수 있다')
    ap.add_argument("--dry-run", action="store_true", help="쓰기 없이 서버가 할 일(통째로 바꾸기/하나씩 넣기)과 건수만 본다")
    args = ap.parse_args(argv)
    db = args.db or load_config(args.config).db_path
    if creds is _LOAD:
        creds = load_credentials()
    if creds is None:
        print(f"원격 자격 증명이 없다: {credentials_path()}. credentials.json 에 서버 주소와 개인 토큰을 둔다.",
              file=sys.stderr)
        return 1
    if not db.is_file():
        print(f"로컬 DB 가 없다: {db}", file=sys.stderr)
        return 1
    work = Path(tempfile.mkdtemp(prefix="fq-push-"))
    try:
        src = sqlite3.connect(str(db))
        try:
            file = copy_db(src, work / "upload.sqlite3")
        finally:
            src.close()
        try:
            counts = prune(file, args.exclude_category)
        except ValueError as e:
            print(f"{e}. 아무것도 올리지 않았다.", file=sys.stderr)
            return 1
        print(excluded(counts, args.exclude_category))
        print(f"{'미리보기 요청' if args.dry_run else '올리는 중'}: {db} ({file.stat().st_size // 1024} KB) → {creds['url']}")
        try:
            r = push(file, creds, http, args.dry_run)
        except httpx.HTTPError as e:
            print(f"서버에 연결하지 못했다: {e}", file=sys.stderr)
            return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    try:
        body = r.json()
    except ValueError:
        body = {"error": r.text[:200]}
    if args.dry_run and r.status_code in (404, 405):
        print("이 서버는 미리보기를 지원하지 않는다(서버를 새 버전으로 올려야 한다). 실제 가져오기는 보내지 않았다.",
              file=sys.stderr)
        return 1
    if r.status_code != 200:
        print(f"서버가 거부했다({r.status_code}): {body.get('error', body)}", file=sys.stderr)
        return 1
    print(report(body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
