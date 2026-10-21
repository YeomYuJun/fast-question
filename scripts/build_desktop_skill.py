"""claude.ai·Claude Desktop 에 올리는 qa Skill 파일(.skill)을 만든다.

사용: .venv\\Scripts\\python.exe scripts\\build_desktop_skill.py [--out "<파일>"]
        기본 출력은 dist/qa.skill 이다. Claude 설정의 Skill 화면에서 이 파일을 올린다.
claude.ai·Desktop 은 fast-question 원격 서버에 커넥터로 직접 붙는다. 원격 서버는 사용자 PC 의 파일을 읽지 못하므로
skill/qa 원본에서 `<!-- remote -->` 구간을 남기고 `<!-- local -->` 구간(파일 근거, 즉석 질문 훅, PC 클라이언트 명령)을 뺀다
(scripts/install.py 의 render). qa-harvest 는 사용자 PC 의 폴더와 PC 클라이언트 명령이 있어야 해서 넣지 않는다.
.skill 은 zip 이고 안에 `qa/SKILL.md` 처럼 Skill 폴더 이름이 앞에 붙는다(skill-creator 의 package_skill 과 같은 모양).
"""
import argparse
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.install import SKILLS_SRC, render  # noqa: E402

NAME = "qa"
OUT = ROOT / "dist" / f"{NAME}.skill"
ZIP_TIME = (1980, 1, 1, 0, 0, 0)  # 내용이 같으면 같은 파일이 나오게 시각을 고정한다
DESCRIPTION_MAX = 1024  # claude.ai 업로드 검사(skill-creator quick_validate 와 같은 기준)


def frontmatter(skill_md: bytes) -> dict[str, str]:
    m = re.match(r"---\n(.*?)\n---\n", skill_md.decode("utf-8"), re.DOTALL)
    if not m:
        raise ValueError("SKILL.md 앞머리(---)가 없다")
    return dict(line.split(": ", 1) for line in m.group(1).splitlines())


def skill_files() -> dict[str, bytes]:
    """zip 안 경로 → 내용. 업로드에서 거부될 모양이면 ValueError 를 낸다."""
    # git 이 원본을 CRLF 로 꺼내는 PC(core.autocrlf)에서도 같은 결과가 나오게 LF 로 맞춘다
    files = {f"{NAME}/{rel}": data.replace(b"\r\n", b"\n")
             for rel, data in render(SKILLS_SRC / NAME, {}, variant="remote").items()}
    for rel, data in files.items():
        text = data.decode("utf-8")
        if "{{" in text or "<!--" in text:
            raise ValueError(f"자리표시나 구간 표시가 남았다: {rel}")
    meta = frontmatter(files[f"{NAME}/SKILL.md"])
    if set(meta) != {"name", "description"} or meta["name"] != NAME:
        raise ValueError(f"SKILL.md 앞머리는 name·description 한 줄씩이어야 한다: {sorted(meta)}")
    desc = meta["description"]
    if "<" in desc or ">" in desc or len(desc) > DESCRIPTION_MAX:
        raise ValueError(f"description 에 꺾쇠가 있거나 {DESCRIPTION_MAX}자를 넘는다({len(desc)}자)")
    return files


def write(out: Path, files: dict[str, bytes]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, data in sorted(files.items()):
            z.writestr(zipfile.ZipInfo(rel, ZIP_TIME), data, zipfile.ZIP_DEFLATED)


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="claude.ai·Claude Desktop 용 qa Skill 파일 만들기")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    files = skill_files()
    write(args.out, files)
    print(f"Skill 파일: {args.out} ({', '.join(sorted(files))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
