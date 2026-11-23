import re
from pathlib import Path

import pytest

from kb.errors import ValidationError
from kb.rules import ANSWER_MAX_SENTENCES, check_claude_qa, problems
from kb.text import ANSWER_MAX, QUESTION_MAX

RULES_MD = Path(__file__).resolve().parent.parent / "skill" / "qa" / "rules.md"


def test_two_sentences_pass():
    assert problems(answer="rename 은 같은 볼륨에서만 원자적이다. 그래서 .nas-tmp 를 루트 안에 둔다.") == []


def test_three_sentences_rejected():
    msgs = problems(answer="첫째다. 둘째다. 셋째다.")
    assert len(msgs) == 1 and "3 sentences" in msgs[0]


def test_head_line_with_items_passes():
    answer = "공백은 AND, a | b 는 OR 이다.\n- -a: 제외\n- \"a b\": 구절\n- q:·a: 질문·답변 한정\n- path:x: 근거 파일 경로"
    assert problems(answer=answer) == []


@pytest.mark.parametrize("answer, part", [
    ("- 첫 줄부터 항목", "Start the answer"),
    ("핵심 답이다.\n항목 머리 없는 둘째 줄", "starting with '- '"),
    ("핵심 답이다.\n- 항목에 문장을 둘 쓴다. 이렇게 쓴다.", "one fact"),
    ("핵심 답이다.\n- 마침표로 끝나는 항목이다.", "one fact"),
    ("가" * 121, "line 1 is 242 long"),
    ("a" * 241, "line 1 is 241 long"),
])
def test_answer_form_rejected(answer, part):
    assert any(part in m for m in problems(answer=answer)), problems(answer=answer)


def test_line_limit_is_weighted():
    assert problems(answer="가" * 120) == []
    assert problems(answer="a" * 240) == []


@pytest.mark.parametrize("answer, kind", [
    ("검토 기능은 2026-10-08 사용자 결정으로 지웠다.", "decision history"),
    ("설계 문서 §2 에 따라 지운다.", "section number"),
    ("캐시 때문인 것으로 보인다.", "speculation"),
    ("재시도는 TODO 로 남아 있다.", "progress status"),
    ("The review feature was removed on 2026-10-08 as the owner decided.", "decision history"),
    ("Approved on 2026-10-08, the limit is 300.", "decision history"),
    ("Retry logic is still TODO.", "progress status"),
    ("The migration is WIP.", "progress status"),
    ("The English guide is TBD.", "progress status"),
    ("The search page is a work in progress.", "progress status"),
])
def test_banned_expressions_rejected(answer, kind):
    assert any(kind in m for m in problems(answer=answer)), problems(answer=answer)


SPECULATION_EN = [
    "The timeout is probably caused by the cache.",
    "Probably the proxy drops the header.",
    "The importer seems to skip empty rows.",
    "The cache seem to expire after an hour.",
    "The job appears to run twice on Mondays.",
    "I think the job runs hourly.",
    "I guess the token is refreshed on login.",
    "Not sure whether the hook runs on Linux.",
    "The value is presumably read from config.json.",
    "The worker appears to retry three times.",
    "It seems to depend on the region.",
    "I'm not sure the index is rebuilt nightly.",
]


@pytest.mark.parametrize("answer", SPECULATION_EN)
def test_english_speculation_rejected(answer):
    assert any("speculation" in m for m in problems(answer=answer)), problems(answer=answer)


# 영어 답의 정상 문장. 날짜, 버전, 포트, 번호, 해시가 들어가도 거부되지 않아야 한다
NORMAL_EN = [
    "The web server reads config.json at startup.",
    "Uploads are stored under data/uploads on the data volume.",
    "The nightly job runs at 03:00 UTC and deletes archived rows older than 90 days.",
    "Kiwi takes about 2.5 seconds to load on the first search.",
    "The server listens on 127.0.0.1:8765 and does not bind 0.0.0.0.",
    "Version 0.1.5 of the client adds the lint-draft command.",
    "Windows 11 build 10.0.26200 runs the hook without changes.",
    "The release was tagged on 2026-11-20 from commit 3f2a9c1.",
    "The fix is in PR #1234 and issue #567.",
    "The importer rejects files whose schema version differs from the server.",
    "Set FQ_CREDENTIALS to point the client at another credentials file.",
    "Search runs the exact, synonym and morpheme tiers in that order.",
    "The proxy forwards kb_search calls to the remote server with the stored token.",
    "Rows are archived, not deleted; deleted_at holds the archive time.",
    "The cache holds at most 200 databases and 500000 documents.",
    "The answer limit is 800, counted with Hangul as 2.",
    "Export downloads the whole database as one SQLite file.",
    "The token header uses the form token: <your token>.",
    "Run tests with python -m pytest from the repository root.",
    "The range U+AC00-D7A3 counts as width 2.",
    "Prices are listed as 1,200,000 KRW per year.",
    "The ratio 300-800 applies to question and answer limits.",
    "Each request waits up to 5000 ms for the database lock.",
    "Phone support is not offered; use the web form instead.",
    "The field might be null when the user has no email.",
    "Signatures are not verified in local mode.",
    "The watcher is not started until the first request.",
    "The banner appears to users after their first login.",
    "The job is in progress while the lock row exists.",
    "The status column holds pending, done or failed.",
]


