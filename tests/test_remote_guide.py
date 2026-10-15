"""원격 모드 기본 가이드: 근거 없이, 화면 주소는 public_url 로 바꿔 사용자 DB 를 처음 만들 때 1회 넣는다.
항목의 target 이 local 이면 로컬 가이드에만, remote 면 원격 가이드에만 들어간다(없으면 둘 다)."""
import json
from pathlib import Path

from kb import guide, tenant
from kb.db import connect
from kb.rules import problems
from kb.text import check_answer
from tests.test_remote_web import URL, analyzer, app, cfg, client_for  # noqa: F401 - 픽스처

LOCAL_URL = "http://127.0.0.1:8765"
ROOT = Path(__file__).resolve().parent.parent
# 원격 사용자가 할 수 없거나 서버 운영자만 아는 것: 로컬 실행·설정, 서버에서 거부되는 사용자 단어, 서버 쪽 재검증 주기
LOCAL_ONLY = ("run_web.py", "config.json", "user_word_add", "recheck_interval_hours", "data/backup")


def test_targets_split_local_and_remote_items():
    raw = json.loads(guide.GUIDE_FILE.read_text(encoding="utf-8"))["items"]
    assert {i.get("target") for i in raw} == {None, "local", "remote"}
    local = [i["question"] for i in guide.load()["items"]]
    remote = [i["question"] for i in guide.load_remote(URL)["items"]]
    assert local == [i["question"] for i in raw if i.get("target") != "remote"]
    assert remote == [i["question"] for i in raw if i.get("target") != "local"]
    assert all("target" not in i for i in guide.load()["items"] + guide.load_remote(URL)["items"])


def test_remote_guide_has_no_local_only_instructions():
    for item in guide.load_remote(URL)["items"]:
        assert not [w for w in LOCAL_ONLY if w in item["answer"]], item["question"]


def test_remote_items_follow_registration_rules():
    for item in guide.load_remote(URL)["items"]:
        assert problems(item["question"], item["answer"]) == [], item["question"]


def test_load_remote_drops_evidence_and_uses_public_url():
    remote = guide.load_remote(URL)
    assert all(i["evidence"] == [] for i in remote["items"])
    assert not any(LOCAL_URL in i["answer"] for i in remote["items"])
    assert sum(URL in i["answer"] for i in remote["items"]) > 0


def test_skill_user_word_lines_say_remote_is_refused():
    for rel in ("skill/qa/SKILL.md", "skill/qa/rules.md", "skill/qa-harvest/SKILL.md"):
        lines = [ln for ln in (ROOT / rel).read_text(encoding="utf-8").splitlines() if "user_word_add" in ln]
        assert lines and all("원격" in ln for ln in lines), rel


def test_load_remote_keeps_answers_within_limit_for_long_url():
    long_url = "https://" + "a" * 200 + ".example.com"
    for item in guide.load_remote(long_url)["items"]:
        assert check_answer(item["answer"]) == item["answer"], item["question"]


def test_new_user_db_gets_remote_guide_once(cfg, app):
    n = len(guide.load_remote(URL)["items"])
    with client_for(app, 1) as c:
        cats = c.get("/api/categories").json()["categories"]
        assert [x["name"] for x in cats] == ["fast-question"] and cats[0]["count"] == n
        hit = c.get("/api/search", params={"q": "주소 형식"}).json()["hits"][0]
        item = c.get(f"/api/qa/{hit['id']}").json()
        assert URL in item["answer"] and item["evidence"] == [] and item["created_by"] == "system"
        c.patch(f"/api/qa/{item['id']}", json={"version": item["version"], "answer": "사용자가 고친 답이다."})
        assert c.get("/api/categories").json()["categories"][0]["count"] == n  # 두 번째 요청은 다시 넣지 않는다
    conn = connect(tenant.db_path_for(cfg.data_dir, 1))
    assert conn.execute("SELECT count(*) FROM qa").fetchone()[0] == n
    assert conn.execute("SELECT answer FROM qa WHERE id = ?", (item["id"],)).fetchone()[0] == "사용자가 고친 답이다."
    conn.close()


def test_existing_user_db_is_not_seeded(cfg, app):
    connect(tenant.db_path_for(cfg.data_dir, 1)).close()
    with client_for(app, 1) as c:
        assert c.get("/api/categories").json()["categories"] == []
