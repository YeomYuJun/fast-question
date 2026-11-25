"""조사 턴 수확 훅(hooks/stop_harvest.py)."""
import json
import subprocess
import sys
from pathlib import Path

from hooks import ask_kb, stop_harvest

ROOT = Path(__file__).resolve().parent.parent


def _line(kind, **block):
    return json.dumps({"type": kind, "message": {"content": [block]}}, ensure_ascii=False, separators=(",", ":"))


def _use(name, **inp):
    return _line("assistant", type="tool_use", name=name, input=inp)


def _prompt(text="질문"):
    return json.dumps({"type": "user", "message": {"content": text}}, ensure_ascii=False, separators=(",", ":"))


def _write(tmp_path, lines, name="t.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _transcript(tmp_path, reads=0, writes=0, extra=()):
    """사용자 입력 한 줄 뒤에 읽기 reads 번, KB 쓰기 writes 번이 이어진 한 턴."""
    lines = [_prompt()] + [_use("Read", file_path="x") for _ in range(reads)]
    lines += [_use("mcp__fast-question__kb_add") for _ in range(writes)]
    lines += [_line("user", type="tool_result", content="ok"), "{broken json", *extra]
    return _write(tmp_path, lines)


def test_scan_counts_the_last_turn_separately(tmp_path):
    p = _write(tmp_path, [
        _prompt("첫 지시"),
        *[_use("Read", file_path="a") for _ in range(3)],
        _use("Write", file_path=r"D:\w\out.html"),
        _use("Edit", file_path=r"D:\w\note.md"),
        _use("Write", file_path=r"D:\w\out.html"),
        _use("Write", file_path=r"C:\Users\u\AppData\Local\Temp\claude\s\scratchpad\check.js"),  # 임시 파일은 뺀다
        _use("mcp__fast-question__kb_add"),
        _line("user", type="tool_result", content="ok"),  # 도구 결과는 턴을 나누지 않는다
        json.dumps({"type": "user", "isMeta": True, "message": {"content": "하니스가 넣은 줄"}}, separators=(",", ":")),
        _prompt("둘째 질문"),
        _use("Grep", pattern="x"), _use("Bash", command="git log"),
        "{broken json",
    ])
    assert stop_harvest.scan(p) == {"reads": 5, "turn_reads": 2, "turn_edits": 0, "turn_writes": 0,
                                    "edited": [r"D:\w\note.md", r"D:\w\out.html"], "turn_start": "", "cited": []}
    assert stop_harvest.scan(tmp_path / "none.jsonl")["reads"] == 0


def _stamped(text, stamp):
    return json.dumps({"type": "user", "timestamp": stamp, "message": {"content": text}}, ensure_ascii=False,
                      separators=(",", ":"))


def test_scan_reads_turn_start_and_item_urls_in_the_last_turn(tmp_path):
    p = _write(tmp_path, [
        _stamped("첫 질문", "2026-11-25T00:10:00.000Z"),
        _line("assistant", type="text", text="앞 턴의 답 https://fast-question.com/#qa/7"),
        _stamped("둘째 질문", "2026-11-25T00:19:32.159Z"),
        _line("user", type="tool_result", content='{"url": "https://fast-question.com/#qa/99"}'),  # 도구 결과의 주소는 세지 않는다
        _line("assistant", type="text", text="답이다. https://fast-question.com/#qa/12 와 http://127.0.0.1:8765/#qa/31, #qa/12"),
    ])
    got = stop_harvest.scan(p)
    assert (got["turn_start"], got["cited"]) == ("2026-11-25T00:19:32Z", [12, 31])


def test_decide_prompts_when_the_turn_missed_a_search_and_then_read():
    turn = {"reads": 3, "turn_reads": 2, "turn_edits": 1, "turn_writes": 0, "edited": []}
    missed = [("넷퍼넬 호출처", 1)]
    assert stop_harvest.decide(turn, None, missed) == (True, {"reads": 3})  # 파일을 쓴 턴이어도 넣는다
    assert stop_harvest.decide(dict(turn, turn_reads=1), None, missed) == (False, None)  # 읽어서 알아낸 것이 없다
    assert stop_harvest.decide(dict(turn, turn_writes=1), None, missed) == (False, None)  # 이미 등록했다
    assert stop_harvest.decide(turn, None, []) == (False, None)
    # KB 에 답이 없는 질문으로 시작한 턴도 읽어서 답했을 때만 넣는다
    assert stop_harvest.decide(turn, None, [], asked=True) == (True, {"reads": 3})
    assert stop_harvest.decide(dict(turn, turn_reads=0), None, [], asked=True) == (False, None)
    text = stop_harvest.context("이관", asked=True)
    assert "질문으로 시작했고" in text and "의견이나 판단" in text and "기본" in text


def test_hook_prompts_after_a_question_without_answer_only_when_the_turn_read(tmp_path):
    db = _project_db(tmp_path)
    marker = tmp_path / "home" / ".fast-question" / "stop" / "s-8.asked"
    base = {"session_id": "s-8", "cwd": r"D:\fast-question", "stop_hook_active": False}
    # 의견을 물은 질문: 읽지 않고 답했다. 표시는 지워지고 안내는 없다
    marker.parent.mkdir(parents=True)
    marker.write_text("", encoding="utf-8")
    talk = _write(tmp_path, [_prompt("이 구조에 대한 네 생각은 어때?"), _line("assistant", type="text", text="의견이다")], "a.jsonl")
    assert _run(tmp_path, dict(base, transcript_path=str(talk)), {"db_path": str(db)}).stdout == b""
    assert not marker.exists()
    # 사실을 물은 질문: 파일을 읽어서 답했다
    marker.write_text("", encoding="utf-8")
    fact = _write(tmp_path, [_prompt("RateLimitFilter 는 분당 몇 회야?"), _use("Grep", pattern="RateLimit"),
                             _use("Read", file_path="a"), _line("assistant", type="text", text="답이다")], "b.jsonl")
    r = _run(tmp_path, dict(base, transcript_path=str(fact)), {"db_path": str(db)})
    text = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "질문으로 시작했고" in text and "kb_add" in text and not marker.exists()
    # 표시가 없으면 같은 전사에 조용하다(읽기 2회는 조사 턴이 아니다)
    assert _run(tmp_path, dict(base, session_id="s-9", transcript_path=str(fact)), {"db_path": str(db)}).stdout == b""


def test_decide_prompts_only_after_an_investigation_turn():
    turn = {"reads": 6, "turn_reads": 6, "turn_edits": 0, "turn_writes": 0, "edited": []}
    assert stop_harvest.decide(dict(turn, turn_reads=5), None) == (False, None)
    assert stop_harvest.decide(turn, None) == (True, {"reads": 6})
    # 파일을 만들거나 고친 턴, 이미 등록한 턴에는 넣지 않는다
    assert stop_harvest.decide(dict(turn, turn_edits=1), None) == (False, None)
    assert stop_harvest.decide(dict(turn, turn_writes=1), None) == (False, None)
    # 한 번 넣은 뒤에는 세션 읽기가 NEXT_STEP 만큼 늘어야 다시 넣는다
    assert stop_harvest.decide(dict(turn, reads=17), {"reads": 6}) == (False, None)
    assert stop_harvest.decide(dict(turn, reads=18), {"reads": 6}) == (True, {"reads": 18})


def test_context_gives_conditions_and_names_files_the_session_wrote():
    text = stop_harvest.context("d:/e-drive", [rf"D:\E-DRIVE\00-command\f{i}.html" for i in range(7)])
    assert "기본" in text and "kb_add" in text and "비용" in text and "d:/e-drive" in text
    assert "f6.html" in text and "f2.html" in text and "f1.html" not in text and "외 2개" in text
    assert "만들거나 고친 파일" not in stop_harvest.context("p").splitlines()[-2]


def _run(tmp_path, stdin: dict, cfg: dict):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return subprocess.run([sys.executable, str(ROOT / "hooks" / "stop_harvest.py")],
                          input=json.dumps(stdin).encode(), capture_output=True, timeout=60,
                          env={"FAST_QUESTION_CONFIG": str(path), "SYSTEMROOT": r"C:\Windows",
                               "USERPROFILE": str(tmp_path / "home"), "FQ_CREDENTIALS": str(tmp_path / "none.json")})


def _project_db(tmp_path):
    from kb.db import connect
    from kb.store import Store
    db = tmp_path / "kb.sqlite3"
    conn = connect(db)
    Store(conn, "claude").add_category("fast-question")
    conn.close()
    return db


def test_hook_prompts_in_kb_project_and_only_once(tmp_path):
    db = _project_db(tmp_path)
    t = _transcript(tmp_path, reads=7)
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


def test_hook_is_silent_when_the_turn_wrote_files_or_registered(tmp_path):
    db = _project_db(tmp_path)
    base = {"cwd": r"D:\fast-question", "stop_hook_active": False}
    wrote = _transcript(tmp_path, reads=20, extra=[_use("Write", file_path=r"D:\fast-question\x.html")])
    assert _run(tmp_path, dict(base, session_id="s-4", transcript_path=str(wrote)), {"db_path": str(db)}).stdout == b""
    added = _transcript(tmp_path, reads=20, writes=1)
    assert _run(tmp_path, dict(base, session_id="s-5", transcript_path=str(added)), {"db_path": str(db)}).stdout == b""
    # 앞 턴에서 많이 읽었어도 마지막 턴이 조사 턴이 아니면 조용하다
    later = _write(tmp_path, [_prompt(), *[_use("Read", file_path="x") for _ in range(20)], _prompt("고마워"),
                              _use("Read", file_path="x")])
    assert _run(tmp_path, dict(base, session_id="s-6", transcript_path=str(later)), {"db_path": str(db)}).stdout == b""


def test_hook_records_cited_items_and_prompts_on_a_missed_search(tmp_path):
    db = _project_db(tmp_path)
    log = tmp_path / "home" / ".fast-question" / "use.jsonl"
    log.parent.mkdir(parents=True)
    rows = [{"at": "2026-11-25T00:00:00Z", "kind": "miss", "cwd": "d:/fast-question", "items": [], "queries": ["앞 턴 검색어"]},
            {"at": "2026-11-25T00:20:00Z", "kind": "miss", "cwd": "d:/fast-question", "items": [], "queries": ["RateLimitFilter"]}]
    log.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    t = _write(tmp_path, [
        _stamped("RateLimitFilter 가 왜 429 를 내지", "2026-11-25T00:19:32.159Z"),
        _use("mcp__fast-question__kb_search", query="RateLimitFilter"),
        _use("Read", file_path="a"), _use("Grep", pattern="b"),
        _use("Edit", file_path=r"D:\fast-question\x.py"),
        _line("assistant", type="text", text="원인은 … 관련 항목 https://fast-question.com/#qa/12"),
    ])
    stdin = {"session_id": "s-7", "transcript_path": str(t), "cwd": r"D:\fast-question", "stop_hook_active": False}
    r = _run(tmp_path, stdin, {"db_path": str(db)})
    assert r.returncode == 0, r.stderr
    text = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "찾았으나 없었던 검색어: 'RateLimitFilter'" in text and "앞 턴 검색어" not in text and "x.py" in text
    cites = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if '"cite"' in ln]
    assert [(c["cwd"], c["items"]) for c in cites] == [("d:/fast-question", [[12, ""]])]
    nudges = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if '"nudge"' in ln]
    assert [(n["cwd"], n["tag"]) for n in nudges] == [("d:/fast-question", "missed")]  # 안내를 넣은 것과 그 경우를 적는다
    # 이 훅 때문에 이어 간 응답에서는 다시 적지 않는다
    _run(tmp_path, dict(stdin, stop_hook_active=True), {"db_path": str(db)})
    assert log.read_text(encoding="utf-8").count('"cite"') == 1


def test_hook_is_silent_outside_kb_project_or_with_few_reads(tmp_path):
    from kb.db import connect
    db = tmp_path / "kb.sqlite3"
    connect(db).close()
    t = _transcript(tmp_path, reads=7)
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
