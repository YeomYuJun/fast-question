import io
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


def test_context_without_hits_in_project_gives_store_conditions():
    text = ask_kb.context("모르는 것", {"hits": [], "similar": [dict(_item(5), score=0.6)]}, "http://127.0.0.1:8765",
                          "이관")
    assert "없다" in text and "기본" in text and "이관" in text and "rules.md" in text and "#5" in text


def test_context_without_hits_outside_project_never_asks_to_store():
    res = {"hits": [], "similar": [dict(_item(5), score=0.6)]}
    text = ask_kb.context("모르는 것", res, "http://127.0.0.1:8765", None)
    assert "#5" in text and "저장" not in text and "등록" not in text
    assert ask_kb.context("모르는 것", {"hits": [], "similar": []}, "http://127.0.0.1:8765", None) == ""


def test_project_of_matches_top_category_name_or_harvest_root(tmp_path):
    db = _db_with_item(tmp_path)
    s = Store(connect(db), "claude")
    s.add_category("fast-question")
    s.harvest_set(r"D:\work", {"created": 1})
    s.conn.close()
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"db_path": str(db)}), encoding="utf-8")
    cfg = load_config(path)
    assert ask_kb.project_of(r"D:\fast-question\kb", cfg) == "fast-question"
    assert ask_kb.project_of(r"d:\work\java", cfg) == "d:/work"
    assert ask_kb.project_of(r"C:\Users\someone", cfg) is None
    assert ask_kb.project_of("", cfg) is None


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
    for stdin in [json.dumps({"prompt": "커밋해줘"}), "not json", "",
                  json.dumps({"prompt": "파이썬 GIL 이 뭐야?", "cwd": r"C:\Users\someone"})]:
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


class FakeRemote:
    """원격 서버 흉내: /api/search, /api/projects. 받은 요청의 경로와 Authorization 을 남긴다."""

    def __init__(self, hits=None, names=None):
        self.seen: list[tuple[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.seen.append((self.path, self.headers.get("Authorization", "")))
                if self.path.startswith("/api/projects"):
                    body = {"names": names or [], "roots": []}
                else:
                    body = {"hits": hits or [], "similar": []}
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def _cred_file(tmp_path, url):
    p = tmp_path / "home" / ".fast-question" / "credentials.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"url": url, "token": "fq_pat_test"}), encoding="utf-8")
    return p


def test_hook_uses_remote_server_with_token(tmp_path):
    remote = FakeRemote(hits=[_item(7, "원격에 있는 질문")], names=["fast-question"])
    try:
        cred = _cred_file(tmp_path, remote.url)
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"db_path": str(_db_with_item(tmp_path))}), encoding="utf-8")
        r = subprocess.run([sys.executable, str(ROOT / "hooks" / "ask_kb.py")],
                           input=json.dumps({"prompt": ".part 에 먼저 쓰는 이유가 뭐야?", "cwd": r"D:\fast-question"}).encode(),
                           capture_output=True, timeout=60,
                           env={"FAST_QUESTION_CONFIG": str(path), "SYSTEMROOT": "C:\Windows",
                                "FQ_CREDENTIALS": str(cred), "USERPROFILE": str(tmp_path / "home")})
    finally:
        remote.close()
    text = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "#7" in text and f"{remote.url}/#qa/7" in text and "#1" not in text  # 로컬 DB 를 읽지 않는다
    assert {auth for _, auth in remote.seen} == {"Bearer fq_pat_test"}
    assert (tmp_path / "home" / ".fast-question" / "projects.json").exists()


def test_hook_reads_local_db_in_local_mode(tmp_path):
    remote = FakeRemote(hits=[_item(7, "원격에 있는 질문")])
    try:
        cred = _cred_file(tmp_path, remote.url)
        cred.write_text(json.dumps({"url": remote.url, "token": "fq_pat_test", "mode": "local"}), encoding="utf-8")
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"db_path": str(_db_with_item(tmp_path)), "port": 1}), encoding="utf-8")
        r = subprocess.run([sys.executable, str(ROOT / "hooks" / "ask_kb.py")],
                           input=json.dumps({"prompt": ".part 에 먼저 쓰는 이유가 뭐야?"}).encode(),
                           capture_output=True, timeout=60,
                           env={"FAST_QUESTION_CONFIG": str(path), "SYSTEMROOT": r"C:\Windows",
                                "FQ_CREDENTIALS": str(cred), "USERPROFILE": str(tmp_path / "home")})
    finally:
        remote.close()
    text = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "#1" in text and "#7" not in text and remote.seen == []


def test_client_hook_ignores_local_mode(tmp_path, monkeypatch):
    """플러그인(fq_client hook, remote_only)은 원격 전용이라 mode 를 보지 않는다."""
    remote = FakeRemote(hits=[_item(7, "원격에 있는 질문")])
    try:
        cred = _cred_file(tmp_path, remote.url)
        cred.write_text(json.dumps({"url": remote.url, "token": "fq_pat_test", "mode": "local"}), encoding="utf-8")
        monkeypatch.setenv("FQ_CREDENTIALS", str(cred))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps({"prompt": ".part 에 먼저 쓰는 이유가 뭐야?"}).encode()))
        monkeypatch.setattr(sys, "stdin", stdin)
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        assert ask_kb.main(remote_only=True) == 0
    finally:
        remote.close()
    assert "#7" in json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]


def test_hook_is_silent_when_remote_is_down(tmp_path):
    cred = _cred_file(tmp_path, "http://127.0.0.1:1")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"db_path": str(_db_with_item(tmp_path))}), encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "hooks" / "ask_kb.py")],
                       input=json.dumps({"prompt": ".part 에 먼저 쓰는 이유가 뭐야?"}).encode(),
                       capture_output=True, timeout=60,
                       env={"FAST_QUESTION_CONFIG": str(path), "SYSTEMROOT": "C:\Windows", "FQ_CREDENTIALS": str(cred)})
    assert r.returncode == 0 and r.stdout == b""


def test_remote_projects_are_cached_for_a_day(tmp_path):
    remote = FakeRemote(names=["fast-question"])
    cache = tmp_path / "projects.json"
    cred = {"url": remote.url, "token": "fq_pat_test"}
    try:
        assert ask_kb.remote_projects(cred, cache, now=1000.0)["names"] == ["fast-question"]
        ask_kb.remote_projects(cred, cache, now=1000.0 + 3600)
        assert len(remote.seen) == 1
        ask_kb.remote_projects(cred, cache, now=1000.0 + 25 * 3600)
        assert len(remote.seen) == 2
    finally:
        remote.close()
    down = {"url": remote.url, "token": "fq_pat_test"}  # 서버가 꺼져도 옛 캐시를 쓴다
    assert ask_kb.remote_projects(down, cache, now=1000.0 + 50 * 3600)["names"] == ["fast-question"]
    other = {"url": "http://127.0.0.1:1", "token": "fq_pat_test"}  # 다른 서버의 캐시는 쓰지 않는다
    assert ask_kb.remote_projects(other, cache, now=1000.0)["names"] == []
    assert ask_kb.project_of(r"D:\fast-question\kb", None, {"names": ["fast-question"], "roots": []}) == "fast-question"
