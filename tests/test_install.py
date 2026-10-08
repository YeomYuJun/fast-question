import json

from scripts import install

OTHER = {"type": "command", "command": 'node "d:\\tools\\hooks\\other/inject.js"'}


def _settings(tmp_path, data, bom=True):
    p = tmp_path / "settings.json"
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + json.dumps(data, ensure_ascii=False).encode("utf-8"))
    return p


def _ours(data):
    return [h for g in data["hooks"]["UserPromptSubmit"] for h in g["hooks"] if "ask_kb.py" in h["command"]]


def test_install_hook_keeps_other_hooks_and_writes_without_bom(tmp_path):
    p = _settings(tmp_path, {"model": "x", "hooks": {"UserPromptSubmit": [{"hooks": [OTHER]}],
                                                     "Stop": [{"hooks": [OTHER]}]}})
    assert "등록" in install.install_hook(p)
    raw = p.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    data = json.loads(raw.decode("utf-8"))
    assert data["model"] == "x" and data["hooks"]["Stop"] == [{"hooks": [OTHER]}]
    assert data["hooks"]["UserPromptSubmit"][0] == {"hooks": [OTHER]}
    (hook,) = _ours(data)
    assert hook["type"] == "command" and hook["timeout"] == 10
    assert "python.exe" in hook["command"] and "hooks/ask_kb.py" in hook["command"]
    assert (tmp_path / "settings.json.fast-question.bak").exists()


def test_install_hook_is_idempotent(tmp_path):
    p = _settings(tmp_path, {}, bom=False)
    install.install_hook(p)
    assert "이미" in install.install_hook(p)
    assert len(_ours(json.loads(p.read_text(encoding="utf-8")))) == 1


def test_remove_hook_removes_only_ours(tmp_path):
    p = _settings(tmp_path, {"hooks": {"UserPromptSubmit": [{"hooks": [OTHER]}]}})
    install.install_hook(p)
    assert "제거" in install.remove_hook(p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["hooks"]["UserPromptSubmit"] == [{"hooks": [OTHER]}]


def test_install_skills_copies_every_skill_folder(tmp_path):
    src, dst = tmp_path / "skill", tmp_path / "dst"
    for name in ("qa", "qa-harvest"):
        (src / name).mkdir(parents=True)
        (src / name / "SKILL.md").write_text(name, encoding="utf-8")
    msgs = install.install_skills(src, dst)
    assert len(msgs) == 2 and (dst / "qa-harvest" / "SKILL.md").read_text(encoding="utf-8") == "qa-harvest"
    assert all("그대로" in m for m in install.install_skills(src, dst))


def test_skill_files_have_no_control_characters():
    """역슬래시 경로를 파이썬 문자열로 옮기다 폼 피드·벨 같은 제어 문자가 들어간 적이 있다."""
    import re
    for f in install.SKILLS_SRC.rglob("*.md"):
        assert not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", f.read_text(encoding="utf-8")), f
