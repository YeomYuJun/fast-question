"""웹 Claude 패널(web/claude_chat.py): API 키 등록·암호화, 서버 대화 반복과 도구 호출, SSE 진행.
Anthropic 은 부르지 않는다. FakeClient 가 정해 둔 응답(턴)을 차례로 돌려주고 받은 요청을 기록한다."""
import json
from types import SimpleNamespace

import pytest
from anthropic.types.beta import BetaMessage
from starlette.testclient import TestClient

from kb import control, tenant
from kb.config import Config
from kb.db import connect
from kb.morph import Analyzer
from web import auth, claude_chat
from web.app import create_app

URL = "https://fq.example.com"
KEY = "sk-ant-api03-test-key-ABCD"


def _no_kiwi():
    raise RuntimeError("이 파일은 형태소 계층을 쓰지 않는다")


class FakeStream:
    def __init__(self, message: dict, fail: Exception | None):
        self.message, self.fail = BetaMessage.model_validate(message), fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._events()

    async def _events(self):
        for b in self.message.content:
            if b.type == "tool_use":
                yield SimpleNamespace(type="content_block_start", content_block=b)
            elif b.type == "text":
                yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=b.text))
            elif b.type == "thinking":
                yield SimpleNamespace(type="content_block_delta",
                                      delta=SimpleNamespace(type="thinking_delta", thinking=b.thinking))
        if self.fail is not None:
            raise self.fail

    async def get_final_message(self):
        return self.message


class FakeClient:
    """turns: 모델 호출마다 돌려줄 (content 블록 목록, stop_reason). fail_at: 그 번째 호출에서 스트림 도중 예외."""

    def __init__(self, turns=(), fail_at=None, fail=None, retrieve_error=None):
        self.turns, self.fail_at, self.fail, self.retrieve_error = list(turns), fail_at, fail, retrieve_error
        self.calls: list[dict] = []
        self.keys: list[str] = []
        self.models = SimpleNamespace(retrieve=self._retrieve)
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    async def _retrieve(self, model):
        if self.retrieve_error is not None:
            raise self.retrieve_error
        return SimpleNamespace(id=model)

    def _stream(self, **kw):
        self.calls.append(json.loads(json.dumps(kw, default=str)))
        n = len(self.calls)
        content, stop = self.turns[n - 1]
        message = {"id": f"msg_{n}", "type": "message", "role": "assistant", "model": kw["model"],
                   "content": content, "stop_reason": stop, "stop_sequence": None,
                   "usage": {"input_tokens": 1, "output_tokens": 1}}
        return FakeStream(message, self.fail if n == self.fail_at else None)


def tool_use(id_, name, input_):
    return {"type": "tool_use", "id": id_, "name": name, "input": input_}


@pytest.fixture
def cfg(tmp_path):
    return Config(db_path=tmp_path / "local.sqlite3", host="0.0.0.0", port=8765, backup_dir=tmp_path / "backup",
                  backup_keep=3, log_dir=tmp_path / "logs", log_keep_days=14, recheck_interval_hours=24,
                  mode="remote", public_url=URL, data_dir=tmp_path / "data")


@pytest.fixture
def fake():
    return FakeClient()


@pytest.fixture
def app(cfg, fake):
    def factory(key):
        fake.keys.append(key)
        return fake
    return create_app(cfg, run_jobs=False, analyzer=Analyzer(loader=_no_kiwi), claude_client_factory=factory)


def login(cfg, app):
    """실제 세션 쿠키로 로그인한 클라이언트와 계정 id."""
    conn = control.connect_control(tenant.control_path(cfg.data_dir))
    acc = control.create_account(conn, "tester")
    token = control.create_session(conn, acc)
    conn.close()
    c = TestClient(app, base_url=URL)
    c.cookies.set(auth.cookie_names(cfg)[0], token)
    return c, acc


def events(r) -> list[dict]:
    return [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]


def register(c):
    r = c.put("/api/claude/key", json={"key": KEY})
    assert r.status_code == 200, r.text
    return r.json()


