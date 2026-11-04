"""Claude Code Stop 훅(세션 끝 수확). Claude 가 응답을 마칠 때 이 세션에서 파일을 많이 읽었는데 KB 에 등록한 것이 없으면,
확인한 사실을 qa Skill 규칙으로 등록하라는 지시를 additionalContext 로 넣어 한 번 더 이어 가게 한다.

- 입력(stdin JSON): session_id, transcript_path, cwd, stop_hook_active. stop_hook_active 가 true 면(이 훅 때문에 이어 간
  응답이다) 아무것도 하지 않는다.
- 전사(transcript_path, jsonl)에서 assistant 의 tool_use 블록을 세어 읽기 도구(READ_TOOLS) 수와 KB 쓰기 도구(kb_add·
  kb_update·kb_add_many) 수를 본다. 전사 형식은 Claude Code 문서에 없어 한 줄에 {"type": "assistant", "message":
  {"content": [{"type": "tool_use", "name": ...}]}} 꼴만 본다. 형식이 다르면 세지 못해 아무것도 넣지 않는다.
- 질문한 폴더(cwd)가 KB 에 있는 프로젝트(hooks/ask_kb.py project_of 와 같은 기준)일 때만 넣는다.
- 세션마다 상태 파일(%USERPROFILE%\\.fast-question\\stop\\<session_id>.json)에 마지막으로 지시를 넣었을 때의 읽기·쓰기 수를
  둔다. 처음은 읽기 FIRST_MIN 이상, 그 뒤로는 읽기가 NEXT_STEP 만큼 더 늘었을 때만 다시 넣는다. 마지막 지시 뒤 KB 쓰기가
  있었으면 그 응답에서는 넣지 않고 기준점만 옮긴다(이미 등록했다).
- 어떤 실패든 아무것도 출력하지 않고 0 으로 끝난다.

설치: scripts/install.py 가 ~/.claude/settings.json 의 hooks.Stop 에, 플러그인은 hooks/hooks.json 의 Stop 에
`fast-question stop-hook` 으로 등록한다.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hooks.ask_kb import project_of, remote_projects  # noqa: E402

READ_TOOLS = ("Read", "Grep", "Glob", "Bash", "NotebookRead")
WRITE_TOOLS = ("kb_add", "kb_update", "kb_add_many")
FIRST_MIN = 4  # 처음 지시를 넣는 읽기 수
NEXT_STEP = 12  # 그 뒤 다시 넣기까지 늘어나야 하는 읽기 수
MAX_TRANSCRIPT_BYTES = 200 * 1024 * 1024


def scan(transcript: Path) -> dict:
    """전사에서 읽기 도구 호출 수와 KB 쓰기 도구 호출 수."""
    reads = writes = 0
    if not transcript.is_file() or transcript.stat().st_size > MAX_TRANSCRIPT_BYTES:
        return {"reads": 0, "writes": 0}
    with transcript.open("rb") as f:
        for raw in f:
            if b'"tool_use"' not in raw:
                continue
            try:
                row = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("type") != "assistant":
                continue
            content = (row.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name", ""))
                if name in READ_TOOLS:
                    reads += 1
                elif any(name.endswith(w) for w in WRITE_TOOLS):
                    writes += 1
    return {"reads": reads, "writes": writes}


def decide(stats: dict, state: dict | None) -> tuple[bool, dict | None]:
    """(지시를 넣는가, 새 상태). 상태를 바꾸지 않으면 None."""
    reads, writes = stats["reads"], stats["writes"]
    if state is None:
        return (reads >= FIRST_MIN, {"reads": reads, "writes": writes} if reads >= FIRST_MIN else None)
    if writes > state.get("writes", 0):  # 마지막 지시 뒤에 등록했다. 기준점만 옮긴다
        return False, {"reads": reads, "writes": writes}
    if reads - state.get("reads", 0) >= NEXT_STEP:
        return True, {"reads": reads, "writes": writes}
    return False, None


def context(project: str) -> str:
    return "\n".join([
        f"[fast-question 지식 DB] 이 세션에서 {project} 의 파일·명령 결과로 확인한 사실 가운데 같은 프로젝트에서 다시 물을 만한 "
        "것이 있으면 지금 qa Skill 등록 절차와 rules.md 양식으로 등록한다(kb_search 로 중복 확인 → kb_add, 읽은 파일은 auto 근거).",
        "결정 경위, 진행 상태, 이 대화에만 해당하는 것, 확인하지 않은 추정은 넣지 않는다. 넣을 것이 없으면 아무 말 없이 끝낸다.",
        "등록했을 때만 '등록: <질문> <주소>' 한 줄씩 보고한다.",
    ])


def state_path(session_id: str, home: Path | None = None) -> Path:
    from kb.remote import home as cred_home
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")[:80] or "session"
    return (home or cred_home()) / "stop" / f"{safe}.json"


def main(remote_only: bool = False) -> int:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig")
        data = json.loads(raw) if raw.strip() else {}
        if data.get("stop_hook_active") or not data.get("transcript_path") or not data.get("session_id"):
            return 0
        stats = scan(Path(str(data["transcript_path"])))
        if stats["reads"] < FIRST_MIN:
            return 0
        path = state_path(str(data["session_id"]))
        try:
            state = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else None
        except (OSError, ValueError):
            state = None
        prompt, new_state = decide(stats, state)
        if new_state is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(new_state), encoding="utf-8")
        if not prompt:
            return 0
        from kb.remote import load_credentials
        cred = load_credentials()
        cwd = str(data.get("cwd", ""))
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
        out = {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": context(project)}}
        sys.stdout.write(json.dumps(out))
    except Exception:  # noqa: BLE001 - 훅 실패가 응답을 막지 않게 한다
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
