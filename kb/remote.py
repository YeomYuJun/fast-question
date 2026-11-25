"""사용자 PC 가 원격 서버에 붙을 때 쓰는 자격 증명 파일과 로컬 근거 판정.

위치: %USERPROFILE%\\.fast-question\\credentials.json (환경 변수 FQ_CREDENTIALS 로 바꾼다).
내용: {"url": "https://도메인", "token": "fq_pat_…", "device_id": "<이 PC 의 uuid>", "mode": "local" | "remote"}.
읽기 utf-8-sig, 쓰기 BOM 없는 utf-8.
mode 는 저장소 설치(scripts/install.py)가 이 PC 의 MCP·즉석 질문 훅을 로컬 DB 와 원격 서버 중 어디에 붙였는지다.
install.py --local·--remote 가 쓰고, 다른 쓰기(login, device_id 보충)는 그대로 둔다. 저장소의 즉석 질문 훅은
mode 가 local 이면 로컬 DB 를, 그 밖(remote, 키 없음)이면 원격 서버를 쓴다. 플러그인(fq_client)은 원격 전용이라
mode 를 보지 않는다. 로컬 모드에서도 이 파일은 지우지 않는다(push_local·local_check 가 쓰고, 지우면 device_id 가 바뀐다).
device_id 는 이 PC 가 캡처한 근거에 origin 으로 붙고(captured_json 의 origin 키), 로컬 검사기는 자기 PC 의 근거만
판정한다. 다른 PC 에서 등록한 근거는 이 PC 에 파일이 없어 "파일이 없어졌다" 로 잘못 판정되기 때문이다.
PC 클라이언트 패키지(pyproject.toml)에 들어가는 모듈이라 표준 라이브러리 밖의 것과 서버 모듈(kb.control 등)을 import 하지 않는다.
"""
import json
import os
import uuid
from pathlib import Path

TOKEN_PREFIX = "fq_pat_"  # 개인 토큰 앞부분. 서버(kb/control.py)가 이 값을 가져다 쓴다
MODES = ("local", "remote")


def home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".fast-question"


def credentials_path() -> Path:
    env = os.environ.get("FQ_CREDENTIALS")
    return Path(env) if env else home() / "credentials.json"


def load_credentials(path: Path | None = None) -> dict | None:
    """{"url", "token", "device_id", "mode"}. 파일이 없거나 형식이 틀리거나 사용자 폴더를 모르면 None.
    mode 는 키가 없거나 MODES 밖의 값이면 None 이다. device_id 가 없는 옛 파일이면 새로 만들어 파일에 적는다."""
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
    mode = data.get("mode") if data.get("mode") in MODES else None
    return {"url": url, "token": token, "device_id": device_id, "mode": mode}


def save_credentials(url: str, token: str, path: Path | None = None, device_id: str | None = None,
                     mode: str | None = None) -> Path:
    """자격 증명을 쓴다. device_id·mode 를 주지 않으면 파일에 있던 것을 둔다. device_id 가 없으면 새로 만든다."""
    url, token = url.strip().rstrip("/"), token.strip()
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"The server address must start with https://: {url}")
    if not token.startswith(TOKEN_PREFIX):
        raise ValueError(f"The personal token must start with {TOKEN_PREFIX}.")
    if mode is not None and mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}: {mode}")
    path = path or credentials_path()
    try:
        old = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        old = {}
    if not isinstance(old, dict):
        old = {}
    body = {"url": url, "token": token, "device_id": device_id or old.get("device_id") or str(uuid.uuid4())}
    if (mode or old.get("mode")) in MODES:
        body["mode"] = mode or old["mode"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def set_mode(mode: str, path: Path | None = None) -> Path:
    """자격 증명 파일의 mode 만 바꾼다. 주소·토큰·device_id 는 그대로 둔다. 읽을 자격 증명이 없으면 ValueError."""
    cred = load_credentials(path)
    if cred is None:
        raise ValueError(f"Remote credentials are missing or malformed: {path or credentials_path()}")
    return save_credentials(cred["url"], cred["token"], path=path, device_id=cred["device_id"], mode=mode)


def settings_path() -> Path:
    return home() / "settings.json"


def study_categories(path: Path | None = None) -> list[str]:
    """이 PC 에서 학습용으로 둔 대분류 이름(소문자). settings.json 의 {"study_categories": ["Kubernetes"]}.
    학습용 대분류의 Q&A 는 사용자가 공부하려고 쌓은 일반 지식이다. 로컬 프록시(mcp_server/proxy.py set_apart)가
    kb_search 결과에서 따로 떼어, Claude 가 프로젝트의 사실을 찾을 때 섞이지 않게 한다. 웹 화면과 즉석 질문 훅은 그대로 보여 준다.
    파일이 없거나 형식이 틀리면 빈 목록이다."""
    try:
        data = json.loads((path or settings_path()).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RuntimeError):
        return []
    names = data.get("study_categories") if isinstance(data, dict) else None
    return [str(n).strip().lower() for n in names if str(n).strip()] if isinstance(names, list) else []


def judge(rows: list[dict]) -> list[dict]:
    """서버가 준 근거 행을 이 PC 의 파일·git 으로 판정한다(kb.evidence.Checker 그대로)."""
    from kb.evidence import Checker
    checker = Checker()
    out = []
    for r in rows:
        verdict, detail = checker.check(r)
        out.append({"evidence_id": r["id"], "version": r["version"], "verdict": verdict, "detail": detail})
    return out
