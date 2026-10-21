import json
import shlex

from scripts import autostart, install

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


def test_venv_python_follows_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    assert install.venv_python(tmp_path) == tmp_path / ".venv" / "Scripts" / "python.exe"
    monkeypatch.setattr(install, "IS_WINDOWS", False)
    assert install.venv_python(tmp_path) == tmp_path / ".venv" / "bin" / "python"


def test_posix_hook_and_cli_commands_split_back_to_paths(monkeypatch, tmp_path):
    """macOS·Linux 의 Claude Code 는 훅 command 와 Skill 의 명령을 sh 계열 셸로 실행한다. 공백·$ 가 든 경로도 그대로 나뉜다."""
    root = tmp_path / "repo dir $HOME"
    monkeypatch.setattr(install, "IS_WINDOWS", False)
    monkeypatch.setattr(install, "ROOT", root)
    monkeypatch.setattr(install, "HOOK", root / "hooks" / "ask_kb.py")
    py = str(root / ".venv" / "bin" / "python")
    assert shlex.split(install.hook_command()) == [py, str(root / "hooks" / "ask_kb.py")]
    assert shlex.split(install.local_marks(root)[install.CLI_MARK]) == [py, str(root / "fq_client" / "cli.py")]


def test_windows_hook_command_keeps_double_quoted_posix_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    monkeypatch.setattr(install, "ROOT", tmp_path)
    monkeypatch.setattr(install, "HOOK", tmp_path / "hooks" / "ask_kb.py")
    py = (tmp_path / ".venv" / "Scripts" / "python.exe").as_posix()
    assert install.hook_command() == f'"{py}" "{(tmp_path / "hooks" / "ask_kb.py").as_posix()}"'


def test_register_mcp_uses_platform_python(monkeypatch, tmp_path):
    monkeypatch.setattr(install, "IS_WINDOWS", False)
    monkeypatch.setattr(install, "ROOT", tmp_path)
    seen = []

    def run(args, **kw):
        import subprocess
        seen.append(args)
        return subprocess.CompletedProcess(args, 1 if args[2] == "get" else 0, b"", b"")
    install.register_mcp(run=run, claude="claude")
    assert seen[-1][-2:] == [str(tmp_path / ".venv" / "bin" / "python"), str(install.ENTRY)]


def test_autostart_refuses_other_platforms(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(autostart, "IS_WINDOWS", False)
    monkeypatch.setattr(autostart.subprocess, "run", lambda *a, **kw: calls.append(a))
    for argv in (["--install"], ["--check-install"], ["--show"], []):
        assert autostart.main(argv) == 1
    assert calls == [] and "run_web.py" in capsys.readouterr().out


def test_install_skills_copies_every_skill_folder(tmp_path):
    src, dst = tmp_path / "skill", tmp_path / "dst"
    for name in ("qa", "qa-harvest"):
        (src / name).mkdir(parents=True)
        (src / name / "SKILL.md").write_text(name, encoding="utf-8")
    msgs = install.install_skills(src, dst)
    assert len(msgs) == 2 and (dst / "qa-harvest" / "SKILL.md").read_text(encoding="utf-8") == "qa-harvest"
    assert all("그대로" in m for m in install.install_skills(src, dst))


def test_install_skills_fills_marks_in_md_only(tmp_path):
    src, dst, root = tmp_path / "skill", tmp_path / "dst", tmp_path / "repo dir"
    (src / "qa").mkdir(parents=True)
    (src / "qa" / "SKILL.md").write_text(f"{install.CLI_MARK} lint-draft x / {install.RULES_MARK}", encoding="utf-8")
    (src / "qa" / "tool.py").write_text(install.CLI_MARK, encoding="utf-8")
    marks = install.local_marks(root)
    install.install_skills(src, dst, marks)
    py, cli = (root / ".venv" / "Scripts" / "python.exe").as_posix(), (root / "fq_client" / "cli.py").as_posix()
    assert (dst / "qa" / "SKILL.md").read_text(encoding="utf-8") == \
        f'"{py}" "{cli}" lint-draft x / ~/.claude/skills/qa/rules.md'
    assert (dst / "qa" / "tool.py").read_text(encoding="utf-8") == install.CLI_MARK
    assert all("그대로" in m for m in install.install_skills(src, dst, marks))
    assert "덮어씀" in install.install_skills(src, dst, {**marks, install.RULES_MARK: "다른 곳"})[0]


def test_skill_sources_use_marks_not_repo_paths():
    texts = {f: f.read_text(encoding="utf-8") for f in install.SKILLS_SRC.rglob("*.md")}
    for f, t in texts.items():
        assert "fast-question/" not in t and ".venv" not in t and "{{FAST_QUESTION_" not in t, f
    harvest = (install.SKILLS_SRC / "qa-harvest" / "SKILL.md").read_text(encoding="utf-8")
    assert f"{install.CLI_MARK} inventory" in harvest and f"{install.CLI_MARK} lint-draft" in harvest
    assert install.RULES_MARK in harvest


def test_skill_files_have_no_control_characters():
    """역슬래시 경로를 파이썬 문자열로 옮기다 폼 피드·벨 같은 제어 문자가 들어간 적이 있다."""
    import re
    for f in install.SKILLS_SRC.rglob("*.md"):
        assert not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", f.read_text(encoding="utf-8")), f


def test_skill_sources_take_item_address_from_tool_results():
    texts = [f.read_text(encoding="utf-8") for f in install.SKILLS_SRC.rglob("SKILL.md")]
    assert all("127.0.0.1:8765" not in t and "#qa/<id>" not in t for t in texts)
    assert all("<결과의 url>" in t for t in texts)


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
