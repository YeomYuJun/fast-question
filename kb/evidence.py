"""근거 캡처와 판정. claim-ledger(lib/verdict.js)의 근거 형식과
판정 규칙을 옮겼다. 판정 결과는 모든 검사가 명시적으로 통과할 때만 same 이고, git 실패·경로 없음·
읽기 실패는 unknown 이다.
"""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Mapping

from kb.errors import ValidationError

KINDS = ("auto", "file", "dir", "git", "githead", "url", "note")
DIR_SKIP = {".git", "node_modules"}
DIR_CAP = 50000
IS_WIN = os.name == "nt"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file(p: Path) -> str | None:
    try:
        return _sha(p.read_bytes())
    except OSError:
        return None


def run_git(repo: Path | str, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=false", *args],
                           capture_output=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", errors="replace")


def _nul_list(out: str | None) -> list[str] | None:
    return None if out is None else [s for s in out.split("\0") if s]


def repo_top(path: Path) -> Path | None:
    start = path if path.is_dir() else path.parent
    out = run_git(start, "rev-parse", "--show-toplevel")
    return Path(out.strip()).resolve() if out else None


def list_dir(root: Path) -> tuple[str, int]:
    rows = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in DIR_SKIP)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            try:
                st = p.stat()
                rows.append(f"{rel}\t{st.st_size}\t{int(st.st_mtime * 1000)}")
            except OSError:
                rows.append(f"{rel}\t?")
            if len(rows) > DIR_CAP:
                raise ValidationError(f"폴더 안 파일이 {DIR_CAP}개를 넘는다. 더 좁은 경로를 근거로 쓴다.")
    rows.sort()
    return _sha("\n".join(rows).encode("utf-8")), len(rows)


def rel_spec(spec: str, top: Path) -> str:
    s = str(spec).strip().replace("\\", "/")
    p = Path(s)
    if p.is_absolute():
        try:
            s = p.resolve().relative_to(top).as_posix()
        except ValueError:
            pass
    s = re.sub(r"^\./", "", s).rstrip("/")
    return s or "."


def spec_matcher(specs: list[str]) -> Callable[[str], bool]:
    flags = re.IGNORECASE if IS_WIN else 0
    tests = []
    for sp in specs:
        if sp in (".", "**"):
            tests.append(lambda rel: True)
        elif any(ch in sp for ch in "*?"):
            parts = re.split(r"(\*\*/|\*\*|\*|\?)", sp)
            conv = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}
            rx = re.compile("^" + "".join(conv.get(x, re.escape(x)) for x in parts) + "$", flags)
            tests.append(lambda rel, rx=rx: bool(rx.match(rel)))
        else:
            a = sp.lower() if IS_WIN else sp
            tests.append(lambda rel, a=a: (rel.lower() if IS_WIN else rel) == a
                         or (rel.lower() if IS_WIN else rel).startswith(a + "/"))
    return lambda rel: any(t(rel) for t in tests)


def _ignored(top: Path, p: Path) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(top), "check-ignore", "-q", "--", p.relative_to(top).as_posix()],
                           capture_output=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False
    return r.returncode == 0


def _exists(path: str | None) -> Path:
    if not path:
        raise ValidationError("근거 경로가 없다: (비어 있음)")
    p = Path(path)
    if not p.exists():
        raise ValidationError(f"근거 경로가 없다: {path}")
    return p.resolve()


def _row(kind, *, path=None, anchor=None, repo=None, commit_sha=None, paths=None, captured=None) -> dict:
    return {
        "kind": kind, "path": path, "anchor": anchor, "repo": repo, "commit_sha": commit_sha,
        "paths_json": json.dumps(paths, ensure_ascii=False) if paths is not None else None,
        "captured_json": json.dumps(captured, ensure_ascii=False) if captured is not None else None,
    }


