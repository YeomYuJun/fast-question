"""로컬·원격 전환: credentials.json 의 mode 와 scripts/install.py 의 --local·--remote."""
import io
import json
import subprocess
from urllib.error import HTTPError

import pytest

from kb import remote as remote_mod
from scripts import install

URL = "https://fq.example.com"


def _cred(**extra):
    path = remote_mod.credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": URL, "token": "fq_pat_abc", "device_id": "dev-1", **extra}), encoding="utf-8")
    return path


def _stored():
    return json.loads(remote_mod.credentials_path().read_text(encoding="utf-8"))


def test_save_keeps_mode_and_set_mode_keeps_token():
    remote_mod.save_credentials(URL, "fq_pat_abc", mode="local")
    remote_mod.save_credentials(URL, "fq_pat_new")  # login 처럼 mode 없이 다시 써도 남는다
    assert _stored()["mode"] == "local" and remote_mod.load_credentials()["mode"] == "local"
    remote_mod.set_mode("remote")
    got = remote_mod.load_credentials()
    assert (got["mode"], got["token"]) == ("remote", "fq_pat_new")
    with pytest.raises(ValueError):
        remote_mod.set_mode("both")


def test_device_id_backfill_keeps_mode():
    path = remote_mod.credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": URL, "token": "fq_pat_abc", "mode": "local"}), encoding="utf-8")
    assert remote_mod.load_credentials()["mode"] == "local"
    assert _stored()["device_id"] and _stored()["mode"] == "local"


def test_missing_or_unknown_mode_loads_as_none():
    _cred()
    assert remote_mod.load_credentials()["mode"] is None
    _cred(mode="x")
    assert remote_mod.load_credentials()["mode"] is None


class FakeRun:
    """claude CLI 흉내. registered 는 mcp get 의 출력(None 이면 등록 없음)."""

    def __init__(self, registered: bytes | None):
        self.registered, self.calls = registered, []

    def __call__(self, args, **kw):
        self.calls.append(args[1:3] + (["--remote"] if "--remote" in args else []))
        ok = args[2] != "get" or self.registered is not None
        return subprocess.CompletedProcess(args, 0 if ok else 1, self.registered or b"", b"")

    @property
    def added(self):
        return [c for c in self.calls if c[:2] == ["mcp", "add"]]


def _opener(status: int, seen: list):
    def opener(req, timeout):
        seen.append(req.get_header("Authorization"))
        if status != 200:
            raise HTTPError(req.full_url, status, "x", {}, io.BytesIO(b"{}"))
        return io.BytesIO(json.dumps({"account": {"id": 1, "display_name": "octo"}}).encode())
    return opener


@pytest.fixture
def env(tmp_path, monkeypatch, capsys):
    """main() 이 실제 ~/.claude·claude CLI·서버·DB 를 건드리지 않게 바꾼다."""
    monkeypatch.setattr(install.sys.stdout, "reconfigure", lambda **kw: None)  # capsys 가 받은 글자를 그대로 둔다
    py = tmp_path / "python.exe"
    py.write_text("")
    monkeypatch.setattr(install, "venv_python", lambda root=None: py)
    monkeypatch.setattr(install, "SETTINGS", tmp_path / "claude" / "settings.json")
    monkeypatch.setattr(install, "SKILLS_DST", tmp_path / "claude" / "skills")
    (tmp_path / "claude").mkdir()
    monkeypatch.setattr(install, "seed_guide", lambda: "기본 가이드: 시험")
    monkeypatch.setattr(install.shutil, "which", lambda name: "claude")
    monkeypatch.setattr(install, "read_token", lambda: pytest.fail("토큰을 다시 묻지 않는다"))

    class Env:
        def run(self, registered=None, status=200):
            self.mcp, self.seen = FakeRun(registered), []
            monkeypatch.setattr(install.subprocess, "run", self.mcp)
            monkeypatch.setattr(install, "urlopen", _opener(status, self.seen))
    return Env()