def test_key_is_checked_encrypted_and_masked(cfg, app, fake):
    c, acc = login(cfg, app)
    assert c.get("/api/claude/key").json() == {"set": False, "last4": None, "model": claude_chat.DEFAULT_MODEL,
                                               "models": list(claude_chat.MODELS), "stale": False}
    assert register(c) == {"ok": True, "last4": "ABCD", "model": claude_chat.DEFAULT_MODEL}
    assert fake.keys == [KEY]  # 등록 전에 그 키로 모델 조회를 한 번 했다
    got = c.get("/api/claude/key").json()
    assert got["set"] is True and got["last4"] == "ABCD" and KEY not in json.dumps(got)
    conn = control.connect_control(tenant.control_path(cfg.data_dir))
    row = control.get_claude_key(conn, acc)
    conn.close()
    assert KEY not in row["secret"] and (cfg.data_dir / claude_chat.SECRET_FILE).exists()
    assert c.put("/api/claude/key", json={"model": "claude-haiku-5-5"}).json()["model"] == "claude-haiku-5-5"
    assert c.put("/api/claude/key", json={"model": "gpt-4"}).status_code == 400
    assert c.delete("/api/claude/key").json() == {"ok": True}
    assert c.get("/api/claude/key").json()["set"] is False


def test_rejected_key_is_not_stored(cfg, app, fake):
    fake.retrieve_error = RuntimeError("401")
    c, _ = login(cfg, app)
    r = c.put("/api/claude/key", json={"key": KEY})
    assert r.status_code == 400 and r.json()["code"] == "rejected"
    assert c.get("/api/claude/key").json()["set"] is False


def test_lost_secret_file_means_register_again(cfg, app):
    c, _ = login(cfg, app)
    register(c)
    (cfg.data_dir / claude_chat.SECRET_FILE).write_bytes(claude_chat.Fernet.generate_key())
    app2 = create_app(cfg, run_jobs=False, analyzer=Analyzer(loader=_no_kiwi),
                      claude_client_factory=lambda k: FakeClient())
    c2 = TestClient(app2, base_url=URL, cookies=c.cookies)
    got = c2.get("/api/claude/key").json()
    assert got["set"] is False and got["stale"] is True
    r = c2.post("/api/claude/chat", json={"messages": [{"role": "user", "content": "안녕"}]})
    assert r.status_code == 400 and r.json()["code"] == "no_key"


def test_chat_needs_browser_session_and_key(cfg, app):
    with TestClient(app, base_url=URL) as anon:
        assert anon.post("/api/claude/chat", json={"messages": []}).status_code == 401
    conn = control.connect_control(tenant.control_path(cfg.data_dir))
    acc = control.create_account(conn)
    pat = control.create_personal_token(conn, acc, "pc")["token"]
    conn.close()
    with TestClient(app, base_url=URL, headers={"Authorization": f"Bearer {pat}"}) as bearer:
        assert bearer.get("/api/claude/key").status_code == 401  # 개인 토큰으로는 키를 다룰 수 없다
    c, _ = login(cfg, app)
    r = c.post("/api/claude/chat", json={"messages": [{"role": "user", "content": "안녕"}]})
    assert r.status_code == 400 and r.json()["code"] == "no_key"


@pytest.mark.parametrize("messages", [[], "x", [{"role": "assistant", "content": "a"}],
                                      [{"role": "system", "content": "a"}],
                                      [{"role": "user", "content": "a"}] * (claude_chat.MAX_MESSAGES + 1)])
def test_bad_history_is_refused(cfg, app, messages):
    c, _ = login(cfg, app)
    register(c)
    r = c.post("/api/claude/chat", json={"messages": messages})
    assert r.status_code == 400 and r.json()["code"] == "invalid"