def _capture_git(repo_arg: str, paths: list[str], commit: str | None, anchor: str | None) -> dict:
    top = repo_top(_exists(repo_arg))
    if top is None:
        raise ValidationError(f"git 저장소가 아니거나 git 을 실행하지 못했다: {repo_arg}")
    out = run_git(top, "rev-parse", "--verify", f"{commit or 'HEAD'}^{{commit}}")
    if out is None:
        raise ValidationError(f"커밋을 찾지 못했다: {commit or 'HEAD'}")
    commit_sha = out.strip()
    specs = [rel_spec(s, top) for s in (paths or ["."])]
    match = spec_matcher(specs)
    diff = _nul_list(run_git(top, "diff", "--name-only", "--no-renames", "-z", commit_sha, "--"))
    untr = _nul_list(run_git(top, "ls-files", "-o", "--exclude-standard", "-z"))
    if diff is None or untr is None:
        raise ValidationError(f"git diff / ls-files 실행에 실패했다: {top}")
    dirty = {f: hash_file(top / f) for f in diff if match(f)}
    untracked = {f: hash_file(top / f) for f in untr if match(f)}
    return _row("git", path=str(top / specs[0]) if len(specs) == 1 and specs[0] != "." else None,
                anchor=anchor, repo=top.as_posix(), commit_sha=commit_sha, paths=specs,
                captured={"dirty": dirty, "untracked": untracked})


