"""Claude Code UserPromptSubmit 훅. 사용자가 질문하면 fast-question KB 를 찾아 결과와 처리 지시를 그 턴에 넣는다.

- 결과가 있으면 그 Q&A 를 먼저 쓰라고 넣는다.
- 없으면 질문한 폴더(입력의 cwd)가 KB 에 있는 프로젝트일 때만 등록 조건을 넣는다. 그 밖의 평시 질문에는
  비슷한 항목이 없으면 아무것도 넣지 않는다.
검색은 실행 중인 web 서버(/api/search, 형태소 계층 포함)를 먼저 쓰고, 꺼져 있으면 DB 를 직접 읽는다
(Kiwi 를 띄우는 데 2.5초가 걸려 직접 읽을 때는 정확히 일치·동의어·비슷한 결과만 쓴다).
어떤 실패든 아무것도 출력하지 않고 0 으로 끝나 사용자 입력을 막지 않는다.

설치: scripts/install.py 가 ~/.claude/settings.json 의 hooks.UserPromptSubmit 에 등록한다.
설정 파일: 환경 변수 FAST_QUESTION_CONFIG, 없으면 저장소의 config.json.
"""
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kb.config import Config, load_config  # noqa: E402

MAX_CHARS = 300
MAX_LINES = 4
SHOW = 3
WEB_TIMEOUT_S = 2.0

_ASK_END = re.compile(
    r"(\?|？|까|까요|나요|니|냐|는지|인지|인가|인가요|는가|죠|지요|가요|뭐야|뭐지|뭔가|뭔데|뭐임|무엇인가)"
    r"[\s.!~]*$")
_ASK_START = re.compile(r"^(왜|어떻게|무엇|뭐가|뭘|언제|어디|누가|어느|what|why|how|when|where|which|who)\b",
                        re.IGNORECASE)
# 묻는 꼴이어도 일을 시키는 말(해줄래?, 볼래?, 해줘)은 지식 질문이 아니다
_REQUEST_END = re.compile(r"(줘|줘요|줄래|줄래요|주세요|주실래요|할래|볼래|래|래요|하자|해라|부탁해|부탁해요)[\s?？.!~]*$")
_QUESTION_WORDS = {"왜", "뭐야", "뭐지", "뭔가", "뭔데", "뭐임", "무엇", "무엇인가", "어떻게", "언제", "어디",
                   "어디서", "누가", "뭐", "무슨", "어느", "what", "why", "how", "when", "where", "which", "who"}


def is_question(prompt: str) -> bool:
    p = prompt.strip()
    if not p or p.startswith("/") or "```" in p:
        return False
    if len(p) > MAX_CHARS or p.count("\n") >= MAX_LINES:
        return False
    if _REQUEST_END.search(p):
        return False
    return bool(_ASK_END.search(p) or _ASK_START.search(p))


def to_query(prompt: str) -> str:
    """검색어. 물음표와 의문사를 뺀다. 의문사는 답 문장에 없어서 정확히 일치·형태소 AND 를 0건으로 만든다."""
    words = [w.rstrip("?？.!~") for w in prompt.split()]  # 앞쪽 점은 .part 같은 이름의 일부다
    return " ".join(w for w in words if w and w.lower() not in _QUESTION_WORDS)


def _from_web(query: str, cfg: Config) -> dict:
    url = f"http://{cfg.host}:{cfg.port}/api/search?" + urlencode({"q": query, "limit": SHOW})
    with urlopen(url, timeout=WEB_TIMEOUT_S) as r:  # noqa: S310 - 설정의 로컬 주소만 연다
        return json.loads(r.read().decode("utf-8"))


def _from_db(query: str, cfg: Config) -> dict:
    from kb.db import connect
    from kb.store import Store
    if not cfg.db_path.exists():
        return {"hits": [], "similar": []}
    conn = connect(cfg.db_path)
    try:
        return Store(conn, "system").search(query, limit=SHOW)
    finally:
        conn.close()


def lookup(query: str, cfg: Config) -> dict:
    try:
        return _from_web(query, cfg)
    except Exception:  # noqa: BLE001 - web 이 꺼져 있거나 응답이 이상하면 DB 를 직접 읽는다
        return _from_db(query, cfg)


