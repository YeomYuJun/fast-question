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


def test_capture_rejects_anchor_missing_from_file(base, repo):
    f = base / "설정.txt"
    f.write_text("시간 제한 timeout = 30\n", encoding="utf-8")
    assert capture({"kind": "file", "path": str(f), "anchor": "시간 제한"})["anchor"] == "시간 제한"
    with pytest.raises(ValidationError, match="앵커 'retry' 가 설정.txt 에 없다"):
        capture({"kind": "file", "path": str(f), "anchor": "retry"})
    target = repo / "src" / "NasFileStorage.java"
    with pytest.raises(ValidationError, match="NasFileStorage.java 에 없다"):
        capture({"kind": "auto", "path": str(target), "anchor": "class Storage"})
    with pytest.raises(ValidationError, match="NasFileStorage.java 에 없다"):
        capture({"kind": "git", "repo": str(repo), "paths": ["src/NasFileStorage.java"], "anchor": "class Storage"})


def test_capture_skips_anchor_check_for_dir_and_many_paths(base, repo):
    assert capture({"kind": "dir", "path": str(base), "anchor": "없는 문자열"})["kind"] == "dir"
    xlsx = base / "목록.xlsx"
    xlsx.write_bytes(b"PK\x03\x04\x14\x00compressed")
    assert capture({"kind": "file", "path": str(xlsx), "anchor": "Sheet1"})["anchor"] == "Sheet1"
    assert capture({"kind": "auto", "path": str(repo / "src"), "anchor": "없는 문자열"})["kind"] == "git"
    assert capture({"kind": "git", "repo": str(repo), "paths": ["src/*.java"], "anchor": "없는 문자열"})["kind"] == "git"
    assert capture({"kind": "git", "repo": str(repo), "paths": ["src/NasFileStorage.java", "src/Other.java"],
                    "anchor": "없는 문자열"})["kind"] == "git"


def test_changed_detail_says_whether_anchor_remains(base, repo):
    f = base / "설정.txt"
    f.write_text("timeout = 30\n", encoding="utf-8")
    row = capture({"kind": "file", "path": str(f), "anchor": "timeout"})
    f.write_text("timeout = 60\n", encoding="utf-8")
    assert check(row) == ("changed", "설정.txt 내용이 바뀌었다 (앵커 'timeout' 있음)")
    f.write_text("limit = 60\n", encoding="utf-8")
    assert check(row) == ("changed", "설정.txt 내용이 바뀌었다 (앵커 'timeout' 없음)")

    target = repo / "src" / "NasFileStorage.java"
    row = capture({"kind": "auto", "path": str(target), "anchor": "class Nas"})
    plain = capture({"kind": "auto", "path": str(target)})
    target.write_text("class Storage {}\n", encoding="utf-8")
    verdict, detail = check(row)
    assert verdict == "changed" and detail.endswith("(앵커 'class Nas' 없음)")
    assert "앵커" not in check(plain)[1]
    target.unlink()
    assert "앵커" not in check(row)[1]  # 파일이 없으면 앵커를 따로 말하지 않는다


def test_git_deleted_file_is_changed(repo):
    target = repo / "src" / "NasFileStorage.java"
    row = capture({"kind": "auto", "path": str(target)})
    target.unlink()
    assert check(row)[0] == "changed"
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "삭제")
    assert check(row)[0] == "changed"


def test_git_renamed_file_is_changed(repo):
    row = capture({"kind": "auto", "path": str(repo / "src" / "NasFileStorage.java")})
    git(repo, "mv", "src/NasFileStorage.java", "src/NasStorage.java")
    git(repo, "commit", "-q", "-m", "이름 변경")
    assert check(row)[0] == "changed"


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


def test_recheck_state_changes_are_recorded_as_system(conn, base):
    f = base / "근거.txt"
    f.write_text("v1", encoding="utf-8")
    user = Store(conn, "user")
    user.add_category("이관")
    item = user.add_qa("이관", "근거 질문", "근거 답변", evidence=[{"kind": "auto", "path": str(f)}])["item"]
    f.write_text("v2", encoding="utf-8")

    user.recheck()  # 사용자가 화면에서 재검증을 눌러도 판정 변화는 system 이 기록한다
    revs = conn.execute("SELECT table_name, actor FROM revision WHERE id > (SELECT max(id) - 2 FROM revision)").fetchall()
    assert {(r[0], r[1]) for r in revs} == {("evidence", "system"), ("qa", "system")}
    assert user.get_qa(item["id"])["evidence_state"] == "stale"


def test_store_rejects_bad_evidence_before_writing(conn, base):
    user = Store(conn, "user")
    user.add_category("이관")
    with pytest.raises(ValidationError):
        user.add_qa("이관", "질문", "답변", evidence=[{"kind": "file", "path": str(base / "없음")}])
    assert conn.execute("SELECT count(*) FROM qa").fetchone()[0] == 0


# ---------- 화면 편집 폼의 근거 한 줄 ----------

