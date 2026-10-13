"""fast-question 기본 사용 가이드 Q&A 를 DB 에 넣는다. 다시 실행하면 바뀐 답만 고친다.

사용: .venv\\Scripts\\python.exe scripts\\seed_guide.py [--config <config.json>]
분류 `fast-question` 아래에 넣고 주체는 system 이다. 근거는 이 저장소 파일이라 코드가 바뀌면 재검증이 stale 로 바꾼다.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kb.config import load_config  # noqa: E402
from kb.db import connect  # noqa: E402
from kb.errors import NotFoundError  # noqa: E402
from kb.store import Store  # noqa: E402

TOP = "fast-question"
TOP_DESCRIPTION = "fast-question 의 사용법·설정·운영 사실만 넣는다. 개발 이력, 결정 경위, 내부 구현은 넣지 않는다."


def _ev(rel: str, anchor: str) -> list[dict]:
    return [{"kind": "auto", "path": str(ROOT / rel), "anchor": anchor}]


GUIDE = [
    ("검색", "fast-question 검색창에서 쓸 수 있는 질의 문법은?",
     "공백으로 나눈 단어는 모두 든 항목만 찾는다(AND).\n"
     "- a | b: 둘 중 하나가 든 항목(OR)\n"
     "- -a: a 가 든 항목을 뺀다\n"
     "- \"a b\": 공백을 포함한 구절\n"
     "- q:a · a:a: 질문에서만 · 답변에서만 찾기\n"
     "- path:x: 근거 파일 경로에 x 가 든 항목",
     _ev("kb/query.py", "a | b, a|b")),
    ("검색", "fast-question 검색이 0건일 때 보이는 비슷한 결과의 기준은?",
     "정확히 일치·동의어·형태소 검색이 모두 0건일 때만 따로 보인다.\n"
     "- 검색어의 2글자 조각이 50% 이상 겹치는 항목\n"
     "- 또는 검색어의 형태소가 50% 이상 맞는 항목",
     _ev("kb/search.py", "def _similar")),
    ("검색", "fast-question 에서 정규식으로 찾으려면?",
     "화면 검색창에는 정규식이 없고, Claude 가 MCP kb_search 의 regex=true 로만 쓴다.\n"
     "- 200자 이하, (a+)+ 같은 중첩 반복은 거부",
     _ev("kb/search.py", "REGEX_MAX")),
    ("화면", "fast-question 화면의 단축키는?",
     "/ 로 검색창에 가고, ↑↓ 로 결과를 옮기며 펼친다.\n"
     "- Enter: 검색창에서 첫 결과 펼치기\n"
     "- E: 펼친 항목 편집\n"
     "- Esc: 펼친 항목 접기, 펼친 항목이 없으면 검색어·분류 필터 지우기\n"
     "- 편집 중 Ctrl+Enter: 저장, Esc: 취소",
     _ev("web/static/app.js", 'document.addEventListener("keydown"')),
    ("화면", "fast-question 에서 Q&A 를 삭제하면 어떻게 되나?",
     "검색 결과에서 빠지고, 행은 지우지 않아 되살릴 수 있다.\n"
     "- 삭제 직후 알림의 되돌리기(6초)로 복원\n"
     "- 그 뒤에는 Claude 에게 복원을 요청(MCP kb_restore)",
     _ev("kb/store.py", "def restore_qa")),
    ("화면", "fast-question 에서 분류를 삭제하면 어떻게 되나?",
     "그 분류의 하위 분류와 그 안의 Q&A 가 함께 삭제된다.\n"
     "- 화면은 확인 창에 함께 삭제될 하위 분류 수·지식 수를 보인다\n"
     "- 분류 자체를 되살리는 기능은 없음\n"
     "- 그 안의 Q&A 는 옮길 분류를 정해 kb_restore 로 복원",
     _ev("kb/store.py", "def archive_category")),
    ("화면", "fast-question 질문·답변의 글자 수와 줄 수 한도는?",
     "질문은 한 줄 150자, 답변은 400자·6줄까지다.\n"
     "- 분류 이름: 60자, > 는 쓸 수 없음\n"
     "- Claude 등록분: 답변 첫 줄은 1~2문장·120자, 둘째 줄부터 '- ' 항목",
     _ev("kb/text.py", "ANSWER_MAX_LINES")),
    ("Claude 연동", "fast-question 에 Claude 로 Q&A 를 등록하거나 찾으려면?",
     "Claude Code 에서 /qa 로 대화 내용을 등록하고, /qa <검색어> 로 찾는다.\n"
     "- \"Q&A 로 남겨\", \"qa 에서 찾아봐\" 같은 말로도 동작\n"
     "- Claude 는 MCP 서버 fast-question 의 kb_* 도구를 쓴다",
     _ev("skill/qa/SKILL.md", "## 등록")),
    ("Claude 연동", "fast-question 에 프로젝트 폴더의 Q&A 를 한 번에 구축하려면?",
     "Claude Code 에서 /qa-harvest <경로> 를 실행한다.\n"
     "- 한 번에 60건, 한 파일에서 5건까지 등록\n"
     "- 두 번째부터는 지난 구축 이후 바뀐 파일만 다룸\n"
     "- 대분류 이름은 대상 폴더 이름과 같게 지음",
     _ev("skill/qa-harvest/SKILL.md", "## 0. 범위")),
    ("Claude 연동", "fast-question 즉석 질문 훅은 언제 동작하나?",
     "Claude Code 에 질문 꼴의 입력(물음표·의문 어미·의문사, 300자·4줄 이하)을 넣으면 KB 를 찾아 결과를 그 턴에 넣는다.\n"
     "- 해줘·줄래 같은 일을 시키는 말은 질문으로 보지 않음\n"
     "- 맞는 Q&A 가 없으면 KB 에 있는 프로젝트 폴더에서 물었을 때만 등록 조건을 안내\n"
     "- 그 밖의 평시 질문은 등록하지 않음",
     _ev("hooks/ask_kb.py", "def project_of")),
    ("Claude 연동", "fast-question 동의어나 고유명사를 검색에 추가하려면?",
     "Claude 에게 요청해 MCP kb_dict 로 넣는다(화면에는 사전 관리가 없다).\n"
     "- 같은 뜻의 다른 표기: action=synonym_add\n"
     "- 형태소 분석이 잘못 자르는 고유명사: action=user_word_add (색인을 다시 만듦)",
     _ev("mcp_server/server.py", "synonym_add")),
    ("Claude 연동", "fast-question 의 Skill 이나 훅을 고친 뒤 설치본에 반영하려면?",
     r"저장소에서 .venv\Scripts\python.exe scripts\install.py 를 실행한다." "\n"
     "- Skill: ~/.claude/skills 로 복사\n"
     "- 훅: ~/.claude/settings.json 의 UserPromptSubmit 에 등록\n"
     "- 훅만 지우기: --remove-hook\n"
     "- MCP 서버는 Claude Code 를 다시 시작해야 새 코드로 뜸",
     _ev("scripts/install.py", "def main")),
    ("운영", "fast-question 웹 화면을 띄우려면?",
     r"저장소에서 .venv\Scripts\python.exe run_web.py 를 실행하고 http://127.0.0.1:8765 를 연다." "\n"
     "- 다른 DB: --config <config.json>\n"
     r"- 로그온 때 자동으로 띄우기: scripts\autostart.py --install",
     _ev("run_web.py", "--config")),
    ("운영", "fast-question 설정 파일 config.json 의 키는?",
     "저장소 루트의 config.json 이고, 상대 경로는 저장소 루트 기준이다.\n"
     "- db_path: DB 파일 (기본 data/kb.sqlite3)\n"
     "- host·port: 웹 주소 (기본 127.0.0.1, 8765)\n"
     "- backup_dir·backup_keep: 백업 폴더·보관 개수 (기본 data/backup, 14)\n"
     "- log_dir·log_keep_days: 로그 폴더·보관 일수 (기본 data/logs, 14)\n"
     "- recheck_interval_hours: 근거 재검증 주기 (기본 24)",
     _ev("kb/config.py", "DEFAULTS")),
    ("운영", "fast-question 백업은 언제 어디에 만들어지나?",
     "웹 서버가 마지막 백업 뒤 24시간이 지나면 data/backup/kb-YYYYMMDD.sqlite3 를 만든다.\n"
     "- 만든 뒤 quick_check 로 사본을 검사\n"
     "- 최신 backup_keep 개(기본 14)만 남김",
     _ev("kb/jobs.py", "def backup")),
    ("운영", "fast-question 근거 상태 stale 의 뜻은?",
     "Q&A 에 단 근거 파일이 기록한 뒤에 바뀌었다는 뜻이다.\n"
     "- 웹 서버가 recheck_interval_hours(기본 24)마다 근거를 다시 판정\n"
     "- Claude 가 조회 중 stale 항목을 만나면 근거를 다시 읽고 답을 고침",
     _ev("kb/jobs.py", "recheck")),
]


def _category(s: Store, path: str, description: str = "") -> None:
    try:
        s.resolve_category(path)
    except NotFoundError:
        parent, _, name = path.rpartition(" > ")
        s.add_category(name or path, parent or None, description)


def seed(s: Store) -> dict:
    _category(s, TOP, TOP_DESCRIPTION)
    added = updated = same = 0
    for sub, question, answer, evidence in GUIDE:
        path = f"{TOP} > {sub}"
        _category(s, path)
        row = s.conn.execute("SELECT id FROM qa WHERE question = ? AND deleted_at IS NULL", (question,)).fetchone()
        if row is None:
            s.add_qa(path, question, answer, evidence=evidence, force=True)
            added += 1
            continue
        cur = s.get_qa(row[0])
        if cur["answer"] == answer:
            same += 1
            continue
        s.update_qa(row[0], cur["version"], answer=answer, note="기본 가이드 갱신")
        updated += 1
    return {"added": added, "updated": updated, "same": same}


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="fast-question 기본 사용 가이드를 넣는다")
    ap.add_argument("--config", type=Path)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    conn = connect(cfg.db_path)
    try:
        print(seed(Store(conn, "system")))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
