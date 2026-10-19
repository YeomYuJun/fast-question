"""Claude Code 플러그인 폴더(plugin/fast-question)와 마켓플레이스 목록(.claude-plugin/marketplace.json)을 만든다.

사용: .venv\\Scripts\\python.exe scripts\\build_plugin.py
        저장소의 plugin/fast-question 과 .claude-plugin/marketplace.json 을 다시 만든다. 결과는 git 에 올린다
        (마켓플레이스가 저장소의 이 폴더를 그대로 읽는다). tests/test_plugin.py 가 원본과 같은지 확인한다.
      .venv\\Scripts\\python.exe scripts\\build_plugin.py --client "<wheel 경로>" --out "<폴더>"
        시험용: PyPI 대신 로컬 wheel 을 uvx 로 실행하는 플러그인을 다른 폴더에 만든다(claude --plugin-dir 로 띄운다).
플러그인의 MCP·즉석 질문 훅·Skill 명령은 PC 클라이언트(pyproject.toml 의 fast-question 패키지)를 uvx 로 실행한다.
버전은 fq_client/__init__.py 의 __version__ 하나다(플러그인 version 과 uvx 가 받을 패키지 버전).
Skill 은 skill/ 원본의 자리표시를 플러그인 값으로 바꿔 복사한다(scripts/install.py 의 render).
서버 주소는 플러그인에 두지 않는다. `fast-question login` 이 credentials.json 에 쓴 주소를 MCP·훅이 함께 읽는다.
릴리스 순서(플러그인이 고정한 버전이 PyPI 에 없으면 uvx 가 모두 실패한다):
  1. fq_client/__init__.py 의 __version__ 을 올리고 이 스크립트로 플러그인을 다시 만들어 커밋한다.
  2. 태그 v<버전> 을 push 한다. .github/workflows/publish-client.yml 이 PyPI 에 올린다.
  3. PyPI 에 그 버전이 보인 뒤에 main 에 병합한다(마켓플레이스는 기본 브랜치를 읽는다).
  클라이언트 코드·Skill·훅 설정 중 하나라도 바뀌면 버전을 올린다. 사용자는 버전이 바뀌어야 새 플러그인을 받는다.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fq_client import __version__  # noqa: E402
from scripts.install import CLI_MARK, RULES_MARK, SKILLS_SRC, render  # noqa: E402

NAME = "fast-question"
OWNER = "YeomYuJun"
REPO_URL = "https://github.com/YeomYuJun/fast-question"
SITE_URL = "https://fast-question.com"
PLUGIN_DIR = ROOT / "plugin" / NAME
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
HOOK_TIMEOUT_S = 10
DESCRIPTION = ("fast-question 지식 DB 연결: 원격 서버 MCP(파일 근거를 이 PC 에서 캡처), qa·qa-harvest Skill, "
               "질문하면 KB 를 먼저 찾아 주는 즉석 질문 훅")

README = """# fast-question Claude Code 플러그인

작업 중 생긴 질문과 짧은 답을 fast-question 지식 DB({site})에 쌓고 찾는 플러그인입니다.
이 폴더는 `scripts/build_plugin.py` 가 만듭니다. 직접 고치지 말고 `skill/` 원본과 그 스크립트를 고칩니다.

## 들어 있는 것

- MCP 서버 `fast-question`: 원격 서버의 `kb_*` 도구를 그대로 씁니다. 파일·git 근거는 이 PC 에서 캡처하고 판정합니다.
- Skill `/fast-question:qa`(등록·검색), `/fast-question:qa-harvest`(프로젝트 폴더에서 Q&A 구축)
- 즉석 질문 훅: 질문하면 KB 에서 맞는 Q&A 를 찾아 그 턴에 넣습니다.

MCP·훅·Skill 명령은 PC 클라이언트 패키지 `fast-question=={version}` 을 `uvx` 로 실행합니다.

## 설치

1. uv 를 설치합니다(`uvx` 가 PATH 에 있어야 합니다). Windows: `winget install --id astral-sh.uv -e`
2. 플러그인을 설치합니다.
   ```
   claude plugin marketplace add {owner}/{name}
   claude plugin install {name}@{name}
   ```
