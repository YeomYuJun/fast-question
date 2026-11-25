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
    text = ask_kb.context("NAS 질문", {"hits": [_item(3, q="NAS 질문", state="stale")], "similar": []},
                          "http://127.0.0.1:8765")
    assert "#3" in text and "http://127.0.0.1:8765/#qa/3" in text and "stale" in text
    assert "먼저" in text


def test_store_conditions_cover_cost_and_exclusions():
    text = "\n".join(ask_kb.store_conditions("이관"))
    assert "이관" in text and "rules.md" in text
    # 등록 조건: 다시 알아내는 비용, 이 세션이 쓴 파일 제외, 문서 위치 제외
    assert "비용" in text and "만들거나 고친 파일" in text and "위치" in text


def test_hook_marks_a_question_without_answer_instead_of_giving_conditions(tmp_path, monkeypatch):
    """KB 에 답이 없는 질문에는 등록 조건을 넣지 않고 표시만 둔다. 조건은 수확 훅이 턴이 끝난 뒤 읽기 수를 보고 넣는다."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    marker = ask_kb.session_file("s-1", ask_kb.ASKED)
    ask_kb.mark_asked("s-1", True)
    assert marker.exists() and marker.read_text(encoding="utf-8") == ""  # 질문을 적지 않는다
    ask_kb.mark_asked("s-1", False); ask_kb.mark_asked("s-1", False)
    assert not marker.exists()
    ask_kb.mark_asked("", True)  # 세션 id 가 없으면 두지 않는다
    assert ask_kb.context("모르는 것", {"hits": [], "similar": []}, "http://x") == ""


@pytest.mark.parametrize("query,question,answer,expected", [
    ("RateLimitFilter 는 분당 몇 회야", "lea BE 의 요청 수 제한(RateLimitFilter)은 어떻게 걸리나?", "답", True),
    ("lea 관리자 401 원인이", "lea 관리자(cms) API 가 일부만 401 이 되는 원인은?", "답", True),
    ("넷퍼넬 대기열은 언제 끝나", "통합예약 포털에서 넷퍼넬 대기열은 언제 끝나나?", "답", True),
    ("넷퍼넬", "통합예약 넷퍼넬에서 요청이 반복되는 것은 오류인가?", "답", True),
    # 대화 문장: 형태소 계층이 하다·되다·남다 로 맞춘 무관한 항목
    ("그럼 해야 돼", "readinessProbe 없이 롤링 업데이트를 하면?", "새 Pod 가 뜨기 전에 트래픽을 받게 해야 한다", False),
    ("흠 남는 게 없는데 그럼", "Pod 가 Evicted 상태로 남으면 무엇을 뜻하나?", "남는 Pod 는 지워도 된다", False),
    ("흠 남는 게 없는데 그럼", "Pod 가 Evicted 상태로 남으면?", "남는 Pod 는 자원이 없는 노드에서 지워도 된다", False),
    ("결제 준비 값은 어디서 정해져", "통합예약 결제 준비에서 MID 값은 어떻게 정해지나?", "답", True),
    ("여전히 안되는데", "Cocktail 파이프라인으로 배포한 리소스를 직접 고치면?", "다음 배포에서 되돌아간다", False),
    ("RateLimitFilter 는 분당 몇 회야", "NodePort 로 들어온 요청의 클라이언트 IP 는?", "노드 주소다", False),
    ("md 는 지워도 돼", "README.md 를 지우면?", "빌드가 실패한다", False),
])
def test_grounded_requires_target_name_in_item(query, question, answer, expected):
    assert ask_kb.grounded(query, {"question": question, "answer": answer}) is expected


def test_context_drops_results_without_the_target_name():
    stray = _item(7, q="readinessProbe 없이 롤링 업데이트를 하면?", a="트래픽을 받게 해야 한다")
    assert ask_kb.context("그럼 해야 돼", {"hits": [stray], "similar": [dict(stray, score=0.7)]}, "http://x") == ""


def test_context_without_hits_never_asks_to_store():
    res = {"hits": [], "similar": [dict(_item(5, a="NAS 답변"), score=0.6)]}
    text = ask_kb.context("NAS 답변", res, "http://127.0.0.1:8765")
    assert "#5" in text and "저장" not in text and "등록" not in text
    assert ask_kb.context("모르는 것", {"hits": [], "similar": []}, "http://127.0.0.1:8765") == ""


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


def test_project_sources_include_folders_of_live_file_evidence(tmp_path):
    def norm(p):
        return str(p).replace("\\", "/").rstrip("/").lower()

    for rel in ("proj/a.txt", "proj/src/b.txt", "gone/c.txt"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x = 1\n", encoding="utf-8")
    s = Store(connect(tmp_path / "kb.sqlite3"), "claude")
    s.add_category("이관")

    def add(question, rel, kind="file"):
        return s.add_qa("이관", question, "다 쓴 뒤 rename 으로 확정한다.",
                        evidence=[{"kind": kind, "path": str(tmp_path / rel)}])

    add("a 파일 설정 값의 위치는", "proj/a.txt")
    add("b 모듈이 재시도하는 조건은", "proj/src/b.txt")  # proj 안이라 따로 나오지 않는다
    gone = add("c 배치가 도는 시각은", "gone/c.txt")["item"]
    s.harvest_set(r"D:\work", {"created": 1})
    assert s.project_sources()["roots"] == sorted(["d:/work", norm(tmp_path / "gone"), norm(tmp_path / "proj")])
    s.archive_qa(gone["id"], gone["version"])
    assert s.project_sources()["roots"] == sorted(["d:/work", norm(tmp_path / "proj")])
    s.conn.close()


def test_project_of_matches_folder_that_holds_project_roots(monkeypatch):
    src = {"names": [], "roots": ["d:/paas/java", "c:/users/me/repo"]}
    monkeypatch.setattr(ask_kb.Path, "home", classmethod(lambda cls: Path("C:/Users/me")))
    assert ask_kb.project_of(r"D:\PaaS\java\src", None, src) == "d:/paas/java"
    assert ask_kb.project_of(r"D:\PaaS", None, src) == "d:/paas"  # 저장소 여럿을 담은 작업 폴더
    assert ask_kb.project_of("D:\\", None, src) is None  # 드라이브 루트는 무엇이든 담는다
    assert ask_kb.project_of(r"C:\Users\me", None, src) is None  # 홈 폴더
    assert ask_kb.project_of(r"C:\Users", None, src) is None  # 홈 폴더의 위
    assert ask_kb.project_of(r"C:\Users\me\repo\kb", None, src) == "c:/users/me/repo"
    assert ask_kb.project_of(r"D:\other", None, src) is None


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
    remote = FakeRemote(hits=[_item(7, "원격에 있는 .part 질문")], names=["fast-question"])
    try:
        cred = _cred_file(tmp_path, remote.url)
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"db_path": str(_db_with_item(tmp_path))}), encoding="utf-8")
        r = subprocess.run([sys.executable, str(ROOT / "hooks" / "ask_kb.py")],
                           input=json.dumps({"prompt": ".part 에 먼저 쓰는 이유가 뭐야?", "cwd": r"D:\fast-question"}).encode(),
                           capture_output=True, timeout=60,
                           env={"FAST_QUESTION_CONFIG": str(path), "SYSTEMROOT": r"C:\Windows",
                                "FQ_CREDENTIALS": str(cred), "USERPROFILE": str(tmp_path / "home")})
    finally:
        remote.close()
    text = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "#7" in text and f"{remote.url}/#qa/7" in text and "#1" not in text  # 로컬 DB 를 읽지 않는다
    assert {auth for _, auth in remote.seen} == {"Bearer fq_pat_test"}
    assert (tmp_path / "home" / ".fast-question" / "projects.json").exists()


def test_hook_leaves_a_mark_and_no_text_when_the_project_has_no_answer(tmp_path):
    remote = FakeRemote(hits=[], names=["fast-question"])
    marker = tmp_path / "home" / ".fast-question" / "stop" / "s-1.asked"
    cred = _cred_file(tmp_path, remote.url)

    def ask(prompt, cwd):
        return subprocess.run([sys.executable, str(ROOT / "hooks" / "ask_kb.py")],
                              input=json.dumps({"prompt": prompt, "cwd": cwd, "session_id": "s-1"}).encode(),
                              capture_output=True, timeout=60,
                              env={"SYSTEMROOT": r"C:\Windows", "FQ_CREDENTIALS": str(cred),
                                   "USERPROFILE": str(tmp_path / "home")})
    try:
        r = ask("uselog 의 measure 는 무엇을 세나?", r"D:\fast-question")
        assert r.returncode == 0 and r.stdout == b"" and marker.exists()
        # 다음 입력이 질문이 아니면 앞 턴의 표시를 지운다
        assert ask("그 파일 고쳐줘", r"D:\fast-question").stdout == b"" and not marker.exists()
        # 프로젝트가 아닌 폴더의 질문에는 표시를 두지 않는다
        assert ask("uselog 의 measure 는 무엇을 세나?", r"C:\Users\someone\other").stdout == b"" and not marker.exists()
    finally:
        remote.close()


def test_hook_reads_local_db_in_local_mode(tmp_path):
    remote = FakeRemote(hits=[_item(7, "원격에 있는 .part 질문")])
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
    remote = FakeRemote(hits=[_item(7, "원격에 있는 .part 질문")])
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
                       env={"FAST_QUESTION_CONFIG": str(path), "SYSTEMROOT": r"C:\Windows", "FQ_CREDENTIALS": str(cred)})
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