def project_of(cwd: str, cfg: Config) -> str | None:
    """질문한 폴더(cwd)가 KB 에 있는 프로젝트면 그 이름을 돌려준다. 아니면 None.

    폴더 경로의 어느 단계 이름이 대분류 이름과 같거나, cwd 가 qa-harvest 로 구축한 경로 안이면 프로젝트다.
    """
    if not cwd or not cfg.db_path.exists():
        return None
    from kb.db import connect
    norm = cwd.replace("\\", "/").rstrip("/").lower()
    parts = set(norm.split("/"))
    conn = connect(cfg.db_path)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM category WHERE parent_id IS NULL AND deleted_at IS NULL ORDER BY name")]
        roots = [r[0][len("harvest:"):] for r in conn.execute("SELECT key FROM meta WHERE key LIKE 'harvest:%'")]
    finally:
        conn.close()
    for name in names:
        if name.lower() in parts:
            return name
    for root in roots:
        if norm == root or norm.startswith(root + "/"):
            return root
    return None


def _line(h: dict, base: str) -> str:
    score = f", 비슷함 {h['score']}" if "score" in h else ""
    return (f"- #{h['id']} [{h.get('category_path', '')}] Q: {h['question']} / A: {h['answer']} "
            f"(근거 {h.get('evidence_state', 'none')}{score}, {base}/#qa/{h['id']})")


def context(query: str, res: dict, base: str, project: str | None = None) -> str:
    """Q&A 가 있으면 먼저 쓰라고 넣는다. 없으면 등록하지 않는 것이 기본이다.
    등록 조건은 질문한 폴더가 KB 에 있는 프로젝트일 때만 넣는다. 그 밖의 평시 질문에는 아무것도 넣지 않는다."""
    hits, similar = res.get("hits", [])[:SHOW], res.get("similar", [])[:SHOW]
    if hits:
        return "\n".join([
            "[fast-question 지식 DB] 이 질문과 맞는 Q&A 가 있다. 답할 때 이 내용을 먼저 쓰고 항목 주소를 붙인다.",
            "근거가 stale 인 항목은 qa Skill 조회 절차로 근거를 다시 읽고 정리한다.",
            *[_line(h, base) for h in hits]])
    lines = []
    if project:
        lines += [
            f"[fast-question 지식 DB] 이 질문의 Q&A 가 없다(검색어: {query}). 등록하지 않는 것이 기본이다.",
            f"다음을 모두 만족할 때만 답한 뒤 qa Skill 등록 절차와 rules.md 로 저장한다: (1) 질문이 {project} 의 "
            "코드·설정·절차·동작에 대한 것이다 (2) 답을 이 세션에서 읽은 그 프로젝트의 파일·명령 결과로 확인했다 "
            "(3) 같은 프로젝트에서 다시 물을 만한 사실이다.",
            "일반 지식(언어·도구 사용법, 개념), 평시 질문, 작업 지시, 이 대화에만 해당하는 질문은 저장하지 않는다. "
            "저장했을 때만 응답 끝에 '등록: <질문> <주소>' 한 줄을 붙인다.",
        ]
    if similar:
        lines.append("[fast-question 지식 DB] 비슷한 항목이다. 같은 사실을 묻는 것이면 그 항목을 쓴다.")
        lines += [_line(h, base) for h in similar]
    return "\n".join(lines)


def main() -> int:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig")
        data = json.loads(raw) if raw.strip() else {}
        prompt = data.get("prompt", "")
        if not isinstance(prompt, str) or not is_question(prompt):
            return 0
        query = to_query(prompt)
        if not query:
            return 0
        env = os.environ.get("FAST_QUESTION_CONFIG")
        cfg = load_config(Path(env) if env else None)
        text = context(query, lookup(query, cfg), f"http://{cfg.host}:{cfg.port}", project_of(data.get("cwd", ""), cfg))
        if not text:
            return 0
        out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}
        sys.stdout.write(json.dumps(out))  # ensure_ascii 기본값: 출력이 ASCII 라 콘솔 인코딩과 무관하다
    except Exception:  # noqa: BLE001 - 훅 실패가 사용자 입력을 막지 않게 한다
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
