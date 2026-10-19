"""PC 클라이언트 패키지(pyproject.toml, fq_client): 서버 모듈 없이 동작하고, 필요한 파일이 패키지 목록에 다 있다."""
import json
import subprocess
import sys
import tomllib
from pathlib import Path

from tests.test_hook import FakeRemote, _cred_file, _db_with_item, _item

ROOT = Path(__file__).resolve().parent.parent
ENTRY_MODULES = ("fq_client.cli", "fq_client.inventory", "fq_client.lint_draft", "fq_client.login",
                 "mcp_server.proxy", "hooks.ask_kb", "kb.remote")
SERVER_MODULES = ("mcp_server.server", "kb.store", "kb.morph", "kb.control", "kb.db", "web")
HEAVY_PACKAGES = ("kiwipiepy", "numpy", "boto3")  # 서버만 쓴다. starlette·jwt·cryptography 는 mcp 의 의존성이다


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _loaded_after_import() -> list[str]:
    code = (f"import sys, json; sys.path.insert(0, {str(ROOT)!r})\n"
            f"for m in {ENTRY_MODULES!r}: __import__(m)\n"
            "print(json.dumps(sorted(sys.modules)))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60, cwd=str(ROOT.parent))
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return json.loads(r.stdout)


def test_client_entry_points_do_not_import_server_modules():
    loaded = set(_loaded_after_import())
    assert not [m for m in loaded if m in SERVER_MODULES or m.startswith("web.")]
    assert not [m for m in loaded if m.split(".")[0] in HEAVY_PACKAGES]


def test_every_imported_project_module_is_packaged():
    wheel = set(_pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["only-include"])
    tops = {"fq_client", "hooks", "kb", "mcp_server"}
    needed = set()
    for m in _loaded_after_import():
        parts = m.split(".")
        if parts[0] in tops:
            p = "/".join(parts)
            needed.add(f"{p}/__init__.py" if (ROOT / p).is_dir() else f"{p}.py")
    assert needed <= wheel, sorted(needed - wheel)
    assert all((ROOT / f).is_file() for f in wheel)


def test_sdist_holds_the_same_files_as_the_wheel():
    targets = _pyproject()["tool"]["hatch"]["build"]["targets"]
    assert set(targets["sdist"]["only-include"]) - {"pyproject.toml"} == set(targets["wheel"]["only-include"])


def test_version_comes_from_the_package():
    from fq_client import __version__
    r = subprocess.run([sys.executable, str(ROOT / "fq_client" / "cli.py"), "--version"], capture_output=True, timeout=60)
    assert r.returncode == 0 and r.stdout.decode().strip() == f"fast-question {__version__}"


def _hook(tmp_path, stdin: dict, env: dict):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"db_path": str(_db_with_item(tmp_path)), "port": 1}), encoding="utf-8")
    return subprocess.run([sys.executable, str(ROOT / "fq_client" / "cli.py"), "hook"],
                          input=json.dumps(stdin).encode("utf-8"), capture_output=True, timeout=60,
                          env={"SYSTEMROOT": "C:\\Windows", "FAST_QUESTION_CONFIG": str(cfg), **env})


def test_client_hook_without_credentials_does_not_read_the_local_db(tmp_path):
    r = _hook(tmp_path, {"prompt": ".part 에 먼저 쓰는 이유가 뭐야?"},
              {"FQ_CREDENTIALS": str(tmp_path / "없음.json"), "USERPROFILE": str(tmp_path / "home")})
    assert r.returncode == 0 and r.stdout == b""  # 로컬 DB 에는 맞는 항목이 있다


def test_client_hook_uses_the_remote_server(tmp_path):
    remote = FakeRemote(hits=[_item(7, "원격에 있는 질문")])
    try:
        cred = _cred_file(tmp_path, remote.url)
        r = _hook(tmp_path, {"prompt": ".part 에 먼저 쓰는 이유가 뭐야?"},
                  {"FQ_CREDENTIALS": str(cred), "USERPROFILE": str(tmp_path / "home")})
    finally:
        remote.close()
    text = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert f"{remote.url}/#qa/7" in text and "#1" not in text


def test_client_mcp_without_credentials_exits_with_a_message(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "fq_client" / "cli.py"), "mcp"], capture_output=True, timeout=60,
                       env={"SYSTEMROOT": "C:\\Windows", "FQ_CREDENTIALS": str(tmp_path / "없음.json")})
    assert r.returncode == 1 and r.stdout == b"" and "없음.json" in r.stderr.decode("utf-8")
