"""qa-harvest 1단계: 대상 폴더의 목록을 결정적으로 뽑는다. 파일 내용은 읽지 않는다.

사용: python inventory.py <대상 폴더> [<대상 폴더> ...] [--since <기록 JSON>] [--out <출력 JSON>]
  --since: {"repos": {"<저장소 경로>": "<기록한 HEAD sha>"}} 형식(kb_harvest get 의 record). 주면 저장소마다
           그 sha 이후 바뀐 파일과 커밋을 함께 낸다.
출력(UTF-8 JSON): 대상 폴더마다 git 저장소(HEAD, 추적 파일 수, 확장자·최상위 폴더 분포, 자주 바뀐 파일,
오류 수정 커밋), git 이 아닌 최상위 폴더(파일 수, 확장자 분포), 문서 파일 목록.
"""
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "target", "build", "dist", ".gradle", ".idea", ".vscode", "__pycache__",
             ".venv", "venv", ".next", ".nuxt", "coverage", ".understand-anything", ".superpowers"}
DOC_EXT = {".md", ".txt", ".xlsx", ".xls", ".xlsm", ".csv", ".hwp", ".hwpx", ".pdf", ".docx", ".doc",
           ".pptx", ".ppt"}
REPO_DEPTH = 3
LOG_LIMIT = 3000
HOT_LIMIT = 30
FIX_LIMIT = 40
DOC_LIMIT = 400
FIX_SUBJECT = re.compile(r"(?i)\b(fix|fixed|hotfix|revert|bug|error)\b|버그|오류|장애|에러|해결|원인|문제")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=false", "-c", "i18n.logOutputEncoding=utf-8",
                        *args], capture_output=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    return r.stdout if r.returncode == 0 else ""


def _ext(name: str) -> str:
    return Path(name).suffix.lower() or "(없음)"


def find_repos(root: Path) -> list[Path]:
    repos = []
    for cur, dirs, _ in os.walk(root):
        p = Path(cur)
        if (p / ".git").exists():
            repos.append(p)
            dirs[:] = []
            continue
        if len(p.relative_to(root).parts) >= REPO_DEPTH:
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
    return repos


def _log(repo: Path, rev: str | None = None) -> list[dict]:
    """커밋마다 sha, 날짜, 제목, 바뀐 파일."""
    args = ["log", "--format=%x00%H%x09%ad%x09%s", "--date=short", "--name-only"]
    args += [rev] if rev else ["-n", str(LOG_LIMIT)]
    commits = []
    for block in git(repo, *args).split("\0")[1:]:
        head, _, rest = block.partition("\n")
        sha, date, subject = (head.split("\t", 2) + ["", ""])[:3]
        commits.append({"sha": sha[:10], "date": date, "subject": subject,
                        "files": [f for f in rest.splitlines() if f.strip()]})
    return commits


def repo_info(repo: Path, since: str | None) -> dict:
    files = [f for f in git(repo, "ls-files").splitlines() if f]
    commits = _log(repo)
    hot = Counter(f for c in commits for f in c["files"])
    info = {
        "path": str(repo),
        "head": git(repo, "rev-parse", "HEAD").strip(),
        "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip(),
        "files": len(files),
        "ext": dict(Counter(_ext(f) for f in files).most_common()),
        "dirs": dict(Counter(f.split("/", 1)[0] if "/" in f else "." for f in files).most_common(40)),
        "commits_seen": len(commits),
        "hot_files": [{"path": p, "changes": n} for p, n in hot.most_common(HOT_LIMIT)],
        "fix_commits": [c for c in commits if FIX_SUBJECT.search(c["subject"])][:FIX_LIMIT],
        "documents": [f for f in files if _ext(f) in DOC_EXT],
    }
    if since:
        changed = git(repo, "diff", "--name-only", since, "HEAD").splitlines()
        info["since"] = {"sha": since, "changed": [f for f in changed if f],
                         "commits": [{k: c[k] for k in ("sha", "date", "subject")} for c in _log(repo, f"{since}..HEAD")]}
    return info


def walk_plain(top: Path, repos: set[Path]) -> tuple[Counter, list[Path]]:
    ext: Counter = Counter()
    docs: list[Path] = []
    for cur, dirs, names in os.walk(top):
        p = Path(cur)
        if p in repos:
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and p / d not in repos)
        for n in names:
            ext[_ext(n)] += 1
            if _ext(n) in DOC_EXT:
                docs.append(p / n)
    return ext, docs


def root_info(root: Path, since: dict[str, str]) -> dict:
    repos = find_repos(root)
    repo_set = set(repos)
    since_by_path = {Path(k).resolve(): v for k, v in since.items()}
    repo_infos = [repo_info(r, since_by_path.get(r.resolve())) for r in repos]
    plain_dirs, documents = [], []
    loose = Counter()
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        if entry.is_file():
            loose[_ext(entry.name)] += 1
            if _ext(entry.name) in DOC_EXT:
                documents.append(entry)
        elif entry.is_dir() and entry.name not in SKIP_DIRS and entry not in repo_set:
            ext, docs = walk_plain(entry, repo_set)
            plain_dirs.append({"path": str(entry), "files": sum(ext.values()), "ext": dict(ext.most_common(15))})
            documents += docs
    for info in repo_infos:
        documents += [Path(info["path"]) / f for f in info.pop("documents")]
    documents.sort()
    doc_rows = []
    for d in documents[:DOC_LIMIT]:
        st = d.stat()
        doc_rows.append({"path": str(d), "size": st.st_size, "mtime": int(st.st_mtime)})
    return {"root": str(root), "loose_files": dict(loose.most_common()), "repos": repo_infos,
            "plain_dirs": plain_dirs, "documents_total": len(documents), "documents": doc_rows}


def main(argv: list[str]) -> int:
    roots, since, out = [], {}, None
    it = iter(argv)
    for a in it:
        if a == "--since":
            since = json.loads(Path(next(it)).read_text(encoding="utf-8-sig")).get("repos", {})
        elif a == "--out":
            out = Path(next(it))
        else:
            roots.append(Path(a).resolve())
    if not roots:
        print(__doc__, file=sys.stderr)
        return 2
    text = json.dumps({"roots": [root_info(r, since) for r in roots]}, ensure_ascii=False, indent=1)
    if out:
        out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.buffer.write(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