def test_no_args_keeps_stored_remote_mode(env, capsys):
    _cred(mode="remote")
    env.run(b"Command: python.exe run_mcp.py --remote")
    assert install.main([]) == 0
    assert env.mcp.added == []  # 이미 원격 프록시로 등록돼 있어 그대로 둔다
    assert _stored()["mode"] == "remote"
    assert "원격 모드" in capsys.readouterr().out


def test_no_args_without_mode_follows_current_registration_and_writes_it(env):
    _cred()
    env.run(b"Command: python.exe run_mcp.py --remote")
    assert install.main([]) == 0
    assert _stored()["mode"] == "remote" and env.mcp.added == []
    _cred()
    env.run(b"Command: python.exe run_mcp.py")
    assert install.main([]) == 0
    assert _stored()["mode"] == "local"


def test_no_args_without_credentials_is_local(env):
    env.run(None)
    assert install.main([]) == 0
    assert env.mcp.added == [["mcp", "add"]]
    assert not remote_mod.credentials_path().exists()


def test_local_switch_keeps_token_and_device_id(env, capsys):
    _cred(mode="remote")
    env.run(b"Command: python.exe run_mcp.py --remote")
    assert install.main(["--local"]) == 0
    assert env.mcp.added == [["mcp", "add"]]
    got = _stored()
    assert (got["mode"], got["token"], got["device_id"]) == ("local", "fq_pat_abc", "dev-1")
    out = capsys.readouterr().out
    assert "로컬 모드" in out and "push_local" in out and "/mcp" in out


def test_remote_switch_reuses_saved_token_after_check(env):
    _cred(mode="local")
    env.run(b"Command: python.exe run_mcp.py")
    assert install.main(["--remote"]) == 0
    assert env.seen == ["Bearer fq_pat_abc"]
    assert env.mcp.added == [["mcp", "add", "--remote"]]
    got = _stored()
    assert (got["mode"], got["token"], got["device_id"]) == ("remote", "fq_pat_abc", "dev-1")


def test_remote_switch_with_same_url_does_not_ask_token(env):
    _cred(mode="local")
    env.run(b"Command: python.exe run_mcp.py")
    assert install.main(["--remote", URL + "/"]) == 0
    assert _stored()["mode"] == "remote"


def test_remote_switch_to_other_server_asks_token(env, monkeypatch):
    _cred(mode="local")
    env.run(b"Command: python.exe run_mcp.py")
    monkeypatch.setattr(install, "read_token", lambda: "fq_pat_other")
    assert install.main(["--remote", "https://other.example.com"]) == 0
    got = _stored()
    assert (got["url"], got["token"], got["mode"], got["device_id"]) == (
        "https://other.example.com", "fq_pat_other", "remote", "dev-1")


def test_remote_switch_with_rejected_token_changes_nothing(env, capsys):
    _cred(mode="local")
    env.run(b"Command: python.exe run_mcp.py", status=401)
    assert install.main(["--remote"]) == 1
    assert env.mcp.added == [] and _stored()["mode"] == "local"
    assert "login" in capsys.readouterr().out


def test_rejected_remote_switch_does_not_write_mode_to_old_file(env):
    _cred()
    env.run(b"Command: python.exe run_mcp.py", status=401)
    assert install.main(["--remote"]) == 1
    assert "mode" not in _stored()


def test_remote_to_other_server_asks_to_reconnect(env, monkeypatch, capsys):
    _cred(mode="remote")
    env.run(b"Command: python.exe run_mcp.py --remote")
    monkeypatch.setattr(install, "read_token", lambda: "fq_pat_other")
    assert install.main(["--remote", "https://other.example.com"]) == 0
    assert "/mcp" in capsys.readouterr().out


def test_remote_switch_without_credentials_needs_url(env, capsys):
    env.run(None)
    assert install.main(["--remote"]) == 1
    assert env.mcp.added == []
    assert "--remote <" in capsys.readouterr().out


def test_local_and_remote_together_is_refused(env):
    env.run(None)
    assert install.main(["--local", "--remote"]) == 1
    assert env.mcp.calls == []