@pytest.mark.parametrize("line, specs", [
    ("", []),
    ("   ", []),
    ("D:/0. work/한글 폴더/a.py", [{"kind": "auto", "path": "D:/0. work/한글 폴더/a.py", "anchor": None}]),
    ("D:/0. work/a.py #def foo", [{"kind": "auto", "path": "D:/0. work/a.py", "anchor": "def foo"}]),
    ("D:/a.py #C# 코드 #2", [{"kind": "auto", "path": "D:/a.py", "anchor": "C# 코드 #2"}]),
    ("https://kubernetes.io/docs #x", [{"kind": "url", "url": "https://kubernetes.io/docs #x"}]),
    ("메모: 사용자가 콘솔에서 확인", [{"kind": "note", "text": "사용자가 콘솔에서 확인"}]),
    ("D:/a.py #f ; https://e.example ; 메모:구두 확인",
     [{"kind": "auto", "path": "D:/a.py", "anchor": "f"}, {"kind": "url", "url": "https://e.example"},
      {"kind": "note", "text": "구두 확인"}]),
])
def test_parse_line(line, specs):
    from kb.evidence import parse_line
    assert parse_line(line) == specs


def test_line_of_round_trips(conn, base):
    from kb.evidence import line_of, parse_line
    f = base / "근거 파일.txt"
    f.write_text("v1 anchor 1", encoding="utf-8")
    f = f.resolve()  # 저장되는 경로는 resolve 한 값이다(윈도우 임시 폴더는 8.3 짧은 이름일 수 있다)
    s = Store(conn, "user")
    s.add_category("이관")
    line = f"{f} #anchor 1 ; https://e.example ; 메모: 사용자 확인"
    item = s.add_qa("이관", "질문", "답변", evidence=parse_line(line))["item"]
    shown = line_of(item["evidence"])
    assert parse_line(shown) == parse_line(line)
    assert line_of([]) == ""


def test_update_replaces_evidence_without_false_conflict(conn, base):
    f1, f2 = base / "첫째.txt", base / "둘째.txt"
    f1.write_text("1", encoding="utf-8")
    f2.write_text("2", encoding="utf-8")
    s = Store(conn, "user")
    s.add_category("이관")
    item = s.add_qa("이관", "질문", "답변")["item"]
    assert item["evidence_state"] == "none"

    # 근거만 바꿔도 된다. 상태 재계산이 버전을 올려도 같은 요청 안이라 충돌로 보지 않는다
    after = s.update_qa(item["id"], item["version"], evidence=[{"kind": "auto", "path": str(f1)}])
    assert [e["path"] for e in after["evidence"]] == [str(f1.resolve())]
    assert after["evidence_state"] == "trusted"

    after2 = s.update_qa(after["id"], after["version"], answer="고친 답변",
                         evidence=[{"kind": "auto", "path": str(f2)}])
    assert [e["path"] for e in after2["evidence"]] == [str(f2.resolve())]
    assert after2["answer"] == "고친 답변"

    cleared = s.update_qa(after2["id"], after2["version"], evidence=[])
    assert cleared["evidence"] == [] and cleared["evidence_state"] == "none"
    assert conn.execute("SELECT count(*) FROM evidence WHERE deleted_at IS NOT NULL").fetchone()[0] == 2

    from kb.errors import ConflictError
    with pytest.raises(ConflictError):
        s.update_qa(item["id"], item["version"], evidence=[])


def test_update_with_bad_evidence_changes_nothing(conn, base):
    s = Store(conn, "user")
    s.add_category("이관")
    item = s.add_qa("이관", "질문", "답변")["item"]
    with pytest.raises(ValidationError, match="근거 경로가 없다"):
        s.update_qa(item["id"], item["version"], answer="바꾼 답변",
                    evidence=[{"kind": "auto", "path": str(base / "없는 파일")}])
    assert s.get_qa(item["id"])["answer"] == "답변"


def test_recheck_does_not_overwrite_concurrent_recapture(conn, base, monkeypatch):
    """재검증이 판정하는 동안 다른 쪽이 근거를 재기록하면, 재검증은 옛 행 기준 판정으로 덮어쓰지 않는다."""
    import kb.store as store_mod

    f = base / "근거.txt"
    f.write_text("v1", encoding="utf-8")
    user = Store(conn, "user")
    user.add_category("이관")
    item = user.add_qa("이관", "질문", "답변", evidence=[{"kind": "auto", "path": str(f)}])["item"]
    ev_id = item["evidence"][0]["id"]
    f.write_text("v2", encoding="utf-8")

    class RacingChecker(store_mod.Checker):
        def check(self, row):
            result = super().check(row)  # 옛 기록 기준으로 "바뀜"
            Store(conn, "claude").recapture_evidence(ev_id)  # 그 사이 Claude 가 다시 읽고 재기록
            return result
    monkeypatch.setattr(store_mod, "Checker", RacingChecker)

    results = Store(conn, "system").recheck()
    assert results[0]["changed"] is False
    assert user.get_qa(item["id"])["evidence"][0]["verdict"] == "same"
    assert user.get_qa(item["id"])["evidence_state"] == "trusted"
