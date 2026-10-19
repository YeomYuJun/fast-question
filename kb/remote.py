"""사용자 PC 가 원격 서버에 붙을 때 쓰는 자격 증명 파일과 로컬 근거 판정.

위치: %USERPROFILE%\\.fast-question\\credentials.json (환경 변수 FQ_CREDENTIALS 로 바꾼다).
내용: {"url": "https://도메인", "token": "fq_pat_…", "device_id": "<이 PC 의 uuid>"}. 읽기 utf-8-sig, 쓰기 BOM 없는 utf-8.
이 파일이 있으면 즉석 질문 훅과 run_mcp.py --remote 가 원격 서버를 쓴다.
device_id 는 이 PC 가 캡처한 근거에 origin 으로 붙고(captured_json 의 origin 키), 로컬 검사기는 자기 PC 의 근거만
판정한다. 다른 PC 에서 등록한 근거는 이 PC 에 파일이 없어 "파일이 없어졌다" 로 잘못 판정되기 때문이다.
PC 클라이언트 패키지(pyproject.toml)에 들어가는 모듈이라 표준 라이브러리 밖의 것과 서버 모듈(kb.control 등)을 import 하지 않는다.
"""
import json
import os
import uuid
from pathlib import Path

TOKEN_PREFIX = "fq_pat_"  # 개인 토큰 앞부분. 서버(kb/control.py)가 이 값을 가져다 쓴다


def home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".fast-question"


def credentials_path() -> Path:
    env = os.environ.get("FQ_CREDENTIALS")
    return Path(env) if env else home() / "credentials.json"


def load_credentials(path: Path | None = None) -> dict | None:
    """{"url", "token", "device_id"}. 파일이 없거나 형식이 틀리거나 사용자 폴더를 모르면 None.
    device_id 가 없는 옛 파일이면 새로 만들어 파일에 적는다."""
    try:
        path = path or credentials_path()
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RuntimeError):  # RuntimeError: 환경 변수가 없어 사용자 폴더를 모른다
        return None
    url, token = str(data.get("url", "")).rstrip("/"), str(data.get("token", ""))
    if not url.startswith(("https://", "http://")) or not token.startswith(TOKEN_PREFIX):
        return None
    device_id = str(data.get("device_id") or "")
    if not device_id:
        try:
            device_id = json.loads(save_credentials(url, token, path=path).read_text(encoding="utf-8"))["device_id"]
        except OSError:
            device_id = ""
    return {"url": url, "token": token, "device_id": device_id}


def save_credentials(url: str, token: str, path: Path | None = None, device_id: str | None = None) -> Path:
    """자격 증명을 쓴다. device_id 를 주지 않으면 파일에 있던 것을 두고, 없으면 새로 만든다."""
    url, token = url.strip().rstrip("/"), token.strip()
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"서버 주소는 https:// 로 시작한다: {url}")
    if not token.startswith(TOKEN_PREFIX):
        raise ValueError(f"개인 토큰은 {TOKEN_PREFIX} 로 시작한다.")
    path = path or credentials_path()
    if not device_id:
        try:
            device_id = json.loads(path.read_text(encoding="utf-8-sig")).get("device_id")
        except (OSError, ValueError):
            device_id = None
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"url": url, "token": token, "device_id": device_id or str(uuid.uuid4())}
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def judge(rows: list[dict]) -> list[dict]:
    """서버가 준 근거 행을 이 PC 의 파일·git 으로 판정한다(kb.evidence.Checker 그대로)."""
    from kb.evidence import Checker
    checker = Checker()
    out = []
    for r in rows:
        verdict, detail = checker.check(r)
        out.append({"evidence_id": r["id"], "version": r["version"], "verdict": verdict, "detail": detail})
    return out
