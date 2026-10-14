"""로컬 DB 를 원격 서버의 내 계정 DB 로 옮긴다.

사용: .venv\\Scripts\\python.exe scripts\\push_local.py [--db <kb.sqlite3>] [--config <config.json>]
--db 가 없으면 config 의 db_path(기본 data/kb.sqlite3)를 쓴다. 서버 주소와 개인 토큰은 kb/remote.py 의
credentials.json(%USERPROFILE%\\.fast-question\\credentials.json)에서 읽는다.
SQLite 온라인 백업 API 로 사본 파일 하나를 임시 폴더에 만들어 POST /api/import/db 로 올리고 사본을 지운다.
로컬 DB 는 바꾸지 않는다. 서버의 내 DB 가 비어 있으면(기본 가이드만 있으면) 통째로 바꾸고, 아니면 항목을
하나씩 넣고 이미 있는 항목과 비슷한 질문은 건너뛴다(web/importer.py). 자격 증명에 device_id 가 있으면
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
from kb.remote import credentials_path, load_credentials  # noqa: E402

UPLOAD_TIMEOUT_S = 600
_LOAD = object()


def snapshot(db: Path, dst: Path) -> Path:
    """WAL 이어도 사본은 journal_mode DELETE 파일 하나다. quick_check 가 ok 가 아니면 RuntimeError."""
    src = sqlite3.connect(str(db))
    target = sqlite3.connect(str(dst))
    try:
        src.backup(target)
        target.execute("PRAGMA journal_mode = DELETE")
        check = target.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        target.close()
        src.close()
    if check != "ok":
        raise RuntimeError(f"사본의 quick_check 가 실패했다: {check}")
    return dst


def push(file: Path, creds: dict, http=None) -> httpx.Response:
    client = http or httpx.Client(timeout=UPLOAD_TIMEOUT_S)
    try:
        params = {"origin": creds["device_id"]} if creds.get("device_id") else None
        return client.post(f"{creds['url']}/api/import/db", params=params, content=file.read_bytes(),
                           headers={"Authorization": f"Bearer {creds['token']}",
                                    "Content-Type": "application/octet-stream"})
    finally:
        if http is None:
            client.close()


def report(result: dict) -> str:
    if result.get("mode") == "replaced":
        return f"서버의 내 DB 가 비어 있어 통째로 바꿨다. 항목 {result['items']}건."
    lines = [f"서버의 내 DB 에 항목 {result['added']}건을 넣었다. 비슷한 질문이 있어 건너뛴 것 "
             f"{len(result['skipped'])}건, 넣지 못한 것 {len(result['failed'])}건."]
    lines += [f"- 건너뜀: {x['question']} (비슷한 항목 #{x['similar'][0]['id']})" for x in result["skipped"]]
    lines += [f"- 실패: {x['question']} ({x['error']})" for x in result["failed"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, creds=_LOAD, http=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="로컬 DB 를 원격 서버의 내 계정으로 옮긴다")
    ap.add_argument("--db", type=Path)
    ap.add_argument("--config", type=Path)
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
        file = snapshot(db, work / "upload.sqlite3")
        print(f"올리는 중: {db} ({file.stat().st_size // 1024} KB) → {creds['url']}")
        try:
            r = push(file, creds, http)
        except httpx.HTTPError as e:
            print(f"서버에 연결하지 못했다: {e}", file=sys.stderr)
            return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    try:
        body = r.json()
    except ValueError:
        body = {"error": r.text[:200]}
    if r.status_code != 200:
        print(f"서버가 거부했다({r.status_code}): {body.get('error', body)}", file=sys.stderr)
        return 1
    print(report(body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
