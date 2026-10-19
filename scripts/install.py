"""Skill 을 ~/.claude/skills 로 복사하고, MCP 서버 fast-question 을 Claude Code 사용자 범위에 등록하고,
즉석 질문 훅(hooks/ask_kb.py)을 ~/.claude/settings.json 의 UserPromptSubmit 에 등록하고,
config.json 의 DB 에 기본 사용 가이드(kb/default_guide.json)를 넣는다.

사용: .venv\\Scripts\\python.exe scripts\\install.py
원격 서버 쓰기: .venv\\Scripts\\python.exe scripts\\install.py --remote https://<도메인>
  개인 토큰(fq_pat_…)을 표준 입력으로 받아 서버의 /api/me 로 확인한 뒤 %USERPROFILE%\\.fast-question\\credentials.json
  에 쓰고, MCP 를 로컬 프록시(run_mcp.py --remote)로 등록한다. 기본 가이드는 서버가 넣는다.
  로컬로 돌아가기: credentials.json 을 지우고 인자 없이 다시 실행한다(MCP 가 로컬로 다시 등록된다).
훅만 지우기: .venv\\Scripts\\python.exe scripts\\install.py --remove-hook
되돌리기: 위 훅 지우기, claude mcp remove -s user fast-question, ~/.claude/skills 아래 qa·qa-harvest 폴더 삭제
Claude Code 플러그인(plugin/fast-question, scripts/build_plugin.py)과 함께 설치하면 MCP·즉석 질문 훅·Skill 이
두 벌 걸린다. 둘 중 하나만 쓴다.
"""
import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
SKILLS_SRC = ROOT / "skill"
SKILLS_DST = Path.home() / ".claude" / "skills"
SETTINGS = Path.home() / ".claude" / "settings.json"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
ENTRY = ROOT / "run_mcp.py"
HOOK = ROOT / "hooks" / "ask_kb.py"
HOOK_EVENT = "UserPromptSubmit"
HOOK_TIMEOUT_S = 10
NAME = "fast-question"
# Skill 원본 md 의 자리표시. 설치 방식마다 값이 다르다(이 스크립트는 로컬 설치, scripts/build_plugin.py 는 플러그인)
CLI_MARK = "{{FQ_CLI}}"  # PC 클라이언트 명령(fq_client/cli.py) 실행 방법
RULES_MARK = "{{QA_RULES}}"  # qa Skill 의 rules.md 위치


def local_marks(root: Path = ROOT) -> dict[str, str]:
    """이 저장소의 가상환경 python 으로 fq_client/cli.py 를 부른다. rules.md 는 ~/.claude/skills/qa 에 복사된다."""
    return {CLI_MARK: f'"{(root / ".venv" / "Scripts" / "python.exe").as_posix()}" "{(root / "fq_client" / "cli.py").as_posix()}"',
            RULES_MARK: "~/.claude/skills/qa/rules.md"}


def _files(top: Path) -> dict[str, bytes]:
    return {p.relative_to(top).as_posix(): p.read_bytes() for p in sorted(top.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts}


def render(src: Path, marks: dict[str, str]) -> dict[str, bytes]:
    """Skill 폴더의 파일. md 에 적힌 자리표시를 marks 의 값으로 바꾼다."""
    def fill(data: bytes) -> bytes:
        for mark, real in marks.items():
            data = data.replace(mark.encode("utf-8"), real.encode("utf-8"))
        return data

    return {rel: fill(data) if rel.endswith(".md") else data for rel, data in _files(src).items()}


def install_skill(src: Path, dst: Path, marks: dict[str, str] | None = None) -> str:
    files = render(src, local_marks() if marks is None else marks)
    existed = dst.exists()
    if existed:
        if _files(dst) == files:
            return f"Skill 그대로: {dst}"
        shutil.rmtree(dst)
    for rel, data in files.items():
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        (dst / rel).write_bytes(data)
    return f"Skill 덮어씀(내용이 달랐다): {dst}" if existed else f"Skill 복사: {dst}"


def install_skills(src_root: Path = SKILLS_SRC, dst_root: Path = SKILLS_DST,
                   marks: dict[str, str] | None = None) -> list[str]:
    return [install_skill(d, dst_root / d.name, marks) for d in sorted(src_root.iterdir())
            if d.is_dir() and (d / "SKILL.md").exists()]


def hook_command() -> str:
    return f'"{PYTHON.as_posix()}" "{HOOK.as_posix()}"'


def _is_ours(hook: dict) -> bool:
    return "hooks/ask_kb.py" in str(hook.get("command", "")).replace("\\", "/")


def _read(settings: Path) -> dict:
    # 윈도우에서 만든 JSON 은 BOM 이 붙어 온다
    return json.loads(settings.read_text(encoding="utf-8-sig")) if settings.exists() else {}


def _write(settings: Path, data: dict) -> None:
    backup = settings.with_name(settings.name + ".fast-question.bak")
    if settings.exists() and not backup.exists():  # 처음 고치기 전의 원본만 남긴다
        shutil.copy2(settings, backup)
    tmp = settings.with_name(settings.name + ".tmp")
    # settings.json 은 LF 로 저장되어 있다. 윈도우 기본 줄바꿈(CRLF)으로 바꾸지 않는다
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, settings)


