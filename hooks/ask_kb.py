"""Claude Code UserPromptSubmit 훅. 사용자가 질문하면 fast-question KB 를 찾아 결과와 처리 지시를 그 턴에 넣는다.

- 결과가 있으면 그 Q&A 를 먼저 쓰라고 넣는다. 검색어의 대상 이름을 담지 않은 결과는 버린다(grounded).
- 없으면 비슷한 항목만 넣고, 그것도 없으면 아무것도 넣지 않는다. 등록 조건(store_conditions)은 이 훅이 넣지 않는다.
  질문한 폴더(입력의 cwd)가 KB 에 있는 프로젝트이고 맞는 Q&A 가 없었으면 세션 상태 폴더에 표시(mark_asked)만 둔다.
  조사 턴 수확 훅이 턴이 끝날 때 그 표시와 읽기 수를 보고 등록 조건을 넣는다. 의견을 묻는 질문처럼 읽지 않고 답한
  턴에는 등록 안내가 붙지 않는다.
검색은 실행 중인 web 서버(/api/search, 형태소 계층 포함)를 먼저 쓰고, 꺼져 있으면 DB 를 직접 읽는다
(Kiwi 를 띄우는 데 2.5초가 걸려 직접 읽을 때는 정확히 일치·동의어·비슷한 결과만 쓴다).
원격 자격 증명(kb/remote.py, %USERPROFILE%\\.fast-question\\credentials.json)이 있고 그 mode 가 local 이 아니면
원격 서버의 /api/search 를 개인 토큰으로 2초 제한으로 부르고, 프로젝트 판정 자료(/api/projects)는 하루에 한 번 받아 projects.json 에 둔다.
원격 검색이 실패하면 아무것도 넣지 않는다(로컬 DB 로 대신 찾지 않는다).
mode 가 local 이면(scripts/install.py --local) 자격 증명이 있어도 로컬 DB 를 쓴다.
어떤 실패든 아무것도 출력하지 않고 0 으로 끝나 사용자 입력을 막지 않는다.

설치: scripts/install.py 가 ~/.claude/settings.json 의 hooks.UserPromptSubmit 에 등록한다. 조사 턴 수확 훅은 hooks/stop_harvest.py 다.
편집기가 끼워 넣는 태그(<ide_opened_file> 등)는 strip_system_tags 로 벗긴 뒤 판정·검색한다.
설정 파일: 환경 변수 FAST_QUESTION_CONFIG, 없으면 저장소의 config.json.
PC 클라이언트 패키지(`fast-question hook`, fq_client/cli.py)는 main(remote_only=True) 로 원격 경로만 쓴다(mode 를 보지 않는다).
"""
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kb.config import Config, load_config  # noqa: E402

MAX_CHARS = 300
MAX_LINES = 4
SHOW = 3
WEB_TIMEOUT_S = 2.0
PROJECTS_MAX_AGE_S = 24 * 3600

_ASK_END = re.compile(
    r"(\?|？|까|까요|나요|니|냐|는지|인지|인가|인가요|는가|죠|지요|가요|뭐야|뭐지|뭔가|뭔데|뭐임|무엇인가)"
    r"[\s.!~]*$")
_ASK_START = re.compile(r"^(왜|어떻게|무엇|뭐가|뭘|언제|어디|누가|어느|what|why|how|when|where|which|who)\b",
                        re.IGNORECASE)
# 묻는 꼴이어도 일을 시키는 말(해줄래?, 볼래?, 해줘)은 지식 질문이 아니다
_REQUEST_END = re.compile(r"(줘|줘요|줄래|줄래요|주세요|주실래요|할래|볼래|래|래요|하자|해라|부탁해|부탁해요)[\s?？.!~]*$")
_QUESTION_WORDS = {"왜", "뭐야", "뭐지", "뭔가", "뭔데", "뭐임", "무엇", "무엇인가", "어떻게", "언제", "어디",
                   "어디서", "누가", "뭐", "무슨", "어느", "what", "why", "how", "when", "where", "which", "who"}


