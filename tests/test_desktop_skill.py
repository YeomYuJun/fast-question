"""claude.ai·Claude Desktop 용 qa Skill 파일(scripts/build_desktop_skill.py): 원격 구간만 들어가고 업로드 모양을 지킨다."""
import zipfile

from scripts import build_desktop_skill as b


def test_desktop_skill_has_only_remote_text():
    files = b.skill_files()
    assert sorted(files) == ["qa/SKILL.md", "qa/rules.md"]
    text = "\n".join(data.decode("utf-8") for data in files.values())
    for local_only in ('"kind": "auto"', "claude mcp", "/mcp", "kb_recheck", "recapture", "user_word_add 로 넣는다"):
        assert local_only not in text, local_only
    assert "커넥터" in text and "## 먼저 찾기" in text
    assert b"\r\n" not in b"".join(files.values())


def test_desktop_skill_frontmatter_passes_upload_rules():
    meta = b.frontmatter(b.skill_files()["qa/SKILL.md"])
    assert meta["name"] == "qa"
    assert "<" not in meta["description"] and len(meta["description"]) <= b.DESCRIPTION_MAX


def test_desktop_skill_zip_is_reproducible(tmp_path):
    one, two = tmp_path / "1.skill", tmp_path / "2.skill"
    b.write(one, b.skill_files())
    b.write(two, b.skill_files())
    assert one.read_bytes() == two.read_bytes()
    with zipfile.ZipFile(one) as z:
        assert {n: z.read(n) for n in z.namelist()} == b.skill_files()
