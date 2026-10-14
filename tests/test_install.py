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
    original = (tmp_path / "settings.json.fast-question.bak").read_bytes()
    assert "이미" in install.install_hook(p)
    assert len(_ours(json.loads(p.read_text(encoding="utf-8")))) == 1
    install.remove_hook(p)
    assert (tmp_path / "settings.json.fast-question.bak").read_bytes() == original
    assert b"\r\n" not in p.read_bytes()


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


def test_install_skills_fills_repo_path_in_md_only(tmp_path):
    src, dst, root = tmp_path / "skill", tmp_path / "dst", tmp_path / "repo dir"
    (src / "qa").mkdir(parents=True)
    (src / "qa" / "SKILL.md").write_text(f'"{install.ROOT_MARK}/hooks/ask_kb.py"', encoding="utf-8")
    (src / "qa" / "tool.py").write_text(install.ROOT_MARK, encoding="utf-8")
    install.install_skills(src, dst, root)
    assert (dst / "qa" / "SKILL.md").read_text(encoding="utf-8") == f'"{root.as_posix()}/hooks/ask_kb.py"'
    assert (dst / "qa" / "tool.py").read_text(encoding="utf-8") == install.ROOT_MARK
    assert all("그대로" in m for m in install.install_skills(src, dst, root))


def test_skill_sources_use_repo_path_mark_not_absolute_path():
    for f in install.SKILLS_SRC.rglob("*.md"):
        assert "fast-question/" not in f.read_text(encoding="utf-8").replace(install.ROOT_MARK, ""), f


def test_skill_files_have_no_control_characters():
    """역슬래시 경로를 파이썬 문자열로 옮기다 폼 피드·벨 같은 제어 문자가 들어간 적이 있다."""
    import re
    for f in install.SKILLS_SRC.rglob("*.md"):
        assert not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", f.read_text(encoding="utf-8")), f


def test_install_skills_fills_screen_url(tmp_path):
    src, dst = tmp_path / "skill", tmp_path / "dst"
    (src / "qa").mkdir(parents=True)
    (src / "qa" / "SKILL.md").write_text(f"화면 {install.URL_MARK}/#qa/1", encoding="utf-8")
    install.install_skills(src, dst, url="https://fq.example.com/")
    assert (dst / "qa" / "SKILL.md").read_text(encoding="utf-8") == "화면 https://fq.example.com/#qa/1"
    assert "덮어씀" in install.install_skills(src, dst, url="http://127.0.0.1:8765")[0]


def test_skill_sources_use_url_mark_not_local_address():
    texts = [f.read_text(encoding="utf-8") for f in install.SKILLS_SRC.rglob("SKILL.md")]
    assert all("127.0.0.1:8765" not in t for t in texts)
    assert sum(install.URL_MARK in t for t in texts) == 2


class FakeRun:
    def __init__(self, registered: bytes | None):
        self.registered, self.calls = registered, []

    def __call__(self, args, **kw):
        import subprocess
        self.calls.append(args[1:3] + (["--remote"] if "--remote" in args else []))
        ok = args[2] != "get" or self.registered is not None
        return subprocess.CompletedProcess(args, 0 if ok else 1, self.registered or b"", b"")


def test_register_mcp_switches_between_local_and_remote():
    fresh = FakeRun(None)
    assert "완료" in install.register_mcp(remote=True, run=fresh, claude="claude")
    assert fresh.calls[-1] == ["mcp", "add", "--remote"]

    local = FakeRun(b"Command: python.exe run_mcp.py")
    install.register_mcp(remote=True, run=local, claude="claude")
    assert [c[:2] for c in local.calls] == [["mcp", "get"], ["mcp", "remove"], ["mcp", "add"]]

    remote = FakeRun(b"Command: python.exe run_mcp.py --remote")
    assert "이미" in install.register_mcp(remote=True, run=remote, claude="claude")
    install.register_mcp(remote=False, run=remote, claude="claude")
    assert remote.calls[-1] == ["mcp", "add"]


def _opener(status: int, body: dict):
    import io
    from urllib.error import HTTPError

    def opener(req, timeout):
        assert req.get_header("Authorization") == "Bearer fq_pat_abc"
        if status != 200:
            raise HTTPError(req.full_url, status, "x", {}, io.BytesIO(b"{}"))
        return io.BytesIO(json.dumps(body).encode())
    return opener


def test_install_remote_checks_token_before_writing(tmp_path, monkeypatch):
    import pytest

    from kb.remote import credentials_path, load_credentials
    with pytest.raises(ValueError):
        install.install_remote("https://fq.example.com", "fq_pat_abc", _opener(401, {}))
    assert not credentials_path().exists()
    msgs = install.install_remote("https://fq.example.com/", "fq_pat_abc",
                                  _opener(200, {"account": {"id": 1, "display_name": "octo", "providers": ["github"]}}))
    assert "octo" in msgs[0]
    cred = load_credentials()
    assert (cred["url"], cred["token"]) == ("https://fq.example.com", "fq_pat_abc") and cred["device_id"]