# 편집기·Claude Code 가 사용자 글 앞뒤에 끼워 넣는 태그. 사용자가 쓴 글이 아니라 질문 판정·검색어에서 뺀다.
# VS Code 는 열린 파일을 <ide_opened_file>, 선택 영역을 <ide_selection> 으로 넣고, 붙여 넣은 글은 <pasted_content id=".."> 줄로 감싼다
_SYSTEM_BLOCK = re.compile(r"<(ide_opened_file|ide_selection|system-reminder)\b[^>]*>.*?</\1>", re.DOTALL)
_PASTE_TAG = re.compile(r"</?pasted_content\b[^>]*>")


def strip_system_tags(prompt: str) -> str:
    return _PASTE_TAG.sub("", _SYSTEM_BLOCK.sub("", prompt)).strip()


def is_question(prompt: str) -> bool:
    p = strip_system_tags(prompt)
    if not p or p.startswith("/") or "```" in p:
        return False
    if len(p) > MAX_CHARS or p.count("\n") >= MAX_LINES:
        return False
    if _REQUEST_END.search(p):
        return False
    return bool(_ASK_END.search(p) or _ASK_START.search(p))


def to_query(prompt: str) -> str:
    """검색어. 물음표와 의문사를 뺀다. 의문사는 답 문장에 없어서 정확히 일치·형태소 AND 를 0건으로 만든다."""
    words = [w.rstrip("?？.!~") for w in strip_system_tags(prompt).split()]  # 앞쪽 점은 .part 같은 이름의 일부다
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


def _get_remote(cred: dict, path: str) -> dict:
    req = Request(cred["url"] + path, headers={"Authorization": f"Bearer {cred['token']}"})
    with urlopen(req, timeout=WEB_TIMEOUT_S) as r:  # noqa: S310 - 자격 증명 파일의 https 주소만 연다
        return json.loads(r.read().decode("utf-8"))


def remote_lookup(query: str, cred: dict) -> dict:
    return _get_remote(cred, "/api/search?" + urlencode({"q": query, "limit": SHOW}))


