"""qa-harvest 초안(draft.json)을 적재 전에 검사한다. 문제가 0건이어야 kb_add_many 로 넣는다.

사용: fast-question lint-draft "<draft.json>" (PC 클라이언트)
      .venv\\Scripts\\python.exe fq_client\\lint_draft.py "<draft.json>" (저장소)
출력: JSON {"count": 항목 수, "problems": [{"index", "question", "problems": [...]}]}. 문제가 있으면 종료 코드 1.

검사
- 등록 경로와 같은 검사: 길이·줄 수(kb.text), 답변 양식·금지 표현·민감 정보(kb.rules)
- 초안 전용 칸: ask_when(이 질문을 다시 하게 되는 상황), 근거가 있으면 quote(원문 인용)
- quote 에 든 식별자가 답에 하나도 없으면 원문과 동떨어진 답으로 본다
- 초안 안에서 등록을 막는 수준으로 비슷한 질문(kb.text.similar_level 의 block), 한 파일에서 나온 항목 5건 초과
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
from kb.text import check_answer, check_question, similar_level  # noqa: E402

PER_FILE_MAX = 5
QUOTE_MAX = 300
_IDENT = re.compile(r"[A-Za-z_][\w.\-/]{2,}")


def _evidence_paths(item: dict) -> list[str]:
    return [str(e.get("path", "")).replace("\\", "/").lower() for e in item.get("evidence") or [] if e.get("path")]


def check_item(item: dict) -> list[str]:
    out = []
    for key in ("category", "question", "answer", "ask_when"):
        if not str(item.get(key) or "").strip():
            out.append(f"The {key} field is empty.")
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
        out.append("An item with evidence needs a quote: copy the source text the answer is based on.")
    if len(quote) > QUOTE_MAX:
        out.append(f"The quote can be up to {QUOTE_MAX} characters. It has {len(quote)} characters now.")
    idents = set(_IDENT.findall(quote))
    if idents and not any(t in a for t in idents):
        out.append("No identifier from the quote appears in the answer. Write the answer from what the source says.")
    return out


def lint(items: list[dict]) -> list[dict]:
    found = {i: check_item(it) for i, it in enumerate(items)}
    qs = [str(it.get("question") or "") for it in items]
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if similar_level(qs[i], qs[j])[1] == "block":
                found[j].append(f"The question is similar to item {i}. If they ask the same fact, keep one; "
                                "otherwise reword the question so the two are distinct.")
    per_file = Counter(p for it in items for p in set(_evidence_paths(it)))
    for path, n in per_file.items():
        if n > PER_FILE_MAX:
            last = max(i for i, it in enumerate(items) if path in _evidence_paths(it))
            found[last].append(f"{n} items come from {path}. Keep up to {PER_FILE_MAX} items per file.")
    return [{"index": i, "question": items[i].get("question", ""), "problems": p} for i, p in found.items() if p]


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) != 1:
        print(__doc__)
        return 2
    items = json.loads(Path(argv[0]).read_text(encoding="utf-8-sig"))
    if not isinstance(items, list):
        print(json.dumps({"error": "The draft must be an array of items."}, ensure_ascii=False))
        return 2
    result = lint(items)
    print(json.dumps({"count": len(items), "problems": result}, ensure_ascii=False, indent=1))
    return 1 if result else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
