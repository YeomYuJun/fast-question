import json
import subprocess
import sys
from pathlib import Path

from scripts import lint_draft

ROOT = Path(__file__).resolve().parent.parent


def _item(**kw):
    base = {"category": "fast-question > 검색", "question": "fast-question 검색창의 OR 문법은?",
            "answer": "a | b 로 쓰면 둘 중 하나가 든 항목을 찾는다.", "ask_when": "검색어를 여러 표현으로 찾을 때",
            "evidence": [{"kind": "auto", "path": "D:/repo/kb/query.py", "anchor": "OR"}],
            "quote": "a | b      OR(대안). 공백으로 둘러싼 | 만 연산자다"}
    return {**base, **kw}


def test_clean_item_passes():
    assert lint_draft.lint([_item()]) == []


def test_missing_fields_and_quote_mismatch_are_reported():
    (r,) = lint_draft.lint([_item(ask_when="", quote="def parse_query(text): return Query(...)")])
    msgs = " ".join(r["problems"])
    assert "ask_when" in msgs and "quote 의 식별자" in msgs


def test_similar_questions_and_per_file_limit():
    items = [_item(question=f"fast-question 검색창의 OR 문법 {i} 번째 질문은?") for i in range(6)]
    msgs = " ".join(p for r in lint_draft.lint(items) for p in r["problems"])
    assert "질문이 비슷하다" in msgs and "5건까지" in msgs


def test_rules_problems_are_included():
    (r,) = lint_draft.lint([_item(answer="2026-10-08 사용자 결정으로 바꿨다.")])
    assert any("결정 경위" in p for p in r["problems"])


def test_cli_exit_code_and_utf8_sig(tmp_path):
    p = tmp_path / "draft.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps([_item()], ensure_ascii=False).encode("utf-8"))
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "lint_draft.py"), str(p)], capture_output=True, timeout=60)
    assert r.returncode == 0 and json.loads(r.stdout.decode("utf-8"))["problems"] == []
    p.write_text(json.dumps([_item(ask_when="")], ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "lint_draft.py"), str(p)], capture_output=True, timeout=60)
    assert r.returncode == 1
