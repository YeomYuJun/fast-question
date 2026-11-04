"""GitHub 주소 근거(kb/evidence.py github): 서버도 PC 파일 없이 판정하는 근거. GitHub API 호출은 가짜로 바꾼다."""
import json

import pytest

from kb import evidence
from kb.db import connect
from kb.errors import ValidationError
from kb.store import Store
from tests.test_remote_mcp import call, new_token, seed
from tests.test_auth import URL, analyzer, app, cfg, idp, secrets_env  # noqa: F401 - 픽스처

URL_MAIN = "https://github.com/YeomYuJun/fast-question/blob/main/kb/query.py"
URL_SHA = "https://github.com/YeomYuJun/fast-question/blob/" + "a" * 40 + "/kb/query.py#L10"


class FakeGitHub:
    """저장소 상태: 기본 브랜치 main, 브랜치·커밋별 파일 blob 과 본문."""

    def __init__(self):
        self.blobs = {("main", "kb/query.py"): "b1", ("a" * 40, "kb/query.py"): "b1"}
        self.bodies = {("main", "kb/query.py"): b"a | b, a|b\n"}
        self.calls = []
        self.down = False

    def __call__(self, url, raw=False):
        self.calls.append(url)
        if self.down:
            raise RuntimeError("timeout")
        if url == f"{evidence.GITHUB_API}/repos/YeomYuJun/fast-question":
            return {"default_branch": "main"}
        if url.startswith(f"{evidence.GITHUB_API}/repos/YeomYuJun/fast-question/contents/"):
            path, _, ref = url[len(f"{evidence.GITHUB_API}/repos/YeomYuJun/fast-question/contents/"):].partition("?ref=")
            sha = self.blobs.get((ref, path))
            return None if sha is None else {"type": "file", "sha": sha}
        if url.startswith(f"{evidence.GITHUB_RAW}/YeomYuJun/fast-question/"):
            ref, _, path = url[len(f"{evidence.GITHUB_RAW}/YeomYuJun/fast-question/"):].partition("/")
            return self.bodies.get((ref, path))
        return None


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(evidence, "github_get", fake)
    return fake


def test_github_kind_captures_blob_and_plain_url_stays_url(gh):
    assert evidence.capture({"kind": "url", "url": URL_MAIN})["kind"] == "url"  # url 은 GitHub 주소여도 판정하지 않는다
    assert gh.calls == []
    row = evidence.capture({"kind": "github", "url": URL_MAIN, "anchor": "a | b"})
    assert row["kind"] == "github" and row["path"] == URL_MAIN and row["repo"] == "YeomYuJun/fast-question"
    assert row["commit_sha"] is None and json.loads(row["paths_json"]) == ["kb/query.py"]
    assert json.loads(row["captured_json"])["blob_sha"] == "b1"
    pinned = evidence.capture({"kind": "github", "url": URL_SHA})
    assert pinned["commit_sha"] == "a" * 40 and json.loads(pinned["captured_json"])["ref"] == "a" * 40
    with pytest.raises(ValidationError, match="blob"):
        evidence.capture({"kind": "github", "url": "https://github.com/YeomYuJun/fast-question"})
    assert evidence.spec_of(dict(row))["url"] == URL_MAIN and evidence.line_of([{**row, "anchor": "a | b"}]).startswith(URL_MAIN)


def test_capture_refuses_missing_file_or_github_down(gh):
    with pytest.raises(ValidationError, match="없습니다"):
        evidence.capture({"kind": "github", "url": "https://github.com/YeomYuJun/fast-question/blob/main/nope.py"})
    gh.down = True
    with pytest.raises(ValidationError, match="읽지 못해"):
        evidence.capture({"kind": "github", "url": URL_MAIN})


def test_checker_compares_blob_on_branch_or_default_branch(gh):
    branch = dict(evidence.capture({"kind": "github", "url": URL_MAIN, "anchor": "a | b"}))
    pinned = dict(evidence.capture({"kind": "github", "url": URL_SHA, "anchor": "zzz"}))
    checker = evidence.Checker()
    assert checker.check(branch) == ("same", "")
    assert checker.check(pinned) == ("same", "")  # 커밋 고정 주소는 기본 브랜치(main)의 지금 blob 과 견준다
    gh.blobs[("main", "kb/query.py")] = "b2"
    checker = evidence.Checker()
    assert checker.check(branch) == ("changed", "kb/query.py 내용이 main 에서 바뀌었습니다 (앵커 'a | b' 있음)")
    assert checker.check(pinned) == ("changed", "kb/query.py 내용이 main 에서 바뀌었습니다 (앵커 'zzz' 없음)")
    del gh.blobs[("main", "kb/query.py")]
    assert evidence.Checker().check(branch)[0] == "changed"
    gh.down = True
    assert evidence.Checker().check(branch) == ("unknown", "GitHub 을 읽지 못했습니다: timeout")


def test_checker_caches_api_calls_per_run(gh):
    rows = [dict(evidence.capture({"kind": "github", "url": URL_SHA})) for _ in range(3)]
    gh.calls.clear()
    checker = evidence.Checker()
    for r in rows:
        checker.check(r)
    assert len(gh.calls) == 2  # 저장소 정보 1번 + blob 1번


def test_remote_store_judges_github_only_and_job_runs_daily(cfg, app, gh):
    from kb.jobs import run_due_all
    from kb import tenant
    _, token = new_token(cfg)
    seed(app, token)
    err, res = call(app, token, "kb_add", {"category": "이관", "question": "질문", "answer": "답이다.",
                                           "evidence": [{"kind": "github", "url": URL_MAIN, "anchor": "a | b"},
                                                        {"kind": "url", "url": "https://example.com/doc"}]})
    assert not err and res["item"]["evidence_state"] == "trusted", res
    kinds = {e["kind"] for e in res["item"]["evidence"]}
    assert kinds == {"github", "url"}
    gh.blobs[("main", "kb/query.py")] = "b2"
    # 주기 작업: 하루 한 번 github 근거만 판정한다
    out = run_due_all(cfg, None)
    assert out["users"][1]["github"] == {"checked": 1, "changed": 1}
    assert "github" not in run_due_all(cfg, None).get("users", {}).get(1, {})
    err, out = call(app, token, "kb_recheck", {})  # MCP 로도 원격 서버가 github 근거만 판정한다(url 은 대상이 아니다)
    assert not err and [r["verdict"] for r in out["results"]] == ["changed"]
    assert out["items"][0]["evidence_state"] == "stale"


def test_local_store_recaptures_github_evidence(tmp_path, gh):
    conn = connect(tmp_path / "kb.sqlite3")
    s = Store(conn, "claude")
    s.add_category("일반 지식")
    item = s.add_qa("일반 지식", "질문", "답이다.", evidence=[{"kind": "github", "url": URL_MAIN}])["item"]
    ev = item["evidence"][0]
    assert ev["kind"] == "github" and item["evidence_state"] == "trusted"
    gh.blobs[("main", "kb/query.py")] = "b3"
    s.recheck(item["id"])
    assert s.get_qa(item["id"])["evidence_state"] == "stale"
    again = s.recapture_evidence(ev["id"])
    assert again["evidence_state"] == "trusted"
    assert json.loads(conn.execute("SELECT captured_json FROM evidence WHERE id = ?", (ev["id"],)).fetchone()[0])["blob_sha"] == "b3"
    conn.close()