def install_hook(settings: Path = SETTINGS) -> str:
    data = _read(settings)
    groups = data.setdefault("hooks", {}).setdefault(HOOK_EVENT, [])
    if any(_is_ours(h) for g in groups for h in g.get("hooks", [])):
        return f"훅 이미 등록됨: {settings} {HOOK_EVENT}"
    groups.append({"hooks": [{"type": "command", "command": hook_command(), "timeout": HOOK_TIMEOUT_S}]})
    _write(settings, data)
    return f"훅 등록: {settings} {HOOK_EVENT} (처음 고치기 전 원본: {settings.name}.fast-question.bak)"


def remove_hook(settings: Path = SETTINGS) -> str:
    data = _read(settings)
    groups = data.get("hooks", {}).get(HOOK_EVENT, [])
    kept = []
    for g in groups:
        hooks = [h for h in g.get("hooks", []) if not _is_ours(h)]
        if hooks:
            kept.append(dict(g, hooks=hooks))
    if kept == groups:
        return f"훅 없음: {settings}"
    if kept:
        data["hooks"][HOOK_EVENT] = kept
    else:
        del data["hooks"][HOOK_EVENT]
    _write(settings, data)
    return f"훅 제거: {settings} {HOOK_EVENT}"


def register_mcp(remote: bool = False, run=subprocess.run, claude: str | None = None) -> str:
    """로컬은 run_mcp.py, 원격은 run_mcp.py --remote 로 사용자 범위에 등록한다. 다른 방식으로 등록돼 있으면 바꾼다."""
    claude = claude or shutil.which("claude")
    if claude is None:
        return "claude CLI 를 찾지 못해 MCP 등록을 건너뜀"
    got = run([claude, "mcp", "get", NAME], capture_output=True, timeout=60)
    if got.returncode == 0:
        if (b"--remote" in got.stdout) == remote:
            return f"MCP 이미 등록됨: {NAME}"
        run([claude, "mcp", "remove", "-s", "user", NAME], capture_output=True, timeout=60)
    args = [str(PYTHON), str(ENTRY)] + (["--remote"] if remote else [])
    r = run([claude, "mcp", "add", "-s", "user", NAME, "--", *args], capture_output=True, timeout=60)
    out = (r.stdout + r.stderr).decode("utf-8", errors="replace").strip()
    return f"MCP 등록 {'완료' if r.returncode == 0 else '실패'}({'원격 프록시' if remote else '로컬'}): {out}"


def seed_guide() -> str:
    """config.json 의 DB 에 기본 사용 가이드를 넣는다. DB 가 없으면 만든다."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from kb import guide
    from kb.config import load_config
    from kb.db import connect
    from kb.store import Store
    cfg = load_config()
    conn = connect(cfg.db_path)
    try:
        return f"기본 가이드: {guide.seed(Store(conn, 'system'))} ({cfg.db_path})"
    finally:
        conn.close()


def read_token() -> str:
    """개인 토큰을 표준 입력으로 받는다. 명령행 인자로 받지 않는다(PowerShell 히스토리에 남는다)."""
    if sys.stdin.isatty():
        return getpass.getpass("개인 토큰(화면의 연결 페이지에서 만든 fq_pat_…): ").strip()
    return sys.stdin.readline().strip()


def check_token(url: str, token: str, opener=urlopen) -> str:
    """서버의 /api/me 로 토큰을 확인하고 계정 표시 이름을 돌려준다. 토큰이 틀리면 ValueError."""
    req = Request(url.rstrip("/") + "/api/me", headers={"Authorization": f"Bearer {token}"})
    try:
        with opener(req, timeout=10) as r:  # noqa: S310 - 사용자가 준 서버 주소만 연다
            acc = json.loads(r.read().decode("utf-8")).get("account") or {}
    except HTTPError as e:
        if e.code == 401:
            raise ValueError("서버가 이 토큰을 받지 않았다(틀렸거나 폐기·만료됨).") from None
        raise
    return acc.get("display_name") or ", ".join(acc.get("providers", [])) or str(acc.get("id", ""))


def install_remote(url: str, token: str, opener=urlopen) -> list[str]:
    """토큰을 확인한 뒤 자격 증명 파일을 쓴다. 확인에 실패하면 파일을 쓰지 않는다."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from kb.remote import save_credentials
    who = check_token(url, token, opener)
    return [f"토큰 확인: {who}", f"자격 증명: {save_credentials(url, token)}"]


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if "--remove-hook" in argv:
        print(remove_hook())
        return 0
    if not PYTHON.exists():
        print(f"가상환경이 없다: {PYTHON}")
        return 1
    if "--remote" in argv:
        i = argv.index("--remote")
        if i + 1 >= len(argv):
            print("--remote 뒤에 서버 주소(https://도메인)를 준다.")
            return 1
        url = argv[i + 1].rstrip("/")
        try:
            for msg in install_remote(url, read_token()):
                print(msg)
        except (ValueError, OSError) as e:
            print(f"원격 설정 실패: {e}")
            return 1
        for msg in install_skills():
            print(msg)
        print(register_mcp(remote=True))
        print(install_hook())
        return 0
    for msg in install_skills():
        print(msg)
    print(register_mcp())
    print(install_hook())
    print(seed_guide())
    from kb.remote import credentials_path, load_credentials
    if load_credentials() is not None:
        print(f"원격 자격 증명이 있어 즉석 질문 훅은 원격 서버를 쓴다: {credentials_path()} (로컬만 쓰려면 이 파일을 지운다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
