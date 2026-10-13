"""qa-harvest 초안(draft.json)을 적재 전에 검사한다. 문제가 0건이어야 kb_add_many 로 넣는다.

사용: .venv\\Scripts\\python.exe scripts\\lint_draft.py "<draft.json>"
출력: JSON {"count": 항목 수, "problems": [{"index", "question", "problems": [...]}]}. 문제가 있으면 종료 코드 1.

검사
- 등록 경로와 같은 검사: 길이·줄 수(kb.text), 답변 양식·금지 표현·민감 정보(kb.rules)
- 초안 전용 칸: ask_when(이 질문을 다시 하게 되는 상황), 근거가 있으면 quote(원문 인용)
- quote 에 든 식별자가 답에 하나도 없으면 원문과 동떨어진 답으로 본다
- 초안 안에서 질문끼리 비슷한 것(2글자 조각 겹침 0.6 이상), 한 파일에서 나온 항목 5건 초과
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kb.errors import ValidationError  # noqa: E402
from kb.rules import problems as rule_problems  # noqa: E402
from kb.store import SIMILAR_MIN, bigrams, dice  # noqa: E402
from kb.text import check_answer, check_question  # noqa: E402

PER_FILE_MAX = 5
QUOTE_MAX = 300
_IDENT = re.compile(r"[A-Za-z_][\w.\-/]{2,}")


def _evidence_paths(item: dict) -> list[str]:
    return [str(e.get("path", "")).replace("\\", "/").lower() for e in item.get("evidence") or [] if e.get("path")]


def check_item(item: dict) -> list[str]:
    out = []
    for key in ("category", "question", "answer", "ask_when"):
        if not str(item.get(key) or "").strip():
            out.append(f"{key} 칸이 비어 있다.")
    q, a = str(item.get("question") or ""), str(item.get("answer") or "")
    for check, text in ((check_question, q), (check_answer, a)):
        if text.strip():
            try:
                check(text)
            except ValidationError as e:
                out.append(e.message)
    out += rule_problems(q or None, a or None, item.get("evidence"))
    quote = str(item.get("quote") or "")
    if item.get("evidence") and not quote.strip():
        out.append("근거가 있으면 quote 에 답의 근거가 된 원문을 그대로 옮긴다.")
    if len(quote) > QUOTE_MAX:
        out.append(f"quote 는 {QUOTE_MAX}자까지 쓴다. 지금 {len(quote)}자다.")
    idents = set(_IDENT.findall(quote))
    if idents and not any(t in a for t in idents):
        out.append("quote 의 식별자가 답에 하나도 없다. 원문에서 확인한 내용으로 답을 쓴다.")
    return out


def lint(items: list[dict]) -> list[dict]:
    found = {i: check_item(it) for i, it in enumerate(items)}
    grams = [bigrams(str(it.get("question") or "")) for it in items]
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if dice(grams[i], grams[j]) >= SIMILAR_MIN:
                found[j].append(f"{i}번 항목과 질문이 비슷하다. 같은 사실이면 하나만 남기고, 다르면 질문을 구별되게 고친다.")
    per_file = Counter(p for it in items for p in set(_evidence_paths(it)))
    for path, n in per_file.items():
        if n > PER_FILE_MAX:
            last = max(i for i, it in enumerate(items) if path in _evidence_paths(it))
            found[last].append(f"{path} 에서 나온 항목이 {n}건이다. 한 파일에서 {PER_FILE_MAX}건까지 쓴다.")
    return [{"index": i, "question": items[i].get("question", ""), "problems": p} for i, p in found.items() if p]


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) != 1:
        print(__doc__)
        return 2
    items = json.loads(Path(argv[0]).read_text(encoding="utf-8-sig"))
    if not isinstance(items, list):
        print(json.dumps({"error": "초안은 항목 배열이어야 한다."}, ensure_ascii=False))
        return 2
    result = lint(items)
    print(json.dumps({"count": len(items), "problems": result}, ensure_ascii=False, indent=1))
    return 1 if result else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