@pytest.mark.parametrize("answer", NORMAL_EN)
def test_normal_english_answers_pass(answer):
    assert problems(answer=answer) == []


@pytest.mark.parametrize("answer", [
    "Kiwi 로드는 2.54초 걸린다(2026-10-08 실측).",
    "kb.jobs.run_due 가 data/logs/web.log 에 남긴다. 버전은 0.24.0 이다.",
    "ATOMIC_MOVE 로 올린다. 실패하면 .part 를 지운다(남은 것은 PartFileSweeper 가 지운다).",
    "끝에 물음표가 없는 질문도 된다... 말줄임은 문장 끝으로 세지 않는다.",
])
def test_dots_inside_tokens_are_not_sentence_ends(answer):
    assert problems(answer=answer) == []


@pytest.mark.parametrize("text, kind", [
    ("DB 접속은 password=Abc123! 로 한다.", "password"),
    ("비밀번호: hunter2 를 쓴다.", "password"),
    ("키는 AKIAABCDEFGHIJKLMNOP 이다.", "AWS access key"),
    ("-----BEGIN RSA PRIVATE KEY----- 로 시작한다.", "private key"),
    ("운영 DB 는 10.20.30.40 에 있다.", "IP address"),
    ("담당은 hong@example.com 이다.", "email"),
    ("연락처는 010-1234-5678로 한다.", "phone number"),
    ("주민번호 900101-1234567 형식", "resident registration"),
    ("Call 415-555-0132 for access.", "phone number"),
    ("Call (415) 555-0132 for access.", "phone number"),
    ("Call 415.555.0132 for access.", "phone number"),
    ("Call +1 415 555 0132 for access.", "phone number"),
    ("The London desk is +44 20 7946 0958.", "phone number"),
    ("Text +14155550132 to reset.", "phone number"),
])
def test_sensitive_values_rejected(text, kind):
    msgs = problems(answer=text)
    assert any(kind in m for m in msgs), msgs


@pytest.mark.parametrize("text", [
    "웹은 127.0.0.1:8765 에서만 받는다.",
    "0.0.0.0 으로 바인딩하지 않는다.",
    "kiwipiepy 0.24.0 을 쓴다.",
    "token 과 password 는 이 표에 넣지 않는다.",
])
def test_harmless_values_pass(text):
    assert problems(answer=text) == []


@pytest.mark.parametrize("text", [
    "레지스트리 로그인은 --docker-password=<pw> 로 한다.",
    "헤더는 token: <your token> 형식이다.",
    '설정은 "password=<비밀번호>" 로 둔다.',
])
def test_secret_placeholders_pass(text):
    assert problems(answer=text) == []


def test_placeholder_followed_by_value_is_rejected():
    assert any("password" in m for m in problems(answer="password=<pw>abc 로 한다."))


def test_sensitive_message_points_line_and_masks_value():
    msgs = problems(answer="로그인 방법은 아래와 같다.\n- 명령은 docker login --docker-password=hunter2 로 한다")
    assert any(m.startswith("Answer line 2 contains a password or token value (password=hu…)") for m in msgs), msgs
    assert not any("hunter2" in m for m in msgs)
    (q,) = problems(question="10.1.1.1 서버는 무엇인가")
    assert q.startswith("Question contains an IP address (10.…)")
    with pytest.raises(ValidationError) as e:
        check_claude_qa(answer="담당은 hong@example.com 이다.")
    assert "Answer line 1 contains an email address (ho…)" in e.value.message and "example" not in e.value.message


def test_question_and_note_evidence_are_checked():
    assert any("IP address" in m for m in problems(question="10.1.1.1 서버는 무엇인가"))
    assert any("email" in m for m in problems(evidence=[{"kind": "note", "text": "a@b.co 가 확인"}]))
    assert any("password" in m for m in problems(evidence=[{"kind": "url", "url": "https://x/?token=abc"}]))
    ev = [{"kind": "auto", "path": "D:/repo/app.env", "anchor": "api_key=sk-live-123"}]
    assert any(m.startswith("Evidence contains a password or token value (api_key=sk…)") for m in problems(evidence=ev))
    assert problems(evidence=[{"kind": "auto", "path": "D:/repo/app.env", "anchor": "api_key=<key>"}]) == []


def test_question_mark_is_not_required():
    assert problems(question="fast-question 검색 계층의 순서", answer="E → Y → M 순이다.") == []


def test_check_raises_validation_error_with_all_problems():
    with pytest.raises(ValidationError) as e:
        check_claude_qa(question="10.1.1.1 은?", answer="하나다. 둘이다. 셋이다.")
    assert "IP address" in e.value.message and "3 sentences" in e.value.message and "rules.md" in e.value.message


def test_rules_md_matches_code_limits():
    text = RULES_MD.read_text(encoding="utf-8")
    assert f"{QUESTION_MAX}자" in text and f"{ANSWER_MAX}자" in text
    assert re.search(rf"1~{ANSWER_MAX_SENTENCES}문장", text)