def test_chat_runs_tools_on_the_account_db_and_returns_replayable_history(cfg, app, fake):
    fake.turns = [
        ([{"type": "thinking", "thinking": "분류를 만든다", "signature": "sig1"},
          tool_use("tu1", "kb_add_category", {"name": "웹패널"}),
          tool_use("tu2", "kb_delete_category", {"category": "웹패널", "version": 1, "confirm": True})], "tool_use"),
        ([{"type": "text", "text": "웹패널 분류를 만들었습니다."}], "end_turn"),
    ]
    c, acc = login(cfg, app)
    register(c)
    history = [{"role": "user", "content": "웹패널 분류 만들어 줘"}]
    r = c.post("/api/claude/chat", json={"messages": history})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = events(r)
    kinds = [e["type"] for e in evs]
    assert kinds[0] == "thinking" and "tool_start" in kinds and kinds[-1] == "done"
    results = {e["name"]: e for e in evs if e["type"] == "tool_result"}
    assert results["kb_add_category"]["is_error"] is False
    assert results["kb_delete_category"]["is_error"] is True  # 이 화면에서 쓸 수 없는 도구
    assert "".join(e["text"] for e in evs if e["type"] == "text") == "웹패널 분류를 만들었습니다."

    append = evs[-1]["append"]
    assert [m["role"] for m in append] == ["assistant", "user", "assistant"]
    assert append[0]["content"][0] == {"type": "thinking", "thinking": "분류를 만든다", "signature": "sig1"}
    assert [b["tool_use_id"] for b in append[1]["content"]] == ["tu1", "tu2"]
    assert append[1]["content"][1]["is_error"] is True

    first, second = fake.calls
    assert first["model"] == claude_chat.DEFAULT_MODEL and first["fallbacks"] == "default"
    assert first["betas"] == [claude_chat.FALLBACK_BETA]
    assert first["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert {t["name"] for t in first["tools"]} == set(claude_chat.ALLOWED_TOOLS)
    assert all(t["eager_input_streaming"] for t in first["tools"])
    assert second["messages"] == history + append[:2]  # 두 번째 호출은 첫 응답과 도구 결과를 이어 받았다

    conn = connect(tenant.db_path_for(cfg.data_dir, acc))
    row = conn.execute("SELECT created_by FROM category WHERE name = '웹패널' AND deleted_at IS NULL").fetchone()
    conn.close()
    assert row is not None and row[0] == "claude"


def test_haiku_runs_without_server_fallback(cfg, app, fake):
    fake.turns = [([{"type": "text", "text": "네"}], "end_turn")]
    c, _ = login(cfg, app)
    register(c)
    c.put("/api/claude/key", json={"model": "claude-haiku-5-5"})
    events(c.post("/api/claude/chat", json={"messages": [{"role": "user", "content": "안녕"}]}))
    assert fake.calls[0]["model"] == "claude-haiku-5-5"
    assert "fallbacks" not in fake.calls[0] and "betas" not in fake.calls[0]


def test_failure_mid_stream_sends_error_without_history(cfg, app, fake):
    fake.turns = [([tool_use("tu1", "kb_categories", {})], "tool_use"), ([{"type": "text", "text": "반"}], "end_turn")]
    fake.fail_at, fake.fail = 2, RuntimeError("끊김")
    c, _ = login(cfg, app)
    register(c)
    evs = events(c.post("/api/claude/chat", json={"messages": [{"role": "user", "content": "분류 보여 줘"}]}))
    assert evs[-1]["type"] == "error" and not any(e["type"] == "done" for e in evs)


def test_truncated_tool_call_is_not_run(cfg, app, fake):
    fake.turns = [([tool_use("tu1", "kb_add_category", {"name": "잘림"})], "max_tokens")]
    c, acc = login(cfg, app)
    register(c)
    evs = events(c.post("/api/claude/chat", json={"messages": [{"role": "user", "content": "분류"}]}))
    assert evs[-1]["type"] == "error" and not any(e["type"] == "tool_result" for e in evs)
    conn = connect(tenant.db_path_for(cfg.data_dir, acc))
    assert conn.execute("SELECT count(*) FROM category WHERE name = '잘림'").fetchone()[0] == 0
    conn.close()


def test_deleting_account_removes_key(cfg, tmp_path):
    conn = control.connect_control(tenant.control_path(cfg.data_dir))
    acc = control.create_account(conn)
    control.put_claude_key(conn, acc, "sealed", "ABCD", claude_chat.DEFAULT_MODEL)
    control.delete_account(conn, acc)
    assert control.get_claude_key(conn, acc) is None
    conn.close()
