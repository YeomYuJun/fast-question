"""영어 질문의 중복 판정(kb.text.similar_level) 표본.

다른 사실을 묻는 쌍이 등록을 막는 수준(block)으로 나오는 수를 고정해 두고, 기준을 바꿀 때 같은 표본으로 비교한다.
같은 사실을 묻는 쌍은 block 이나 notice(등록하고 후보를 알림) 중 하나면 된다.
"""
import pytest

from kb.text import similar_level

SAME = [
    ("Where is the web server config file stored?", "Where is the config file for the web server stored?"),
    ("Why does the nightly backup job run at 3 AM?", "Why is the nightly backup job scheduled at 3 AM?"),
    ("Which port does the MCP proxy listen on?", "What port does the MCP proxy listen on?"),
    ("How do I rotate the API token for the deploy bot?", "How to rotate the deploy bot API token?"),
    ("What command rebuilds the search index?", "Which command rebuilds the search index?"),
    ("Why does the importer reject files with a different schema version?",
     "Why are files with a different schema version rejected by the importer?"),
    ("Where are uploaded attachments saved on disk?", "Where on disk are uploaded attachments saved?"),
    ("How long are access logs kept?", "How long are the access logs retained?"),
    ("What does evidence_state stale mean?", "What does evidence_state stale mean in fast-question?"),
    ("Which Python version does the server image use?", "What Python version is used by the server image?"),
    ("Why does login fail with invalid_grant after a token refresh?",
     "Why does a token refresh lead to invalid_grant on login?"),
    ("Who owns the billing service on-call rotation?", "Who owns on-call rotation for the billing service?"),
    ("How is the cache invalidated after a write?", "How does a write invalidate the cache?"),
    ("What is the default page size of the list API?", "What is the list API's default page size?"),
    ("Where do I set the SMTP host for outgoing mail?", "Where is the SMTP host for outgoing mail set?"),
    ("Why are deleted rows kept instead of removed?", "Why are deleted rows archived instead of removed?"),
    ("How do I run only the e2e tests?", "How can I run just the e2e tests?"),
    ("What timezone are the cron schedules written in?", "Which timezone do the cron schedules use?"),
]

DIFFERENT = [
    ("Where is the web server config file stored?", "Where is the web server log file stored?"),
    ("Why does the nightly backup job run at 3 AM?", "Why does the nightly cleanup job run at 4 AM?"),
    ("Which port does the MCP proxy listen on?", "Which port does the web dashboard listen on?"),
    ("How do I rotate the API token for the deploy bot?", "How do I revoke the API token for the deploy bot?"),
    ("What command rebuilds the search index?", "What command clears the search cache?"),
    ("How long are access logs kept?", "How long are signup IP addresses kept?"),
    ("Which Python version does the server image use?", "Which Node version does the dashboard build use?"),
    ("Where are uploaded attachments saved on disk?", "Where are exported backups saved on disk?"),
    ("What is the default page size of the list API?", "What is the maximum page size of the search API?"),
    ("How do I run only the e2e tests?", "How do I skip the e2e tests in CI?"),
    ("Why does login fail with invalid_grant after a token refresh?",
     "Why does login fail with redirect_uri_mismatch?"),
    ("Who owns the billing service on-call rotation?", "Who approves changes to the billing service schema?"),
    ("What timezone are the cron schedules written in?", "What timezone are timestamps stored in the database?"),
    ("How is the cache invalidated after a write?", "How large can the search cache grow?"),
    ("Where do I set the SMTP host for outgoing mail?", "Where do I set the sender address for outgoing mail?"),
    ("What does evidence_state stale mean?", "What does evidence_state unknown mean?"),
    ("Why is the question limit 300 characters?", "Why is the answer limit 800 characters?"),
    ("Which region hosts the production database?", "Which region hosts the staging database?"),
]

DIFFERENT_BLOCKED_MAX = 0  # 허용치: 다른 사실 18쌍 중 등록이 막히는 쌍
SAME_BLOCKED = 8  # 같은 사실 18쌍 중 등록이 막히는 쌍. 나머지 10쌍은 notice 다


def _level(a: str, b: str) -> str | None:
    return similar_level(a, b)[1]


def test_sample_size():
    assert len(SAME) + len(DIFFERENT) >= 30


def test_different_fact_pairs_are_not_blocked():
    assert sum(_level(*p) == "block" for p in DIFFERENT) <= DIFFERENT_BLOCKED_MAX


def test_same_fact_pairs_are_blocked_or_noticed():
    assert all(_level(*p) in ("block", "notice") for p in SAME)
    assert sum(_level(*p) == "block" for p in SAME) == SAME_BLOCKED


@pytest.mark.parametrize("a, b", [
    ("Where is the config file?", "where is the config file"),
    ("Where is the config file?", "Where  is the CONFIG file ?"),
    ("Why?", "why"),
])
def test_case_space_and_question_mark_only_differences_are_blocked(a, b):
    assert _level(a, b) == "block"


def test_add_qa_blocks_or_notices(conn):
    from kb.store import Store
    s = Store(conn, "claude")
    s.add_category("Ops")
    first = s.add_qa("Ops", "Where is the web server config file stored?", "In deploy/config.json.")["item"]["id"]
    res = s.add_qa("Ops", "Where is the web server log file stored?", "In data/logs/web.log.")
    assert res["status"] == "created"
    assert [(i["id"], i["level"]) for i in res["similar"]] == [(first, "notice")]
    res = s.add_qa("Ops", "where is the web server config file stored", "In deploy/config.json.")
    assert res["status"] == "similar_found" and res["similar"][0]["level"] == "block"
    res = s.add_qa("Ops", "How long are signup IP addresses kept?", "Ninety days.")
    assert res["status"] == "created" and "similar" not in res


def test_add_qa_hangul_still_blocks_at_0_6(conn):
    from kb.store import Store
    s = Store(conn, "user")
    s.add_category("이관")
    s.add_qa("이관", "쓰기 전에 여유 공간을 검사하는 이유", "볼륨 용량이 정해져 있다.")
    res = s.add_qa("이관", "쓰기 전에 여유 공간을 비우는 이유", "임시 파일을 지운다.")
    assert res["status"] == "similar_found" and res["similar"][0]["level"] == "block"


def test_hangul_pairs_keep_char_bigrams_and_0_6():
    assert similar_level("NAS 여유 공간 검사 이유", "NAS 여유공간 검사 이유") == (1.0, "block")
    score, level = similar_level("쓰기 전에 여유 공간을 검사하는 이유", "쓰기 전에 여유 공간을 비우는 이유")
    assert (level == "block") == (score >= 0.6)
    assert similar_level("config 파일 위치는", "config 파일 위치")[1] == "block"
