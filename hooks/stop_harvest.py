"""Claude Code Stop 훅(조사 턴 수확). Claude 가 응답을 마칠 때, 방금 끝난 턴이 파일을 여러 번 읽어 답한 조사 턴이거나
kb_search 로 찾았으나 없었던 것을 읽어서 알아낸 턴이면 등록 조건(hooks/ask_kb.py store_conditions)을 additionalContext 로
넣어 한 번 더 이어 가게 한다. 등록하지 않는 것이 기본이다.
그 턴의 응답 글에 항목 주소(#qa/<id>)가 있으면 사용 기록(kb/uselog.py)에 cite 로 적는다. 찾아진 항목이 답에 쓰였는지를
재는 자료다. 이 기록은 안내를 넣지 않는 턴에도 남긴다.

- 입력(stdin JSON): session_id, transcript_path, cwd, stop_hook_active. stop_hook_active 가 true 면(이 훅 때문에 이어 간
  응답이다) 아무것도 하지 않는다.
- 전사(transcript_path, jsonl)를 턴으로 나눈다. 턴은 사용자가 입력한 줄(type user, tool_result 블록이 없고 isMeta 가 아님)
  에서 시작한다. 전사 형식은 Claude Code 문서에 없어 한 줄에 {"type": "assistant", "message": {"content":
  [{"type": "tool_use", "name": ..., "input": ...}]}} 꼴만 본다. 형식이 다르면 세지 못해 아무것도 넣지 않는다.
- 넣는 경우는 둘이다. 어느 쪽이든 마지막 턴에 KB 쓰기 도구(kb_add·kb_update·kb_add_many) 호출이 있으면 넣지 않는다
  (이미 등록했다).
  A. 조사 턴. 아래를 모두 만족한다.
     1. 마지막 턴의 읽기 도구(READ_TOOLS) 호출이 TURN_MIN 이상이다. 조사 없이 답한 턴에는 다시 알아내기 어려운 사실이 없다.
     2. 마지막 턴에 파일을 쓰는 도구(EDIT_TOOLS) 호출이 없다. 파일을 만들거나 고치는 턴은 작업이 진행 중이고, 그때
        확인한 것은 Claude 가 방금 쓴 내용이다.
     3. 이 세션에서 마지막으로 넣은 뒤 읽기가 NEXT_STEP 이상 늘었다. 세션마다 상태 파일
        (%USERPROFILE%\\.fast-question\\stop\\<session_id>.json)에 넣었을 때의 읽기 수를 둔다.
  B. KB 에 없던 것을 읽어서 알아낸 턴. 아래 둘 중 하나이고 그 턴의 읽기가 MISS_MIN 이상이다. 파일을 쓴 턴이어도
     넣는다(쓴 파일의 내용은 등록 조건 (3)이 거른다).
     - 마지막 턴이 시작된 뒤(전사의 사용자 입력 줄 timestamp) 사용 기록에 0건 검색(miss)이 있고 아직 등록으로
       메워지지 않았다. Claude 가 필요해서 찾았는데 없었다.
     - 그 턴이 KB 에 답이 없는 질문으로 시작했다. 즉석 질문 훅(hooks/ask_kb.py mark_asked)이 세션 상태 폴더에
       둔 표시로 안다. 사용자가 물었는데 없었다. 읽지 않고 답한 턴(의견을 물은 질문 등)에는 넣지 않는다.
- 질문한 폴더(cwd)가 KB 에 있는 프로젝트(hooks/ask_kb.py project_of 와 같은 기준)일 때만 넣는다.
- 이 세션이 EDIT_TOOLS 로 쓴 파일 이름을 안내에 함께 적는다. 그 파일의 내용은 등록 대상이 아니다.
- 어떤 실패든 아무것도 출력하지 않고 0 으로 끝난다.

설치: scripts/install.py 가 ~/.claude/settings.json 의 hooks.Stop 에, 플러그인은 hooks/hooks.json 의 Stop 에
`fast-question stop-hook` 으로 등록한다.
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hooks.ask_kb import (ASKED, MISSED_MIN_DAYS, REPORT, grounded, mark_asked, project_of,  # noqa: E402
                          remote_projects, session_file, show_terms, store_conditions)

READ_TOOLS = ("Read", "Grep", "Glob", "Bash", "PowerShell", "NotebookRead")
EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
WRITE_TOOLS = ("kb_add", "kb_update", "kb_add_many")
TURN_MIN = 6  # 조사 턴으로 보는 마지막 턴의 읽기 수
MISS_MIN = 2  # 놓친 검색이 있는 턴에서, 답을 읽어서 알아냈다고 보는 읽기 수
NEXT_STEP = 12  # 한 번 넣은 뒤 다시 넣기까지 세션 전체에서 늘어나야 하는 읽기 수
SHOW_EDITED = 5  # 안내에 적는 이 세션이 쓴 파일 수
TEMP_PARTS = ("temp", "tmp", "scratchpad")  # 경로에 이 이름의 폴더가 있으면 임시 파일로 본다
MAX_TRANSCRIPT_BYTES = 200 * 1024 * 1024

_EMPTY = {"reads": 0, "turn_reads": 0, "turn_edits": 0, "turn_writes": 0, "edited": [], "turn_start": "", "cited": []}
_QA_REF = re.compile(r"#qa/(\d+)")  # 항목 주소(mcp_server/server.py link)


def _is_prompt(row: dict) -> bool:
    """사용자가 입력한 줄인가. 도구 결과를 싣는 user 줄과 하니스가 넣은 줄(isMeta)은 턴을 나누지 않는다."""
    if row.get("type") != "user" or row.get("isMeta") or row.get("isSidechain"):
        return False
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    return isinstance(content, list) and bool(content) and not any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def scan(transcript: Path) -> dict:
    """전사에서 세션 전체 읽기 수(reads), 마지막 턴의 읽기·파일 쓰기·KB 쓰기 수(turn_*), 이 세션이 쓴 파일 경로(edited),
    마지막 턴이 시작된 시각(turn_start, 사용 기록과 같은 꼴의 UTC 시각. 전사에 없으면 빈 문자열),
    마지막 턴의 응답 글에 주소가 적힌 항목 id(cited)."""
    if not transcript.is_file() or transcript.stat().st_size > MAX_TRANSCRIPT_BYTES:
        return dict(_EMPTY, edited=[], cited=[])
    reads = turn_reads = turn_edits = turn_writes = 0
    turn_start = ""
    edited: dict[str, None] = {}  # 넣은 순서를 지키는 집합
    cited: dict[int, None] = {}
    with transcript.open("rb") as f:
        for raw in f:
            is_use = b'"tool_use"' in raw
            # 도구 결과 줄은 크고 많다. 블록 머리만 보고 건너뛴다(사용자가 붙여 넣은 글 안의 따옴표는 \" 로 적힌다)
            is_ref = b"#qa/" in raw and b'"type":"assistant"' in raw  # 응답 글에 항목 주소가 있는 줄
            if not is_use and not is_ref and (b'"type":"user"' not in raw or b'"type":"tool_result"' in raw):
                continue
            try:
                row = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if _is_prompt(row):
                turn_reads = turn_edits = turn_writes = 0
                cited.clear()
                stamp = str(row.get("timestamp") or "")  # 2026-11-25T00:19:32.159Z
                turn_start = stamp[:19] + "Z" if len(stamp) >= 19 else ""
                continue
            if row.get("type") != "assistant" or row.get("isSidechain"):
                continue
            content = (row.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "text":
                    cited.update((int(n), None) for n in _QA_REF.findall(str(block.get("text", ""))))
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name", ""))
                if name in READ_TOOLS:
                    reads += 1
                    turn_reads += 1
                elif name in EDIT_TOOLS:
                    turn_edits += 1
                    inp = block.get("input")
                    path = str((inp if isinstance(inp, dict) else {}).get("file_path") or "")
                    # 임시 폴더의 검사용 스크립트는 프로젝트 파일이 아니다. 안내에 적지 않는다
                    if path and not any(part in TEMP_PARTS for part in path.replace("\\", "/").lower().split("/")):
                        edited.pop(path, None)
                        edited[path] = None
                elif any(name.endswith(w) for w in WRITE_TOOLS):
                    turn_writes += 1
    return {"reads": reads, "turn_reads": turn_reads, "turn_edits": turn_edits, "turn_writes": turn_writes,
            "edited": list(edited), "turn_start": turn_start, "cited": list(cited)}


def decide(stats: dict, state: dict | None, turn_missed: list | None = None,
           asked: bool = False) -> tuple[bool, dict | None]:
    """(안내를 넣는가, 새 상태). 넣을 때만 상태를 바꾼다. turn_missed 는 이 턴에서 찾았으나 없었던 검색어,
    asked 는 이 턴이 KB 에 답이 없는 질문으로 시작했는가다."""
    if stats["turn_writes"]:
        return False, None
    if (turn_missed or asked) and stats["turn_reads"] >= MISS_MIN:
        return True, {"reads": stats["reads"]}
    if stats["turn_reads"] < TURN_MIN or stats["turn_edits"]:
        return False, None
    if state is not None and stats["reads"] - state.get("reads", 0) < NEXT_STEP:
        return False, None
    return True, {"reads": stats["reads"]}


def context(project: str, edited: list[str] | None = None, used: list[tuple[str, int]] | None = None,
            missed: list[tuple[str, int]] | None = None, turn_missed: list[tuple[str, int]] | None = None,
            asked: bool = False) -> str:
    """turn_missed 는 이 턴에서, missed 는 이 폴더에서 여러 날 찾았으나 없었던 검색어다(kb/uselog.py open_misses).
    asked 는 이 턴이 KB 에 답이 없는 질문으로 시작했는가다(hooks/ask_kb.py mark_asked)."""
    if asked and not turn_missed:
        head = (f"[fast-question 지식 DB] 이 턴은 사용자의 질문으로 시작했고 그 질문에 맞는 Q&A 가 KB 에 없었다. "
                f"답하느라 {project} 의 파일·명령 결과를 읽었다. 그렇게 알아낸 사실이 다음에도 물어질 것이면 등록 후보다. "
                "의견이나 판단을 물은 것이었거나 답이 이 대화에만 해당하면 등록할 것이 없다. 등록하지 않는 것이 기본이다.")
    elif turn_missed:
        head = (f"[fast-question 지식 DB] 이 턴에서 kb_search 로 찾았으나 없었던 검색어: {show_terms(turn_missed)}. "
                f"그 답을 이 턴에서 {project} 의 파일·명령 결과로 알아냈으면 필요했는데 없던 사실이다(조건 (2)는 채워졌다). "
                "알아내지 못했거나 작업을 시작하며 이름만 넣어 본 것이면 등록할 것이 없다. 등록하지 않는 것이 기본이다.")
    else:
        head = (f"[fast-question 지식 DB] 이 턴은 {project} 의 파일·명령 결과를 여러 번 읽어 답했다. "
                "읽은 내용 전체가 아니라 조사한 경로를 되짚는다: 읽기 전 예상과 달랐던 것, 원인을 찾느라 여러 파일을 거친 것이 "
                "있었는가. 없었으면 등록할 것이 없다. 등록하지 않는 것이 기본이다.")
    lines = [head, *store_conditions(project, used, missed)]
    if edited:
        names = ", ".join(Path(p.replace("\\", "/")).name for p in edited[-SHOW_EDITED:])
        more = f" 외 {len(edited) - SHOW_EDITED}개" if len(edited) > SHOW_EDITED else ""
        lines.append(f"이 세션이 만들거나 고친 파일: {names}{more}. 이 파일들의 내용과 위치는 등록하지 않는다.")
    lines.append("넣을 것이 없으면 아무 말 없이 끝낸다. " + REPORT)
    return "\n".join(lines)


def state_path(session_id: str, home: Path | None = None) -> Path:
    return session_file(session_id, ".json", home)


def main(remote_only: bool = False) -> int:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig")
        data = json.loads(raw) if raw.strip() else {}
        if data.get("stop_hook_active") or not data.get("transcript_path") or not data.get("session_id"):
            return 0
        from kb import uselog
        stats = scan(Path(str(data["transcript_path"])))
        cwd = str(data.get("cwd", ""))
        uselog.record("cite", [(i, "") for i in stats["cited"]], cwd)
        turn_missed = uselog.open_misses(cwd, grounded, since=stats["turn_start"]) if stats["turn_start"] else []
        asked = session_file(str(data["session_id"]), ASKED).exists()
        mark_asked(str(data["session_id"]), False)  # 한 턴에 한 번만 쓴다
        path = state_path(str(data["session_id"]))
        try:
            state = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else None
        except (OSError, ValueError):
            state = None
        prompt, new_state = decide(stats, state if isinstance(state, dict) else None, turn_missed, asked)
        if not prompt:
            return 0
        from kb.remote import load_credentials
        cred = load_credentials()
        if cred and (remote_only or cred["mode"] != "local"):
            project = project_of(cwd, None, remote_projects(cred))
        elif remote_only:
            return 0
        else:
            from kb.config import load_config
            env = os.environ.get("FAST_QUESTION_CONFIG")
            project = project_of(cwd, load_config(Path(env) if env else None))
        if not project:
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(new_state), encoding="utf-8")
        uselog.record("nudge", [], cwd, tag="missed" if turn_missed else "asked" if asked else "investigation")
        text = context(project, stats["edited"], uselog.examples(cwd),
                       uselog.open_misses(cwd, grounded, min_days=MISSED_MIN_DAYS), turn_missed, asked)
        out = {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": text}}
        sys.stdout.write(json.dumps(out))
    except Exception:  # noqa: BLE001 - 훅 실패가 응답을 막지 않게 한다
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
