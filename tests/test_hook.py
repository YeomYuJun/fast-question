import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from hooks import ask_kb
from kb.config import load_config
from kb.db import connect
from kb.store import Store

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("prompt", [
    "NasFileStorage 가 .part 에 먼저 쓰는 이유는?",
    "k8s 에서 Pod 와 Deployment 차이가 뭐야",
    "왜 rename 은 같은 볼륨에서만 원자적인가",
    "ARIA 암호화 방식은 어떻게 되나요",
    "What is a PVC?",
])
def test_questions_are_detected(prompt):
    assert ask_kb.is_question(prompt)


@pytest.mark.parametrize("prompt", [
    "1,2,3번에 대해서 먼저 분석 및 제안만 해볼래?",
    "이거 테스트까지 해줘",
    "커밋해줄래?",
    "/qa rename",
    "디자인 반영 끝났으니 이제 착수해도 돼.",
    "아래 로그 봐줘\n```\nerror\n```\n왜 이래?",
    "가" * 301 + "?",
])
def test_requests_and_long_prompts_are_not_questions(prompt):
    assert not ask_kb.is_question(prompt)


def test_query_drops_question_words():
    assert ask_kb.to_query("NasFileStorage 가 여유 공간을 검사하는 이유는 뭐야?") == "NasFileStorage 가 여유 공간을 검사하는 이유는"
    assert ask_kb.to_query("왜 .part 를 쓰나요?") == ".part 를 쓰나요"


def _item(i, q="질문", a="답변", state="trusted"):
    return {"id": i, "category_path": "이관 > NAS", "question": q, "answer": a, "evidence_state": state}


def test_context_with_hits_lists_items_and_urls():
    text = ask_kb.context("q", {"hits": [_item(3, state="stale")], "similar": []}, "http://127.0.0.1:8765")
    assert "#3" in text and "http://127.0.0.1:8765/#qa/3" in text and "stale" in text
    assert "먼저" in text


def test_context_without_hits_asks_to_store():
    text = ask_kb.context("모르는 것", {"hits": [], "similar": [dict(_item(5), score=0.6)]}, "http://127.0.0.1:8765")
    assert "없다" in text and "rules.md" in text and "묻지 않는다" in text and "#5" in text


def _db_with_item(tmp_path):
    db = tmp_path / "kb.sqlite3"
    s = Store(connect(db), "user")
    s.add_category("이관")
    s.add_qa("이관", ".part 에 먼저 쓰는 이유", "다 쓴 뒤 rename 으로 확정한다.")
    s.conn.close()
    return db


def _run(tmp_path, cfg: dict, stdin: str):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return subprocess.run([sys.executable, str(ROOT / "hooks" / "ask_kb.py")], input=stdin.encode("utf-8"),
                          capture_output=True, timeout=60, env={"FAST_QUESTION_CONFIG": str(path), "SYSTEMROOT": "C:\\Windows"})


def test_hook_falls_back_to_db_when_web_is_down(tmp_path):
    db = _db_with_item(tmp_path)
    r = _run(tmp_path, {"db_path": str(db), "port": 1}, json.dumps({"prompt": ".part 에 먼저 쓰는 이유가 뭐야?"}))
    assert r.returncode == 0
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "UserPromptSubmit"
    assert "#1" in out["additionalContext"] and "rename" in out["additionalContext"]


def test_hook_is_silent_for_non_questions_and_bad_input(tmp_path):
    db = _db_with_item(tmp_path)
    for stdin in [json.dumps({"prompt": "커밋해줘"}), "not json", ""]:
        r = _run(tmp_path, {"db_path": str(db), "port": 1}, stdin)
        assert r.returncode == 0 and r.stdout == b""


def test_hook_uses_running_web_server(tmp_path):
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen["path"] = self.path
            body = json.dumps({"hits": [_item(9, "웹에서 온 질문")], "similar": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"db_path": str(tmp_path / "none.sqlite3"), "port": srv.server_address[1]}),
                        encoding="utf-8")
        res = ask_kb.lookup("웹 질문", load_config(path))
    finally:
        srv.shutdown()
    assert seen["path"].startswith("/api/search?q=")
    assert res["hits"][0]["id"] == 9
