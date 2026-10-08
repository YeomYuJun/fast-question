"""design/ 의 토큰과 글꼴을 web/static/ 으로 복사한다. 화면은 오프라인으로 돌아야 해서 CDN 을 쓰지 않는다.

design/ 은 claude.ai Design System 에서 내보낸 사본이라 git 에 올리지 않는다. 복사본(web/static/tokens.css,
web/static/fonts/)은 커밋한다. 디자인을 다시 내보낸 뒤 이 스크립트를 돌린다.

사용: .venv\\Scripts\\python.exe scripts\\sync_design.py
"""
import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESIGN = ROOT / "design"
STATIC = ROOT / "web" / "static"
# 글꼴은 OFL 이라 라이선스 파일을 함께 둔다
FILES = ["tokens.css"] + [f"fonts/{p.name}" for p in sorted((DESIGN / "fonts").glob("*"))
                          if p.suffix in (".woff2", ".md")]


def sync() -> list[str]:
    changed = []
    for rel in FILES:
        src, dst = DESIGN / rel, STATIC / rel
        if dst.exists() and filecmp.cmp(src, dst, shallow=False):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        changed.append(rel)
    return changed


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if not (DESIGN / "tokens.css").exists():
        print(f"design 폴더가 없다: {DESIGN}")
        return 1
    changed = sync()
    print("복사함: " + ", ".join(changed) if changed else "바뀐 파일 없음")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
