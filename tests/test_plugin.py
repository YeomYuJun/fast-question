"""Claude Code 플러그인(plugin/fast-question, scripts/build_plugin.py): 올려 둔 폴더가 원본에서 다시 만든 결과와 같다."""
import json
import re
from pathlib import Path

from fq_client import __version__
from scripts import build_plugin

ROOT = Path(__file__).resolve().parent.parent


def _committed(top: Path) -> dict[str, bytes]:
    # 이 PC 는 git 이 작업 폴더에 CRLF 로 꺼낸다(core.autocrlf). 만드는 쪽은 LF 다
    return {p.relative_to(top).as_posix(): p.read_bytes().replace(b"\r\n", b"\n")
            for p in sorted(top.rglob("*")) if p.is_file()}


def test_plugin_folder_matches_a_fresh_build():
    assert _committed(build_plugin.PLUGIN_DIR) == build_plugin.plugin_files(f"fast-question=={__version__}")
    assert build_plugin.MARKETPLACE.read_bytes().replace(b"\r\n", b"\n") == build_plugin.marketplace()


def test_plugin_has_no_marks_or_local_paths():
    for rel, data in build_plugin.plugin_files(f"fast-question=={__version__}").items():
        text = data.decode("utf-8")
        assert "{{" not in text, rel
        assert not re.search(r"\b[A-Za-z]:[\\/]", text), rel  # 드라이브 경로(이 PC 의 저장소 위치)


def test_plugin_pins_the_client_version():
    files = build_plugin.plugin_files(f"fast-question=={__version__}")
    assert json.loads(files[".claude-plugin/plugin.json"])["version"] == __version__
    mcp = json.loads(files[".mcp.json"])["mcpServers"]["fast-question"]
    assert mcp == {"command": "uvx", "args": ["--from", f"fast-question=={__version__}", "fast-question", "mcp"]}
    (hook,) = json.loads(files["hooks/hooks.json"])["hooks"]["UserPromptSubmit"][0]["hooks"]
    assert hook["command"] == "uvx" and hook["args"][-1] == "hook" and f"fast-question=={__version__}" in hook["args"]
    harvest = files["skills/qa-harvest/SKILL.md"].decode("utf-8")
    assert f"uvx --from fast-question=={__version__} fast-question lint-draft" in harvest
    assert "${CLAUDE_PLUGIN_ROOT}/skills/qa/rules.md" in harvest


def test_marketplace_points_at_the_plugin_folder():
    data = json.loads(build_plugin.marketplace())
    (entry,) = data["plugins"]
    assert entry["name"] == data["name"] == "fast-question"
    assert (ROOT / entry["source"]).resolve() == build_plugin.PLUGIN_DIR.resolve()


def test_test_build_quotes_paths_with_spaces(tmp_path):
    whl = str(tmp_path / "a b" / "fast_question-0.1.0-py3-none-any.whl")
    files = build_plugin.plugin_files(whl)
    assert f'uvx --from "{whl}" fast-question inventory' in files["skills/qa-harvest/SKILL.md"].decode("utf-8")
    assert json.loads(files[".mcp.json"])["mcpServers"]["fast-question"]["args"][1] == whl