def remote_projects(cred: dict, cache: Path | None = None, now: float | None = None) -> dict:
    """대분류 이름·수확 경로. 하루 지난 캐시만 다시 받고, 받지 못하면 옛 캐시를 쓴다."""
    from kb.remote import home
    cache = cache or home() / "projects.json"
    now = time.time() if now is None else now
    try:
        old = json.loads(cache.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        old = None
    if old and old.get("url") == cred["url"] and now - old.get("fetched_at", 0) < PROJECTS_MAX_AGE_S:
        return old
    try:
        got = _get_remote(cred, "/api/projects")
    except Exception:  # noqa: BLE001 - 서버가 안 되면 옛 캐시(다른 서버 것이면 버림)
        return old if old and old.get("url") == cred["url"] else {"names": [], "roots": []}
    data = {"url": cred["url"], "fetched_at": now, "names": got.get("names", []), "roots": got.get("roots", [])}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return data


def project_of(cwd: str, cfg: Config | None, sources: dict | None = None) -> str | None:
    """질문한 폴더(cwd)가 KB 에 있는 프로젝트면 그 이름을 돌려준다. 아니면 None.

    폴더 경로의 어느 단계 이름이 대분류 이름과 같거나, cwd 가 프로젝트 폴더(roots: qa-harvest 로 구축한 경로와
    Q&A 의 파일 근거가 놓인 폴더, kb/store.py project_sources) 안이거나, 프로젝트 폴더가 cwd 안에 있으면 프로젝트다.
    마지막 경우는 여러 저장소를 담은 작업 폴더에서 세션을 연 때다. cwd 가 드라이브·파일시스템 루트이거나 사용자 홈
    폴더(또는 그 위)면 그 안에 무엇이 있든 프로젝트로 보지 않는다.
    sources({"names", "roots"})가 없으면 로컬 DB(cfg.db_path)에서 읽는다.
    """
    if not cwd:
        return None
    if sources is None:
        if not cfg.db_path.exists():
            return None
        from kb.db import connect
        from kb.store import Store
        conn = connect(cfg.db_path)
        try:
            sources = Store(conn, "system").project_sources()
        finally:
            conn.close()
    names, roots = sources.get("names", []), sources.get("roots", [])
    norm = cwd.replace("\\", "/").rstrip("/").lower()
    parts = set(norm.split("/"))
    for name in names:
        if name.lower() in parts:
            return name
    for root in roots:
        if norm == root or norm.startswith(root + "/"):
            return root
    if _holds_projects(norm) and any(root.startswith(norm + "/") for root in roots):
        return norm
    return None


def _holds_projects(norm: str) -> bool:
    """정규화한 cwd 가 안에 든 프로젝트 폴더로 판정해도 되는 폴더인가. 루트와 홈 폴더(그 위 포함)는 아니다."""
    if "/" not in norm.strip("/"):  # "d:", "", "/home" 같은 한 단계 경로
        return False
    try:
        home = str(Path.home()).replace("\\", "/").rstrip("/").lower()
    except RuntimeError:
        return False
    return norm != home and not home.startswith(norm + "/")


def _line(h: dict, base: str) -> str:
    score = f", 비슷함 {h['score']}" if "score" in h else ""
    return (f"- #{h['id']} [{h.get('category_path', '')}] Q: {h['question']} / A: {h['answer']} "
            f"(근거 {h.get('evidence_state', 'none')}{score}, {base}/#qa/{h['id']})")


# 검색어에서 뽑는 낱말: 영문·숫자로 시작하는 식별자(RateLimitFilter, paas.storage.root, 401)와 한글 2글자 이상
_WORD = re.compile(r"[0-9A-Za-z][0-9A-Za-z._:/\\-]*|[가-힣]{2,}")
ASCII_MIN = 3  # 영문 낱말은 이 길이부터 대상 이름으로 본다. 숫자가 든 낱말(401, v2)은 2글자부터
HANGUL_MIN_WORDS = 2  # 한글 낱말만으로 맞았다고 보려면 이만큼의 낱말이 항목에 그대로 있어야 한다
HANGUL_MIN_CHARS = 5  # 그리고 맞은 글자 수의 합이 이 이상이어야 한다
JOSA_MAX = 2  # 한글 낱말 끝에서 떼어 보는 조사·어미 글자 수


def _has(word: str, text: str) -> int:
    """한글 낱말이 text 에 그대로 있으면 맞은 글자 수, 없으면 0. 끝의 조사·어미 1~2글자를 떼어도 본다(대기열은 → 대기열)."""
    return next((len(word) - cut for cut in range(JOSA_MAX + 1)
                 if len(word) - cut >= 2 and word[:len(word) - cut] in text), 0)


def grounded(query: str, item: dict) -> bool:
    """검색 결과 항목이 검색어의 대상 이름을 실제로 담고 있는가.

    서버의 형태소 계층은 "그럼 해야 돼" 같은 대화 문장을 하다·되다 로 풀어 무관한 항목과 맞춘다(2026-11-24 실측 3건).
    그래서 검색어의 영문·숫자 낱말 하나, 또는 한글 낱말 HANGUL_MIN_WORDS 개(맞은 글자 합 HANGUL_MIN_CHARS 이상)가
    항목의 질문·답변에 그대로 있을 때만 맞는 항목으로 본다. 검색어가 한글 3글자 이상 낱말 하나뿐이면 그 낱말 하나로 본다.
    """
    text = f"{item.get('question', '')} {item.get('answer', '')}".lower()
    words = list(dict.fromkeys(w.lower() for w in _WORD.findall(query)))
    hangul = [w for w in words if "가" <= w[0] <= "힣"]
    for w in words:
        if w in hangul:
            continue
        w = w.rstrip("._:/\\-")
        if (len(w) >= ASCII_MIN or (len(w) >= 2 and any(ch.isdigit() for ch in w))) and w in text:
            return True
    found = [n for n in (_has(w, text) for w in hangul) if n]
    if len(words) == 1 and hangul and len(hangul[0]) >= 3:
        return bool(found)
    # 2글자 낱말 둘(남는·없는)은 무관한 항목에도 흔히 있다. 맞은 글자를 합쳐 HANGUL_MIN_CHARS 이상이어야 한다
    return len(found) >= HANGUL_MIN_WORDS and sum(found) >= HANGUL_MIN_CHARS


# 등록 보고 양식. "모르면" 구절을 쓰게 해서 다시 알아내는 비용이 없는 건을 등록 전에 거르게 한다
REPORT = ("등록했을 때만 응답 끝에 '등록: <질문> <주소> (모르면: <다시 치르는 것>)' 한 줄씩 붙인다. "
          "'모르면' 구절을 쓸 수 없는 건은 등록하지 않는다.")


def store_conditions(project: str, used: list[tuple[str, int]] | None = None,
                     missed: list[tuple[str, int]] | None = None) -> list[str]:
    """등록 조건 안내. 조사 턴 수확 훅(hooks/stop_harvest.py)이 쓴다. 조건의 원본은 skill/qa/rules.md "등록할 가치" 다.
    used 는 이 폴더에서 여러 날 다시 찾아진 질문과 날 수(kb/uselog.py examples)다. 있으면 판단의 예로 덧붙인다.
    missed 는 이 폴더에서 여러 날 찾았으나 없었던 검색어와 날 수(kb/uselog.py open_misses)다. 실제로 필요했던 것의 기록이다."""
    lines = [
        "아래를 모두 만족하는 사실만 qa Skill 등록 절차와 rules.md 양식으로 등록한다"
        "(kb_search 로 중복 확인 → kb_add, 읽은 파일은 auto 근거, 사용자 말·명령 결과는 note 근거).",
        f"(1) {project} 의 코드·설정·절차·동작에 대한 사실이고 이 세션에서 읽은 파일·명령 결과나 사용자의 말로 확인했다 "
        "(2) 다음에 이 프로젝트에서 일할 때 모르면 다시 치를 비용이 있다(예: 여러 파일을 이어 읽어야 했다, 읽기 전 예상과 "
        "달랐다, 오류·증상의 원인이다, 사람에게 확인했다, 쓰지 않기로 했거나 해 보고 안 된 방법과 그 이유다). 파일 하나를 "
        "열거나 이름으로 검색하면 나오는 것(문서·파일의 위치와 목차, 한 파일에 적힌 값)은 비용이 없다 "
        "(3) 이 세션에서 만들거나 고친 파일의 내용이 아니다 "
        "(4) 작업이 끝난 뒤에도 그대로인 사실이다. 논의 중인 안, 결정 전인 것, 진행 상태가 아니다.",
        "일반 지식(언어·도구 사용법, 개념), 작업 지시, 이 대화에만 해당하는 것은 등록하지 않는다.",
    ]
    if used:
        shown = ", ".join(f"'{q}'({days}일)" for q, days in used)
        lines.append(f"이 폴더에서 등록 뒤 여러 날 다시 찾아진 항목(이 PC 기록): {shown}. "
                     "다시 필요해지는 사실이 어떤 것인지 가늠하는 예로만 쓰고, 같은 문형을 따라 쓰지 않는다.")
    if missed:
        lines.append(f"이 폴더에서 여러 날 kb_search 로 찾았으나 없었던 검색어(이 PC 기록): {show_terms(missed)}. "
                     "이 세션에서 그 답을 확인했으면 조건 (2)는 채워진 것이다. 나머지 조건을 보고 등록한다.")
    return lines


def show_terms(missed: list[tuple[str, int]]) -> str:
    """검색어 목록을 안내문에 적는 꼴로. 이틀 이상 놓친 것에만 날 수를 붙인다."""
    return ", ".join(f"'{q}'({days}일)" if days > 1 else f"'{q}'" for q, days in missed)


MISSED_MIN_DAYS = 2  # 등록 조건에 붙이는 "여러 날 찾았으나 없었던 검색어" 의 날 수 하한. 하루뿐인 것은 작업 시작 때 넣어 본 이름일 수 있다


def pick(query: str, res: dict) -> tuple[list[dict], list[dict]]:
    """검색 결과에서 검색어의 대상 이름을 담은 것만 남긴 (맞는 항목, 비슷한 항목). 각각 SHOW 건까지."""
    return ([h for h in res.get("hits", []) if grounded(query, h)][:SHOW],
            [h for h in res.get("similar", []) if grounded(query, h)][:SHOW])


def context(query: str, res: dict, base: str) -> str:
    """Q&A 가 있으면 먼저 쓰라고 넣는다. 비슷한 항목만 있으면 그것을 넣는다. 둘 다 없으면 아무것도 넣지 않는다.
    검색어의 대상 이름을 담지 않은 결과(grounded)는 맞는 항목·비슷한 항목 어느 쪽으로도 넣지 않는다.
    등록 조건은 여기서 넣지 않는다. 질문만 보고는 답하느라 조사가 필요한지 알 수 없어서, 조사 턴 수확 훅이 턴이 끝난 뒤에
    읽기 수를 보고 넣는다(mark_asked)."""
    hits, similar = pick(query, res)
    if hits:
        return "\n".join([
            "[fast-question 지식 DB] 이 질문과 맞는 Q&A 가 있다. 답할 때 이 내용을 먼저 쓰고 항목 주소를 붙인다.",
            "근거가 stale 인 항목은 qa Skill 조회 절차로 근거를 다시 읽고 정리한다.",
            *[_line(h, base) for h in hits]])
    if similar:
        return "\n".join(["[fast-question 지식 DB] 비슷한 항목이다. 같은 사실을 묻는 것이면 그 항목을 쓴다.",
                          *[_line(h, base) for h in similar]])
    return ""


def session_file(session_id: str, suffix: str, home: Path | None = None) -> Path:
    """세션마다 두는 훅 상태 파일. %USERPROFILE%\\.fast-question\\stop\\<session_id><suffix>."""
    from kb.remote import home as cred_home
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")[:80] or "session"
    return (home or cred_home()) / "stop" / f"{safe}{suffix}"


ASKED = ".asked"  # 이 턴이 KB 에 답이 없는 질문으로 시작했다는 표시. 조사 턴 수확 훅이 읽고 지운다


def mark_asked(session_id: str, asked: bool) -> None:
    """표시를 두거나 지운다. 표시에는 내용이 없다(사용자의 질문을 적지 않는다). 실패는 버린다."""
    if not session_id:
        return
    try:
        path = session_file(session_id, ASKED)
        if asked:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    except (OSError, RuntimeError):
        return


def main(remote_only: bool = False) -> int:
    """remote_only 는 PC 클라이언트(fq_client hook)다. 자격 증명이 없으면 로컬 설정·DB 를 읽지 않고 끝낸다.
    저장소 훅은 자격 증명의 mode 가 local 이면 로컬 DB 를 쓴다."""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig")
        data = json.loads(raw) if raw.strip() else {}
        prompt = data.get("prompt", "")
        session_id = str(data.get("session_id") or "")
        mark_asked(session_id, False)  # 앞 턴의 표시가 남아 있으면 지운다(그 턴이 중단되어 수확 훅이 돌지 않은 경우)
        if not isinstance(prompt, str) or not is_question(prompt):
            return 0
        query = to_query(prompt)
        if not query:
            return 0
        from kb.remote import load_credentials
        from kb import uselog
        cred = load_credentials()
        cwd = str(data.get("cwd", ""))
        if cred and (remote_only or cred["mode"] != "local"):
            res, base = remote_lookup(query, cred), cred["url"]
            project = project_of(cwd, None, remote_projects(cred))
        elif remote_only:
            return 0
        else:
            env = os.environ.get("FAST_QUESTION_CONFIG")
            cfg = load_config(Path(env) if env else None)
            res, base, project = lookup(query, cfg), f"http://{cfg.host}:{cfg.port}", project_of(cwd, cfg)
        hits, _ = pick(query, res)
        uselog.record("hook_hit", [(h["id"], h["question"]) for h in hits], cwd)
        mark_asked(session_id, bool(project and not hits))
        text = context(query, res, base)
        if not text:
            return 0
        out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}
        sys.stdout.write(json.dumps(out))  # ensure_ascii 기본값: 출력이 ASCII 라 콘솔 인코딩과 무관하다
    except Exception:  # noqa: BLE001 - 훅 실패가 사용자 입력을 막지 않게 한다
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
