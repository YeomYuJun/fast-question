"""원격 모드 로컬 검사기. 이 PC 에서 등록한 근거(파일·폴더·git)를 이 PC 의 파일로 판정해 서버에 보고한다.

서버는 사용자 PC 의 파일을 읽을 수 없어 근거를 판정하지 못한다. 이 스크립트가 /api/evidence/check 로 이 PC(device_id)의
근거를 받아 kb.evidence.Checker 로 판정하고 /api/evidence/verdicts 로 보낸다. 주소·토큰·device_id 는
credentials.json(kb/remote.py)에서 읽는다. 결과 한 줄을 %USERPROFILE%\\.fast-question\\check.log 에 덧붙인다.
사용: .venv\\Scripts\\python.exe scripts\\local_check.py [--qa <id>]
매일 돌리기: .venv\\Scripts\\python.exe scripts\\autostart.py --check-install
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kb.remote import credentials_path, home, judge, load_credentials  # noqa: E402

TIMEOUT_S = 60


def _call(cred: dict, path: str, body: dict | None = None, opener=urlopen) -> dict:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": f"Bearer {cred['token']}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    with opener(Request(cred["url"] + path, data=data, headers=headers), timeout=TIMEOUT_S) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8"))


def check(cred: dict, qa: int | None = None, opener=urlopen) -> dict:
    params = {"origin": cred["device_id"], **({"qa": qa} if qa is not None else {})}
    rows = _call(cred, "/api/evidence/check?" + urlencode(params), opener=opener)["rows"]
    if not rows:
        return {"results": [], "items": []}
    return _call(cred, "/api/evidence/verdicts", {"results": judge(rows)}, opener=opener)


def summary(res: dict) -> str:
    changed = sum(r["changed"] for r in res["results"])
    states = {}
    for item in res["items"]:
        states[item["evidence_state"]] = states.get(item["evidence_state"], 0) + 1
    tail = ", ".join(f"{k} {v}" for k, v in sorted(states.items()))
    return f"판정 {len(res['results'])}건, 판정이 바뀐 근거 {changed}건" + (f" (항목 상태: {tail})" if tail else "")


def main(argv: list[str]) -> int:
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cred = load_credentials()
    if cred is None or not cred.get("device_id"):
        print(f"원격 자격 증명이 없거나 형식이 틀렸다: {credentials_path()} (scripts/install.py --remote 로 만든다)")
        return 1
    qa = int(argv[argv.index("--qa") + 1]) if "--qa" in argv else None
    try:
        line = summary(check(cred, qa))
        code = 0
    except Exception as e:  # noqa: BLE001 - 작업 스케줄러에서 돌 때도 결과를 로그에 남긴다
        line, code = f"실패: {type(e).__name__}: {e}", 1
    print(line)
    try:
        log = home() / "check.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except (OSError, RuntimeError):
        pass
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
