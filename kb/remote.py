"""사용자 PC 가 원격 서버에 붙을 때 쓰는 자격 증명 파일.

위치: %USERPROFILE%\\.fast-question\\credentials.json (환경 변수 FQ_CREDENTIALS 로 바꾼다).
내용: {"url": "https://도메인", "token": "fq_pat_…"}. 읽기 utf-8-sig, 쓰기 BOM 없는 utf-8.
이 파일이 있으면 즉석 질문 훅과 run_mcp.py --remote 가 원격 서버를 쓴다.
"""
import json
import os
from pathlib import Path

from kb.control import TOKEN_PREFIX


def home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".fast-question"


def credentials_path() -> Path:
    env = os.environ.get("FQ_CREDENTIALS")
    return Path(env) if env else home() / "credentials.json"


def load_credentials(path: Path | None = None) -> dict | None:
    """{"url", "token"}. 파일이 없거나 형식이 틀리거나 사용자 폴더를 모르면 None."""
    try:
        path = path or credentials_path()
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RuntimeError):  # RuntimeError: 환경 변수가 없어 사용자 폴더를 모른다
        return None
    url, token = str(data.get("url", "")).rstrip("/"), str(data.get("token", ""))
    if not url.startswith(("https://", "http://")) or not token.startswith(TOKEN_PREFIX):
        return None
    return {"url": url, "token": token}


def save_credentials(url: str, token: str, path: Path | None = None) -> Path:
    url, token = url.strip().rstrip("/"), token.strip()
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"서버 주소는 https:// 로 시작한다: {url}")
    if not token.startswith(TOKEN_PREFIX):
        raise ValueError(f"개인 토큰은 {TOKEN_PREFIX} 로 시작한다.")
    path = path or credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": url, "token": token}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
