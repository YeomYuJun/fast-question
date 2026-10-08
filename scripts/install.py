"""Skill 을 ~/.claude/skills/qa 로 복사하고, MCP 서버 fast-question 을 Claude Code 사용자 범위에 등록한다.

사용: .venv\\Scripts\\python.exe scripts\\install.py
되돌리기: claude mcp remove -s user fast-question, ~/.claude/skills/qa 폴더 삭제
"""
import filecmp
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_SRC = ROOT / "skill" / "qa"
SKILL_DST = Path.home() / ".claude" / "skills" / "qa"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
ENTRY = ROOT / "run_mcp.py"
NAME = "fast-question"


def install_skill() -> str:
    if SKILL_DST.exists():
        cmp = filecmp.dircmp(SKILL_SRC, SKILL_DST)
        if not (cmp.left_only or cmp.right_only or cmp.diff_files):
            return f"Skill 그대로: {SKILL_DST}"
        shutil.rmtree(SKILL_DST)
        shutil.copytree(SKILL_SRC, SKILL_DST)
        return f"Skill 덮어씀(내용이 달랐다): {SKILL_DST}"
    SKILL_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SKILL_SRC, SKILL_DST)
    return f"Skill 복사: {SKILL_DST}"


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


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if not PYTHON.exists():
        print(f"가상환경이 없다: {PYTHON}")
        return 1
    print(install_skill())
    print(register_mcp())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