3. 로그인합니다. 브라우저에서 승인하면 개인 토큰이 `%USERPROFILE%\\.fast-question\\credentials.json` 에 저장됩니다.
   ```
   uvx --from fast-question=={version} fast-question login
   ```
4. Claude Code 를 다시 시작합니다(이미 열려 있으면 `/mcp` 에서 `fast-question` 을 다시 연결합니다).

## 설치 없이 쓰기

파일 근거와 즉석 질문 훅이 필요 없으면 플러그인 없이 원격 MCP 만 연결합니다.

```
claude mcp add --transport http -s user {name} {site}/mcp
```

## 주의

- 저장소의 `scripts/install.py` 로 설치한 적이 있으면 MCP·훅·Skill 이 두 벌 걸립니다. 둘 중 하나만 씁니다.
- 다른 서버를 쓰려면 `uvx --from fast-question=={version} fast-question login --url https://<주소>` 로 로그인합니다.
"""


def client_command(spec: str) -> list[str]:
    return ["uvx", "--from", spec, NAME]


def shell_command(spec: str) -> str:
    """Skill 문서에 적는 같은 명령. 경로에 공백이 있으면 따옴표로 감싼다."""
    return " ".join(f'"{a}"' if " " in a else a for a in client_command(spec))


def _json(data) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def plugin_files(spec: str) -> dict[str, bytes]:
    """플러그인 폴더 기준 경로 → 내용."""
    files = {
        ".claude-plugin/plugin.json": _json({
            "name": NAME, "version": __version__, "description": DESCRIPTION, "author": {"name": OWNER},
            "homepage": SITE_URL, "repository": REPO_URL, "keywords": ["knowledge-base", "qa", "mcp"]}),
        ".mcp.json": _json({"mcpServers": {NAME: {"command": "uvx", "args": client_command(spec)[1:] + ["mcp"]}}}),
        "hooks/hooks.json": _json({
            "description": "질문하면 fast-question KB 에서 맞는 Q&A 를 찾아 그 턴에 넣습니다",
            "hooks": {"UserPromptSubmit": [{"hooks": [{
                "type": "command", "command": "uvx", "args": client_command(spec)[1:] + ["hook"],
                "timeout": HOOK_TIMEOUT_S}]}]}}),
        "README.md": README.format(site=SITE_URL, version=__version__, owner=OWNER, name=NAME).encode("utf-8"),
    }
    marks = {CLI_MARK: shell_command(spec), RULES_MARK: "${CLAUDE_PLUGIN_ROOT}/skills/qa/rules.md"}
    for d in sorted(SKILLS_SRC.iterdir()):
        if d.is_dir() and (d / "SKILL.md").exists():
            # git 이 원본을 CRLF 로 꺼내는 PC(core.autocrlf)에서도 같은 결과가 나오게 LF 로 맞춘다
            files.update({f"skills/{d.name}/{rel}": data.replace(b"\r\n", b"\n")
                          for rel, data in render(d, marks).items()})
    return files


def marketplace() -> bytes:
    return _json({"name": NAME, "owner": {"name": OWNER},
                  "metadata": {"description": "fast-question 지식 DB 의 Claude Code 플러그인"},
                  "plugins": [{"name": NAME, "source": f"./plugin/{NAME}", "description": DESCRIPTION}]})


def write(out: Path, files: dict[str, bytes]) -> None:
    if out.exists():
        if not (out / ".claude-plugin" / "plugin.json").exists():
            raise SystemExit(f"플러그인 폴더가 아닌 곳은 지우지 않는다: {out}")
        shutil.rmtree(out)
    for rel, data in files.items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_bytes(data)


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="fast-question Claude Code 플러그인 만들기")
    ap.add_argument("--client", default=f"{NAME}=={__version__}", help="uvx --from 에 줄 값(패키지 버전 또는 wheel 경로)")
    ap.add_argument("--out", type=Path, default=PLUGIN_DIR)
    args = ap.parse_args(argv)
    write(args.out, plugin_files(args.client))
    print(f"플러그인: {args.out} (클라이언트 {args.client})")
    if args.out == PLUGIN_DIR:
        MARKETPLACE.parent.mkdir(exist_ok=True)
        MARKETPLACE.write_bytes(marketplace())
        print(f"마켓플레이스 목록: {MARKETPLACE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
