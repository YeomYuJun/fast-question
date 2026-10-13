"""Skill 을 ~/.claude/skills 로 복사하고, MCP 서버 fast-question 을 Claude Code 사용자 범위에 등록하고,
즉석 질문 훅(hooks/ask_kb.py)을 ~/.claude/settings.json 의 UserPromptSubmit 에 등록한다.

사용: .venv\\Scripts\\python.exe scripts\\install.py
훅만 지우기: .venv\\Scripts\\python.exe scripts\\install.py --remove-hook
되돌리기: 위 훅 지우기, claude mcp remove -s user fast-question, ~/.claude/skills 아래 qa·qa-harvest 폴더 삭제
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

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
ROOT_MARK = "{{FAST_QUESTION_ROOT}}"  # Skill 원본 md 에서 저장소 경로 자리. 설치할 때 실제 경로로 바꾼다


def _files(top: Path) -> dict[str, bytes]:
    return {p.relative_to(top).as_posix(): p.read_bytes() for p in sorted(top.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts}


def _render(src: Path, root: Path) -> dict[str, bytes]:
    """Skill 원본의 md 에 적힌 ROOT_MARK 를 이 저장소의 실제 경로로 바꾼다."""
    mark, real = ROOT_MARK.encode("utf-8"), root.as_posix().encode("utf-8")
    return {rel: data.replace(mark, real) if rel.endswith(".md") else data for rel, data in _files(src).items()}


def install_skill(src: Path, dst: Path, root: Path = ROOT) -> str:
    files = _render(src, root)
    existed = dst.exists()
    if existed:
        if _files(dst) == files:
            return f"Skill 그대로: {dst}"
        shutil.rmtree(dst)
    for rel, data in files.items():
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        (dst / rel).write_bytes(data)
    return f"Skill 덮어씀(내용이 달랐다): {dst}" if existed else f"Skill 복사: {dst}"


def install_skills(src_root: Path = SKILLS_SRC, dst_root: Path = SKILLS_DST, root: Path = ROOT) -> list[str]:
    return [install_skill(d, dst_root / d.name, root) for d in sorted(src_root.iterdir())
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


def register_mcp() -> str:
    claude = shutil.which("claude")
    if claude is None:
        return "claude CLI 를 찾지 못해 MCP 등록을 건너뜀"
    got = subprocess.run([claude, "mcp", "get", NAME], capture_output=True, timeout=60)
    if got.returncode == 0:
        return f"MCP 이미 등록됨: {NAME}"
    r = subprocess.run([claude, "mcp", "add", "-s", "user", NAME, "--", str(PYTHON), str(ENTRY)],
                       capture_output=True, timeout=60)
    out = (r.stdout + r.stderr).decode("utf-8", errors="replace").strip()
    return f"MCP 등록 {'완료' if r.returncode == 0 else '실패'}: {out}"


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if "--remove-hook" in argv:
        print(remove_hook())
        return 0
    if not PYTHON.exists():
        print(f"가상환경이 없다: {PYTHON}")
        return 1
    for msg in install_skills():
        print(msg)
    print(register_mcp())
    print(install_hook())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
