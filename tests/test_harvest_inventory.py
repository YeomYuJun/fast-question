import json
import subprocess
import sys
from pathlib import Path

INVENTORY = Path(__file__).resolve().parent.parent / "fq_client" / "inventory.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
                          encoding="utf-8").stdout.strip()


def commit(repo: Path, rel: str, text: str, msg: str) -> str:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    git(repo, "add", rel)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def make_tree(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "proj root"
    repo = root / "api"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    first = commit(repo, "src/Nas.java", "class Nas {}", "NAS 저장소 추가")
    commit(repo, "src/Nas.java", "class Nas { int x; }", "fix: 마운트 빠진 Pod 에서 delete 가 false 를 돌려주던 문제")
    commit(repo, "docs/설계.md", "# 설계", "문서 추가")
    plain = root / "정책"
    plain.mkdir()
    (plain / "방화벽 정책.xlsx").write_bytes(b"PK")
    (plain / "메모.txt").write_text("메모", encoding="utf-8")
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "node_modules" / "x" / "README.md").write_text("무시", encoding="utf-8")
    return root, repo, first


def run(*args: str) -> dict:
    r = subprocess.run([sys.executable, str(INVENTORY), *args], capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return json.loads(r.stdout.decode("utf-8")) if r.stdout else {}


def test_inventory_lists_repos_plain_dirs_and_documents(tmp_path):
    root, repo, _ = make_tree(tmp_path)
    (only,) = run(str(root))["roots"]
    (r,) = only["repos"]
    assert Path(r["path"]) == repo and r["files"] == 2 and r["ext"] == {".java": 1, ".md": 1}
    assert r["hot_files"][0] == {"path": "src/Nas.java", "changes": 2}
    (fix,) = r["fix_commits"]
    assert fix["subject"].startswith("fix:") and fix["files"] == ["src/Nas.java"]
    assert [Path(d["path"]).name for d in only["plain_dirs"]] == ["정책"]
    docs = {Path(d["path"]).name for d in only["documents"]}
    assert docs == {"방화벽 정책.xlsx", "메모.txt", "설계.md"}


def test_inventory_since_lists_changes_after_recorded_head(tmp_path):
    root, repo, first = make_tree(tmp_path)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"repos": {repo.as_posix(): first}}), encoding="utf-8")
    out = tmp_path / "out.json"
    run(str(root), "--since", str(state), "--out", str(out))
    (r,) = json.loads(out.read_text(encoding="utf-8"))["roots"][0]["repos"]
    assert r["since"]["sha"] == first
    assert set(r["since"]["changed"]) == {"src/Nas.java", "docs/설계.md"}
    assert len(r["since"]["commits"]) == 2
