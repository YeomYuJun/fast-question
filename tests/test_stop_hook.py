"""세션 끝 수확 훅(hooks/stop_harvest.py)."""
import json
import subprocess
import sys
from pathlib import Path

from hooks import ask_kb, stop_harvest

ROOT = Path(__file__).resolve().parent.parent


def _line(kind, **block):
    return json.dumps({"type": kind, "message": {"content": [block]}}, ensure_ascii=False)


def _transcript(tmp_path, reads=0, writes=0, extra=""):
    lines = [_line("assistant", type="tool_use", name="Read", input={"file_path": "x"}) for _ in range(reads)]
    lines += [_line("assistant", type="tool_use", name="mcp__fast-question__kb_add", input={}) for _ in range(writes)]
    lines += [_line("user", type="tool_result", content="ok"), "{broken json", extra]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_scan_counts_read_and_kb_write_tools_only(tmp_path):
    p = _transcript(tmp_path, reads=3, writes=2,
                    extra=_line("assistant", type="tool_use", name="Edit", input={}))
    assert stop_harvest.scan(p) == {"reads": 3, "writes": 2}
    assert stop_harvest.scan(tmp_path / "none.jsonl") == {"reads": 0, "writes": 0}


def test_decide_prompts_once_then_after_more_reads_unless_written():
    assert stop_harvest.decide({"reads": 3, "writes": 0}, None) == (False, None)
    assert stop_harvest.decide({"reads": 4, "writes": 0}, None) == (True, {"reads": 4, "writes": 0})
    state = {"reads": 4, "writes": 0}
    assert stop_harvest.decide({"reads": 10, "writes": 0}, state) == (False, None)
    assert stop_harvest.decide({"reads": 16, "writes": 0}, state) == (True, {"reads": 16, "writes": 0})
    # 지시 뒤 등록했으면 묻지 않고 기준점만 옮긴다
    assert stop_harvest.decide({"reads": 20, "writes": 1}, state) == (False, {"reads": 20, "writes": 1})


def _run(tmp_path, stdin: dict, cfg: dict):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return subprocess.run([sys.executable, str(ROOT / "hooks" / "stop_harvest.py")],
                          input=json.dumps(stdin).encode(), capture_output=True, timeout=60,
                          env={"FAST_QUESTION_CONFIG": str(path), "SYSTEMROOT": r"C:\Windows",
                               "USERPROFILE": str(tmp_path / "home"), "FQ_CREDENTIALS": str(tmp_path / "none.json")})


def test_hook_prompts_in_kb_project_and_only_once(tmp_path):
    from kb.db import connect
    from kb.store import Store
    db = tmp_path / "kb.sqlite3"
    conn = connect(db)
    Store(conn, "claude").add_category("fast-question")
    conn.close()
    t = _transcript(tmp_path, reads=5)
    stdin = {"session_id": "s-1", "transcript_path": str(t), "cwd": r"D:\fast-question", "stop_hook_active": False}
    r = _run(tmp_path, stdin, {"db_path": str(db)})
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "Stop" and "fast-question" in out["additionalContext"] and "kb_add" in out["additionalContext"]
    assert (tmp_path / "home" / ".fast-question" / "stop" / "s-1.json").exists()
    # 같은 상태에서 다시 멈추면 조용하다
    assert _run(tmp_path, stdin, {"db_path": str(db)}).stdout == b""
    # 이 훅 때문에 이어 간 응답이면 조용하다
    assert _run(tmp_path, dict(stdin, session_id="s-2", stop_hook_active=True), {"db_path": str(db)}).stdout == b""


def test_hook_is_silent_outside_kb_project_or_with_few_reads(tmp_path):
    from kb.db import connect
    db = tmp_path / "kb.sqlite3"
    connect(db).close()
    t = _transcript(tmp_path, reads=5)
    base = {"session_id": "s-3", "transcript_path": str(t), "cwd": r"C:\Users\someone\other"}
    assert _run(tmp_path, base, {"db_path": str(db)}).stdout == b""
    few = _transcript(tmp_path, reads=2)
    assert _run(tmp_path, dict(base, transcript_path=str(few), cwd=r"D:\fast-question"), {"db_path": str(db)}).stdout == b""
    assert _run(tmp_path, {}, {"db_path": str(db)}).stdout == b""


def test_prompt_hook_strips_ide_tags():
    tagged = ("<ide_opened_file>The user opened the file d:/x/README.md in the IDE. This may or may not be related.</ide_opened_file>"
              "서비스로 보는 거지 그래서 로컬에 저장해서 측정하는 건 의미가 없을 걸?")
    assert ask_kb.strip_system_tags(tagged).startswith("서비스로")
    assert "README" not in ask_kb.to_query(tagged)
    assert ask_kb.is_question("<ide_selection>x</ide_selection>k8s 에서 Pod 와 Deployment 차이가 뭐야")
    assert ask_kb.to_query('<pasted_content id="1">\n왜 .part 를 쓰나요?\n</pasted_content id="1">') == ".part 를 쓰나요"
