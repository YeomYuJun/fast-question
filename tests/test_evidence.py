import subprocess

import pytest

from kb.errors import ValidationError
from kb.evidence import Checker, capture, overall_state
from kb.store import Store


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "-c", "core.autocrlf=false", *args], check=True, capture_output=True)


@pytest.fixture
def base(tmp_path):
    d = tmp_path / "한글 폴더" / "sub dir"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def repo(base):
    r = base / "저장소"
    (r / "src").mkdir(parents=True)
    git(r, "init", "-q")
    (r / "src" / "NasFileStorage.java").write_text("class Nas { int x = 1; }\n", encoding="utf-8")
    (r / "src" / "Other.java").write_text("class Other {}\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


def check(spec_or_row):
    row = dict(spec_or_row)
    return Checker().check(row)


def test_file_same_changed_missing(base):
    f = base / "정책.xlsx"
    f.write_bytes(b"v1")
    row = capture({"kind": "file", "path": str(f)})
    assert row["kind"] == "file"
    assert check(row) == ("same", "")

    f.write_bytes(b"v2")
    verdict, detail = check(row)
    assert verdict == "changed" and "정책.xlsx" in detail

    f.unlink()
    assert check(row)[0] == "changed"


def test_dir_changes_when_file_added(base):
    row = capture({"kind": "dir", "path": str(base)})
    assert check(row)[0] == "same"
    (base / "새 파일.txt").write_text("x", encoding="utf-8")
    assert check(row)[0] == "changed"


def test_git_path_tracks_only_its_paths(repo):
    target = repo / "src" / "NasFileStorage.java"
    row = capture({"kind": "auto", "path": str(target), "anchor": "class Nas"})
    assert row["kind"] == "git"
    assert row["anchor"] == "class Nas"
    assert check(row)[0] == "same"

    (repo / "src" / "Other.java").write_text("class Other { int y; }\n", encoding="utf-8")
    assert check(row)[0] == "same"

    target.write_text("class Nas { int x = 2; }\n", encoding="utf-8")
    verdict, detail = check(row)
    assert verdict == "changed" and "NasFileStorage.java" in detail


def test_git_untracked_file_in_scope(repo):
    row = capture({"kind": "git", "repo": str(repo), "paths": ["src/**"]})
    assert check(row)[0] == "same"
    (repo / "src" / "New.java").write_text("x", encoding="utf-8")
    assert check(row)[0] == "changed"


def test_githead_changes_after_commit(repo):
    row = capture({"kind": "githead", "repo": str(repo)})
    assert check(row)[0] == "same"
    (repo / "README.md").write_text("x", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "next")
    assert check(row)[0] == "changed"


def test_auto_on_git_ignored_file_is_file(repo):
    (repo / ".gitignore").write_text("docs/\n", encoding="utf-8")
    doc = repo / "docs" / "설계.md"
    doc.parent.mkdir()
    doc.write_text("v1", encoding="utf-8")

    row = capture({"kind": "auto", "path": str(doc)})
    assert row["kind"] == "file"
    doc.write_text("v2", encoding="utf-8")
    assert check(row)[0] == "changed"


def test_auto_outside_git_is_file(base):
    f = base / "메모.txt"
    f.write_text("x", encoding="utf-8")
    assert capture({"kind": "auto", "path": str(f)})["kind"] == "file"
    assert capture({"kind": "auto", "path": str(base)})["kind"] == "dir"


def test_url_and_note_are_external():
    assert check(capture({"kind": "url", "url": "https://example.com/doc"}))[0] == "external"
    assert check(capture({"kind": "note", "text": "운영사 담당자 확인"}))[0] == "external"


def test_capture_rejects_missing_path(base):
    with pytest.raises(ValidationError, match="근거 경로가 없다"):
        capture({"kind": "auto", "path": str(base / "없음.txt")})
    with pytest.raises(ValidationError, match="근거 형식"):
        capture({"kind": "zip", "path": str(base)})


def test_missing_repo_is_unknown(repo):
    row = capture({"kind": "githead", "repo": str(repo)})
    row["repo"] = str(repo.parent / "사라진 저장소")
    assert check(row)[0] == "unknown"


@pytest.mark.parametrize(
    "verdicts, state",
    [
        ([], "none"),
        (["external"], "external"),
        (["same", "external"], "trusted"),
        (["same", "changed", "unknown"], "stale"),
        (["same", "unknown"], "unknown"),
    ],
)
def test_overall_state(verdicts, state):
    assert overall_state(verdicts) == state


def test_store_evidence_lifecycle(conn, base):
    f = base / "근거.txt"
    f.write_text("v1", encoding="utf-8")
    user = Store(conn, "user")
    system = Store(conn, "system")
    user.add_category("이관")
    item = user.add_qa("이관", "근거가 있는 질문", "근거가 있는 답변",
                       evidence=[{"kind": "auto", "path": str(f)}, {"kind": "note", "text": "회의 확인"}])["item"]
    assert item["evidence_state"] == "trusted"
    assert [e["kind"] for e in item["evidence"]] == ["file", "note"]

    f.write_text("v2", encoding="utf-8")
    results = system.recheck()
    assert any(r["changed"] and r["verdict"] == "changed" for r in results)
    after = user.get_qa(item["id"])
    assert after["evidence_state"] == "stale"
    assert after["revisions"][0]["actor"] == "system"

    ev_id = after["evidence"][0]["id"]
    user.recapture_evidence(ev_id)
    assert user.get_qa(item["id"])["evidence_state"] == "trusted"

    user.archive_evidence(ev_id)
    assert user.get_qa(item["id"])["evidence_state"] == "external"


def test_store_rejects_bad_evidence_before_writing(conn, base):
    user = Store(conn, "user")
    user.add_category("이관")
    with pytest.raises(ValidationError):
        user.add_qa("이관", "질문", "답변", evidence=[{"kind": "file", "path": str(base / "없음")}])
    assert conn.execute("SELECT count(*) FROM qa").fetchone()[0] == 0