def capture(spec: Mapping) -> dict:
    """근거 spec 을 evidence 테이블 열 값으로 만든다. 경로·저장소가 없으면 ValidationError."""
    kind = spec.get("kind") or "auto"
    if kind not in KINDS:
        raise ValidationError(f"근거 형식은 {', '.join(KINDS)} 중 하나다. 받은 값: {kind}")
    anchor = spec.get("anchor") or None
    if kind == "auto":
        p = _exists(spec.get("path"))
        top = repo_top(p)
        # git 이 무시하는 파일(.gitignore)은 diff·ls-files 에 나오지 않아 git 근거로는 변경을 못 잡는다
        if top is not None and not _ignored(top, p):
            return _capture_git(str(top), [p.relative_to(top).as_posix()], None, anchor)
        kind = "dir" if p.is_dir() else "file"
    if kind == "file":
        p = _exists(spec.get("path"))
        if not p.is_file():
            raise ValidationError(f"파일이 아니다: {p}")
        return _row("file", path=str(p), anchor=anchor, captured={"sha256": hash_file(p)})
    if kind == "dir":
        p = _exists(spec.get("path"))
        if not p.is_dir():
            raise ValidationError(f"폴더가 아니다: {p}")
        h, n = list_dir(p)
        return _row("dir", path=str(p), anchor=anchor, captured={"list_hash": h, "count": n})
    if kind == "git":
        return _capture_git(spec.get("repo") or spec.get("path"), list(spec.get("paths") or []),
                            spec.get("commit"), anchor)
    if kind == "githead":
        top = repo_top(_exists(spec.get("repo") or spec.get("path")))
        if top is None:
            raise ValidationError(f"git 저장소가 아니다: {spec.get('repo')}")
        head = (run_git(top, "rev-parse", "HEAD") or "").strip()
        branch = (run_git(top, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
        if not head:
            raise ValidationError(f"HEAD 를 읽지 못했다: {top}")
        return _row("githead", repo=top.as_posix(), commit_sha=head, captured={"head": head, "branch": branch})
    if kind == "url":
        url = (spec.get("url") or spec.get("path") or "").strip()
        if not url:
            raise ValidationError("url 근거에 주소가 없다.")
        return _row("url", path=url, anchor=anchor)
    text = (spec.get("text") or "").strip()
    if not text:
        raise ValidationError("note 근거에 내용이 없다. 누가 어떻게 확인했는지 적는다.")
    return _row("note", anchor=anchor, captured={"text": text})


def spec_of(row: Mapping) -> dict:
    """저장된 근거 행에서 다시 캡처할 spec 을 만든다(재기록용, git 은 현재 HEAD 기준)."""
    kind = row["kind"]
    if kind == "git":
        return {"kind": "git", "repo": row["repo"], "paths": json.loads(row["paths_json"] or "[]"),
                "anchor": row["anchor"]}
    if kind == "githead":
        return {"kind": "githead", "repo": row["repo"]}
    if kind == "note":
        return {"kind": "note", "text": json.loads(row["captured_json"] or "{}").get("text", "")}
    if kind == "url":
        return {"kind": "url", "url": row["path"]}
    return {"kind": kind, "path": row["path"], "anchor": row["anchor"]}


class Checker:
    """판정기. 한 번의 재검증 동안 git diff·ls-files 결과를 (저장소, 커밋) 단위로 재사용한다."""

    def __init__(self):
        self._diff: dict[tuple[str, str], list[str] | None] = {}
        self._untr: dict[str, list[str] | None] = {}

    def _diff_of(self, repo: str, commit: str):
        k = (repo, commit)
        if k not in self._diff:
            self._diff[k] = _nul_list(run_git(repo, "diff", "--name-only", "--no-renames", "-z", commit, "--"))
        return self._diff[k]

    def _untr_of(self, repo: str):
        if repo not in self._untr:
            self._untr[repo] = _nul_list(run_git(repo, "ls-files", "-o", "--exclude-standard", "-z"))
        return self._untr[repo]

    def check(self, row: Mapping) -> tuple[str, str]:
        try:
            return self._check(row)
        except Exception as e:  # noqa: BLE001 - 판정 실패는 신뢰하지 않음(unknown)으로 간다
            return "unknown", f"판정 중 오류: {e}"

    def _check(self, row: Mapping) -> tuple[str, str]:
        kind = row["kind"]
        cap = json.loads(row["captured_json"]) if row.get("captured_json") else {}
        if kind in ("url", "note"):
            return "external", ""
        if kind == "file":
            p = Path(row["path"])
            if not p.exists():
                return "changed", f"{p.name} 파일이 없어졌다"
            h = hash_file(p)
            if h is None:
                return "unknown", f"{p.name} 파일을 읽지 못했다"
            return ("same", "") if h == cap.get("sha256") else ("changed", f"{p.name} 내용이 바뀌었다")
        if kind == "dir":
            p = Path(row["path"])
            if not p.exists():
                return "changed", "폴더가 없어졌다"
            h, n = list_dir(p)
            if h == cap.get("list_hash"):
                return "same", ""
            return "changed", f"폴더 안 파일 목록·크기·수정시각이 바뀌었다 ({cap.get('count')} -> {n}개)"
        if kind == "git":
            repo = row["repo"]
            if not Path(repo).exists():
                return "unknown", "저장소 경로가 없다"
            diff, untr = self._diff_of(repo, row["commit_sha"]), self._untr_of(repo)
            if diff is None or untr is None:
                return "unknown", "git 으로 비교하지 못했다(커밋이 없거나 git 실행 실패)"
            match = spec_matcher(json.loads(row["paths_json"] or '["."]'))
            dirty, untracked = cap.get("dirty", {}), cap.get("untracked", {})
            changed = []
            now_dirty = [f for f in diff if match(f)]
            changed += [f for f in now_dirty if dirty.get(f) != hash_file(Path(repo) / f)]
            changed += [f for f in dirty if f not in now_dirty]
            now_untr = [f for f in untr if match(f)]
            changed += [f for f in now_untr if untracked.get(f) != hash_file(Path(repo) / f)]
            changed += [f for f in untracked if f not in now_untr]
            if not changed:
                return "same", ""
            uniq = list(dict.fromkeys(changed))
            more = f" 외 {len(uniq) - 3}개" if len(uniq) > 3 else ""
            return "changed", ", ".join(uniq[:3]) + more + " 가 기록 뒤에 바뀌었다"
        if kind == "githead":
            repo = row["repo"]
            if not Path(repo).exists():
                return "unknown", "저장소 경로가 없다"
            head = run_git(repo, "rev-parse", "HEAD")
            branch = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
            if head is None or branch is None:
                return "unknown", "git 으로 HEAD 를 읽지 못했다"
            if head.strip() == cap.get("head") and branch.strip() == cap.get("branch"):
                return "same", ""
            return "changed", (f"HEAD 가 {cap.get('branch')}@{str(cap.get('head'))[:8]} 에서 "
                               f"{branch.strip()}@{head.strip()[:8]} 로 바뀌었다")
        return "unknown", f"알 수 없는 근거 형식: {kind}"


def overall_state(verdicts: list[str]) -> str:
    if not verdicts:
        return "none"
    checkable = [v for v in verdicts if v != "external"]
    if not checkable:
        return "external"
    if "changed" in checkable:
        return "stale"
    if "unknown" in checkable:
        return "unknown"
    return "trusted"
