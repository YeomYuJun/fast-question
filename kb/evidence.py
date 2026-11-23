"""근거 캡처와 판정. 판정 결과는 모든 검사가 명시적으로 통과할 때만 same 이고, git 실패·경로 없음·
읽기 실패는 unknown 이다.

github 근거: kind 가 github 이고 주소가 GitHub 파일 주소(https://github.com/<owner>/<repo>/blob/<ref>/<경로>)인 근거.
url 근거는 주소가 GitHub 이어도 그대로 url(판정 없음)이다(기본 가이드처럼 API 를 부르지 않아야 하는 곳이 있다).
서버(원격 모드)도 PC 파일 없이 판정할 수 있는 근거다. 기록할 때 GitHub API 로 그 ref 의 blob sha 를 받아 두고,
판정 때 비교 ref(ref 가 커밋이면 저장소 기본 브랜치, 아니면 그 ref)의 blob sha 와 견준다. 다르면 changed 이고,
raw 본문을 받아 앵커 유무를 설명에 붙인다. API 호출은 환경 변수 FQ_GITHUB_TOKEN 이 있으면 그 토큰으로 한다
(없으면 비인증 한도 시간당 60회).
"""
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Mapping

from kb.errors import ValidationError

KINDS = ("auto", "file", "dir", "git", "githead", "url", "note", "github")
GITHUB_BLOB = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/blob/([^/]+)/([^#?]+)")
GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
GITHUB_TIMEOUT_S = 8.0
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
DIR_SKIP = {".git", "node_modules"}
DIR_CAP = 50000
BINARY_DOCS = {".xlsx", ".xls", ".xlsm", ".docx", ".doc", ".pptx", ".ppt", ".hwp", ".hwpx", ".pdf"}
IS_WIN = os.name == "nt"
# pythonw 로 뜬 web 이 git 을 부를 때 콘솔 창이 깜빡이지 않게 한다
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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
                           capture_output=True, timeout=8, creationflags=NO_WINDOW)
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
                raise ValidationError(f"The folder has more than {DIR_CAP} files. Use a narrower path as evidence.",
                                      code="evidence_dir_too_large")
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
                           capture_output=True, timeout=8, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False
    return r.returncode == 0


def _exists(path: str | None) -> Path:
    if not path:
        raise ValidationError("Evidence path is missing: (empty)", code="evidence_path_missing")
    p = Path(path)
    if not p.exists():
        raise ValidationError(f"Evidence path does not exist: {path}", code="evidence_path_missing")
    return p.resolve()


def _check_anchor(anchor: str | None, p: Path) -> None:
    """anchor 가 파일 내용(UTF-8 바이트)에 없으면 등록을 거부한다. 판정(Checker)은 앵커를 상태에 쓰지 않는다."""
    if not anchor:
        return
    try:
        data = p.read_bytes()
    except OSError:
        raise ValidationError(f"Could not read the file to look for the anchor: {p}", code="evidence_unreadable") from None
    # 압축·이진 문서는 시트 이름·절 제목이 바이트로 드러나지 않는다(rules.md 가 이들에 시트·절 앵커를 쓰게 한다)
    if p.suffix.lower() in BINARY_DOCS or b"\0" in data[:8192]:
        return
    if anchor.encode("utf-8") not in data:
        raise ValidationError(f"Anchor '{anchor}' is not in {p.name}. Use a string that appears in the file exactly.",
                              code="evidence_anchor_missing")


def github_get(url: str, raw: bool = False) -> dict | bytes | None:
    """GitHub API(JSON) 또는 raw 본문(bytes). 404 는 None, 그 밖의 실패는 RuntimeError. 테스트가 바꿔 끼운다."""
    headers = {"User-Agent": "fast-question", "Accept": "application/vnd.github+json"}
    token = os.environ.get("FQ_GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=GITHUB_TIMEOUT_S) as r:  # noqa: S310 - github.com 고정 주소만
            data = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise RuntimeError(f"GitHub responded {e.code}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise RuntimeError(f"Could not reach GitHub: {e}") from None
    return data if raw else json.loads(data.decode("utf-8"))


def parse_github_url(url: str) -> dict | None:
    m = GITHUB_BLOB.match(url.strip())
    if not m:
        return None
    owner, repo, ref, file = m.groups()
    return {"owner": owner, "repo": repo.removesuffix(".git"), "ref": ref, "file": file.rstrip("/")}


def _github_blob(owner: str, repo: str, ref: str, file: str) -> str | None:
    got = github_get(f"{GITHUB_API}/repos/{owner}/{repo}/contents/{file}?ref={ref}")
    if not got or not isinstance(got, dict) or got.get("type") != "file":
        return None
    return got.get("sha")


def _capture_github(url: str, anchor: str | None) -> dict:
    g = parse_github_url(url)
    try:
        blob = _github_blob(g["owner"], g["repo"], g["ref"], g["file"])
    except RuntimeError as e:
        raise ValidationError(f"Could not read the file from GitHub, so the evidence cannot be recorded: {e}",
                              code="evidence_github_unreachable") from None
    if blob is None:
        raise ValidationError(f"The file does not exist on GitHub: {url}", code="evidence_github_missing")
    captured = {"owner": g["owner"], "repo": g["repo"], "ref": g["ref"], "file": g["file"], "blob_sha": blob}
    return _row("github", path=url, anchor=anchor, repo=f"{g['owner']}/{g['repo']}",
                commit_sha=g["ref"] if _HEX40.match(g["ref"]) else None, paths=[g["file"]], captured=captured)


def _row(kind, *, path=None, anchor=None, repo=None, commit_sha=None, paths=None, captured=None) -> dict:
    return {
        "kind": kind, "path": path, "anchor": anchor, "repo": repo, "commit_sha": commit_sha,
        "paths_json": json.dumps(paths, ensure_ascii=False) if paths is not None else None,
        "captured_json": json.dumps(captured, ensure_ascii=False) if captured is not None else None,
    }


def _capture_git(repo_arg: str, paths: list[str], commit: str | None, anchor: str | None) -> dict:
    top = repo_top(_exists(repo_arg))
    if top is None:
        raise ValidationError(f"Not a git repository, or git could not be run: {repo_arg}", code="evidence_not_git")
    out = run_git(top, "rev-parse", "--verify", f"{commit or 'HEAD'}^{{commit}}")
    if out is None:
        raise ValidationError(f"Commit not found: {commit or 'HEAD'}", code="evidence_commit_missing")
    commit_sha = out.strip()
    specs = [rel_spec(s, top) for s in (paths or ["."])]
    # 앵커는 경로 하나가 파일을 가리킬 때만 확인한다. 폴더·여러 경로·glob 은 어느 파일에서 찾을지 정할 수 없다
    if len(specs) == 1 and not any(ch in specs[0] for ch in "*?") and (top / specs[0]).is_file():
        _check_anchor(anchor, top / specs[0])
    match = spec_matcher(specs)
    diff = _nul_list(run_git(top, "diff", "--name-only", "--no-renames", "-z", commit_sha, "--"))
    untr = _nul_list(run_git(top, "ls-files", "-o", "--exclude-standard", "-z"))
    if diff is None or untr is None:
        raise ValidationError(f"git diff / ls-files failed: {top}", code="evidence_git_failed")
    dirty = {f: hash_file(top / f) for f in diff if match(f)}
    untracked = {f: hash_file(top / f) for f in untr if match(f)}
    return _row("git", path=str(top / specs[0]) if len(specs) == 1 and specs[0] != "." else None,
                anchor=anchor, repo=top.as_posix(), commit_sha=commit_sha, paths=specs,
                captured={"dirty": dirty, "untracked": untracked})


def capture(spec: Mapping) -> dict:
    """근거 spec 을 evidence 테이블 열 값으로 만든다. 경로·저장소가 없으면 ValidationError."""
    kind = spec.get("kind") or "auto"
    if kind not in KINDS:
        raise ValidationError(f"Evidence kind must be one of {', '.join(KINDS)}. Got: {kind}", code="evidence_kind")
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
            raise ValidationError(f"Not a file: {p}", code="evidence_not_file")
        _check_anchor(anchor, p)
        return _row("file", path=str(p), anchor=anchor, captured={"sha256": hash_file(p)})
    if kind == "dir":
        p = _exists(spec.get("path"))
        if not p.is_dir():
            raise ValidationError(f"Not a folder: {p}", code="evidence_not_dir")
        h, n = list_dir(p)
        return _row("dir", path=str(p), anchor=anchor, captured={"list_hash": h, "count": n})
    if kind == "git":
        return _capture_git(spec.get("repo") or spec.get("path"), list(spec.get("paths") or []),
                            spec.get("commit"), anchor)
    if kind == "githead":
        top = repo_top(_exists(spec.get("repo") or spec.get("path")))
        if top is None:
            raise ValidationError(f"Not a git repository: {spec.get('repo')}", code="evidence_not_git")
        head = (run_git(top, "rev-parse", "HEAD") or "").strip()
        branch = (run_git(top, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
        if not head:
            raise ValidationError(f"Could not read HEAD: {top}", code="evidence_git_failed")
        return _row("githead", repo=top.as_posix(), commit_sha=head, captured={"head": head, "branch": branch})
    if kind in ("url", "github"):
        url = (spec.get("url") or spec.get("path") or "").strip()
        if not url:
            raise ValidationError("url evidence has no address.", code="evidence_url_missing")
        if kind == "github":
            if not parse_github_url(url):
                raise ValidationError("github evidence must be a https://github.com/<owner>/<repo>/blob/<ref>/<path> address.",
                                      code="evidence_github_url")
            return _capture_github(url, anchor)
        return _row("url", path=url, anchor=anchor)
    text = (spec.get("text") or "").strip()
    if not text:
        raise ValidationError("note evidence has no text. Write who confirmed it and how.", code="evidence_note_empty")
    return _row("note", anchor=anchor, captured={"text": text})


ROW_KEYS = ("kind", "path", "anchor", "repo", "commit_sha", "paths_json", "captured_json")
CAPTURED_MAX_BYTES = 64 * 1024


def accept_captured(row) -> dict:
    """다른 PC 에서 capture() 로 만든 행을 받는다(원격 모드). 경로는 그 PC 의 것이라 확인하지 않고 형식만 본다."""
    if not isinstance(row, Mapping):
        raise ValidationError("captured must be an object returned by capture().", code="captured_format")
    out = {k: row.get(k) for k in ROW_KEYS}
    if out["kind"] not in KINDS or out["kind"] == "auto":
        raise ValidationError(f"captured has an invalid kind: {out['kind']}", code="captured_format")
    for k in ROW_KEYS[1:]:
        if out[k] is not None and not isinstance(out[k], str):
            raise ValidationError(f"captured {k} must be a string.", code="captured_format")
    for k in ("paths_json", "captured_json"):
        if out[k] is not None:
            try:
                json.loads(out[k])
            except ValueError:
                raise ValidationError(f"captured {k} is not JSON.", code="captured_format") from None
    if len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > CAPTURED_MAX_BYTES:
        raise ValidationError(f"captured can be at most {CAPTURED_MAX_BYTES // 1024} KB.", code="captured_too_large")
    return out


LINE_SEP = " ; "
NOTE_PREFIX = "메모:"
_NOTE_HEAD = re.compile(r"(?:메모|note):", re.IGNORECASE)


def parse_line(line: str) -> list[dict]:
    """화면 편집 폼의 근거 한 줄을 근거 spec 목록으로 바꾼다. 여러 근거는 " ; " 로 나눈다.
    http(s):// 로 시작하면 url, "메모:" 나 "note:" 로 시작하면 note, 나머지는 경로(auto)다.
    경로 뒤 첫 " #" 다음은 anchor 다(경로에는 공백이, anchor 에는 # 이 들어갈 수 있다)."""
    specs = []
    for part in (p.strip() for p in (line or "").split(LINE_SEP)):
        if not part:
            continue
        if re.match(r"https?://", part, re.IGNORECASE):
            specs.append({"kind": "url", "url": part})
        elif m := _NOTE_HEAD.match(part):
            specs.append({"kind": "note", "text": part[m.end():].strip()})
        else:
            path, _, anchor = part.partition(" #")
            specs.append({"kind": "auto", "path": path.strip(), "anchor": anchor.strip() or None})
    return specs


def line_of(evidence: list[Mapping]) -> str:
    """Store 가 돌려준 근거 목록을 parse_line 이 다시 읽을 수 있는 한 줄로 만든다."""
    parts = []
    for e in evidence:
        if e["kind"] == "url":
            parts.append(e["path"])
            continue
        if e["kind"] == "github":
            parts.append(f"{e['path']} #{e['anchor']}" if e.get("anchor") else e["path"])
            continue
        if e["kind"] == "note":
            parts.append(f"{NOTE_PREFIX} {e['text']}")
            continue
        path = e["path"] or e["repo"]  # 경로가 여럿인 git 근거와 githead 는 저장소 경로로 보인다
        parts.append(f"{path} #{e['anchor']}" if e.get("anchor") else path)
    return LINE_SEP.join(parts)


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
    if kind == "github":
        return {"kind": "github", "url": row["path"], "anchor": row["anchor"]}
    return {"kind": kind, "path": row["path"], "anchor": row["anchor"]}


def _anchor_note(anchor: str | None, files: list[Path]) -> str:
    """바뀐 근거의 판정 설명 끝에 붙이는 앵커 유무. 근거 상태(changed)는 바꾸지 않고 정리 목록이 보여 주는 설명만 늘린다.
    앵커가 없거나 읽을 파일이 없으면 빈 문자열."""
    if not anchor or not files:
        return ""
    needle = anchor.encode("utf-8")
    for f in files:
        try:
            if needle in f.read_bytes():
                return f" (anchor '{anchor}' present)"
        except OSError:
            return ""
    return f" (anchor '{anchor}' missing)"


class Checker:
    """판정기. 한 번의 재검증 동안 git diff·ls-files 결과를 (저장소, 커밋) 단위로 재사용한다."""

    def __init__(self):
        self._diff: dict[tuple[str, str], list[str] | None] = {}
        self._untr: dict[str, list[str] | None] = {}
        self._default_branch: dict[str, str | None] = {}
        self._blob: dict[tuple[str, str, str], str | None] = {}

    def _branch_of(self, owner: str, repo: str) -> str | None:
        k = f"{owner}/{repo}"
        if k not in self._default_branch:
            got = github_get(f"{GITHUB_API}/repos/{owner}/{repo}")
            self._default_branch[k] = (got or {}).get("default_branch") if isinstance(got, dict) else None
        return self._default_branch[k]

    def _blob_of(self, owner: str, repo: str, ref: str, file: str) -> str | None:
        k = (f"{owner}/{repo}", ref, file)
        if k not in self._blob:
            self._blob[k] = _github_blob(owner, repo, ref, file)
        return self._blob[k]

    def _check_github(self, row: Mapping, cap: dict) -> tuple[str, str]:
        owner, repo, ref, file = cap.get("owner"), cap.get("repo"), cap.get("ref"), cap.get("file")
        if not all((owner, repo, ref, file)) or not cap.get("blob_sha"):
            return "unknown", "The recorded GitHub evidence values are empty"
        try:
            compare = self._branch_of(owner, repo) if _HEX40.match(ref) else ref
            if not compare:
                return "unknown", "GitHub repository not found"
            blob = self._blob_of(owner, repo, compare, file)
            if blob is None:
                return "changed", f"{file} does not exist on {compare}"
            if blob == cap["blob_sha"]:
                return "same", ""
            note = ""
            if row.get("anchor"):
                body = github_get(f"{GITHUB_RAW}/{owner}/{repo}/{compare}/{file}", raw=True)
                if isinstance(body, bytes):
                    note = f" (anchor '{row['anchor']}' {'present' if row['anchor'].encode('utf-8') in body else 'missing'})"
            return "changed", f"{file} content changed on {compare}" + note
        except RuntimeError as e:
            return "unknown", f"Could not read GitHub: {e}"

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
            return "unknown", f"Error while checking: {e}"

    def _check(self, row: Mapping) -> tuple[str, str]:
        kind = row["kind"]
        cap = json.loads(row["captured_json"]) if row.get("captured_json") else {}
        if kind in ("url", "note"):
            return "external", ""
        if kind == "github":
            return self._check_github(row, cap)
        if kind == "file":
            p = Path(row["path"])
            if not p.exists():
                return "changed", f"File {p.name} no longer exists"
            h = hash_file(p)
            if h is None:
                return "unknown", f"Could not read file {p.name}"
            if h == cap.get("sha256"):
                return "same", ""
            return "changed", f"{p.name} content changed" + _anchor_note(row.get("anchor"), [p])
        if kind == "dir":
            p = Path(row["path"])
            if not p.exists():
                return "changed", "The folder no longer exists"
            h, n = list_dir(p)
            if h == cap.get("list_hash"):
                return "same", ""
            return "changed", f"The file list, sizes or modification times in the folder changed ({cap.get('count')} -> {n} files)"
        if kind == "git":
            repo = row["repo"]
            if not Path(repo).exists():
                return "unknown", "The repository path does not exist"
            diff, untr = self._diff_of(repo, row["commit_sha"]), self._untr_of(repo)
            if diff is None or untr is None:
                return "unknown", "Could not compare with git (commit missing or git failed)"
            specs = json.loads(row["paths_json"] or '["."]')
            match = spec_matcher(specs)
            dirty, untracked = cap.get("dirty", {}), cap.get("untracked", {})
            changed = []
            now_dirty = [f for f in diff if match(f)]
            # 기록 때 없던 키는 기록 뒤 바뀐 것이다(삭제·이름 변경 포함. 둘 다 None 이어도 같다고 보지 않는다)
            changed += [f for f in now_dirty if f not in dirty or dirty[f] != hash_file(Path(repo) / f)]
            changed += [f for f in dirty if f not in now_dirty]
            now_untr = [f for f in untr if match(f)]
            changed += [f for f in now_untr if f not in untracked or untracked[f] != hash_file(Path(repo) / f)]
            changed += [f for f in untracked if f not in now_untr]
            if not changed:
                return "same", ""
            uniq = list(dict.fromkeys(changed))
            more = f" and {len(uniq) - 3} more" if len(uniq) > 3 else ""
            files = [Path(repo) / s for s in specs if (Path(repo) / s).is_file()]
            return "changed", ", ".join(uniq[:3]) + more + " changed after recording" + _anchor_note(row.get("anchor"), files)
        if kind == "githead":
            repo = row["repo"]
            if not Path(repo).exists():
                return "unknown", "The repository path does not exist"
            head = run_git(repo, "rev-parse", "HEAD")
            branch = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
            if head is None or branch is None:
                return "unknown", "Could not read HEAD with git"
            if head.strip() == cap.get("head") and branch.strip() == cap.get("branch"):
                return "same", ""
            return "changed", (f"HEAD changed from {cap.get('branch')}@{str(cap.get('head'))[:8]} "
                               f"to {branch.strip()}@{head.strip()[:8]}")
        return "unknown", f"Unknown evidence kind: {kind}"


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
