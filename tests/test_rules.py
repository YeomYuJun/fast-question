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
    assert len(msgs) == 1 and "3문장" in msgs[0]


def test_head_line_with_items_passes():
    answer = "공백은 AND, a | b 는 OR 이다.\n- -a: 제외\n- \"a b\": 구절\n- q:·a: 질문·답변 한정\n- path:x: 근거 파일 경로"
    assert problems(answer=answer) == []


@pytest.mark.parametrize("answer, part", [
    ("- 첫 줄부터 항목", "첫 줄"),
    ("핵심 답이다.\n항목 머리 없는 둘째 줄", "'- ' 로 시작"),
    ("핵심 답이다.\n- 항목에 문장을 둘 쓴다. 이렇게 쓴다.", "한 사실만"),
    ("핵심 답이다.\n- 마침표로 끝나는 항목이다.", "한 사실만"),
    ("가" * 121, "121자"),
])
def test_answer_form_rejected(answer, part):
    assert any(part in m for m in problems(answer=answer)), problems(answer=answer)


@pytest.mark.parametrize("answer, kind", [
    ("검토 기능은 2026-10-08 사용자 결정으로 지웠다.", "결정 경위"),
    ("설계 문서 §2 에 따라 지운다.", "절 번호"),
    ("캐시 때문인 것으로 보인다.", "추정"),
    ("재시도는 TODO 로 남아 있다.", "할 일"),
])
def test_banned_expressions_rejected(answer, kind):
    assert any(kind in m for m in problems(answer=answer)), problems(answer=answer)


@pytest.mark.parametrize("answer", [
    "Kiwi 로드는 2.54초 걸린다(2026-10-08 실측).",
    "kb.jobs.run_due 가 data/logs/web.log 에 남긴다. 버전은 0.24.0 이다.",
    "ATOMIC_MOVE 로 올린다. 실패하면 .part 를 지운다(남은 것은 PartFileSweeper 가 지운다).",
    "끝에 물음표가 없는 질문도 된다... 말줄임은 문장 끝으로 세지 않는다.",
])
def test_dots_inside_tokens_are_not_sentence_ends(answer):
    assert problems(answer=answer) == []


@pytest.mark.parametrize("text, kind", [
    ("DB 접속은 password=Abc123! 로 한다.", "비밀번호"),
    ("비밀번호: hunter2 를 쓴다.", "비밀번호"),
    ("키는 AKIAABCDEFGHIJKLMNOP 이다.", "접근 키"),
    ("-----BEGIN RSA PRIVATE KEY----- 로 시작한다.", "개인 키"),
    ("운영 DB 는 10.20.30.40 에 있다.", "IP 주소"),
    ("담당은 hong@example.com 이다.", "이메일"),
    ("연락처는 010-1234-5678로 한다.", "전화번호"),
    ("주민번호 900101-1234567 형식", "주민등록번호"),
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
    assert any("비밀번호" in m for m in problems(answer="password=<pw>abc 로 한다."))


def test_sensitive_message_points_line_and_masks_value():
    msgs = problems(answer="로그인 방법은 아래와 같다.\n- 명령은 docker login --docker-password=hunter2 로 한다")
    assert any(m.startswith("답변 2번째 줄에 비밀번호·토큰 값(password=hu…)") for m in msgs), msgs
    assert not any("hunter2" in m for m in msgs)
    (q,) = problems(question="10.1.1.1 서버는 무엇인가")
    assert q.startswith("질문에 IP 주소(10.…)")
    with pytest.raises(ValidationError) as e:
        check_claude_qa(answer="담당은 hong@example.com 이다.")
    assert "답변 1번째 줄에 이메일 주소(ho…)" in e.value.message and "example" not in e.value.message


def test_question_and_note_evidence_are_checked():
    assert any("IP 주소" in m for m in problems(question="10.1.1.1 서버는 무엇인가"))
    assert any("이메일" in m for m in problems(evidence=[{"kind": "note", "text": "a@b.co 가 확인"}]))
    assert any("비밀번호" in m for m in problems(evidence=[{"kind": "url", "url": "https://x/?token=abc"}]))
    ev = [{"kind": "auto", "path": "D:/repo/app.env", "anchor": "api_key=sk-live-123"}]
    assert any(m.startswith("근거에 비밀번호·토큰 값(api_key=sk…)") for m in problems(evidence=ev))
    assert problems(evidence=[{"kind": "auto", "path": "D:/repo/app.env", "anchor": "api_key=<key>"}]) == []


def test_question_mark_is_not_required():
    assert problems(question="fast-question 검색 계층의 순서", answer="E → Y → M 순이다.") == []


def test_check_raises_validation_error_with_all_problems():
    with pytest.raises(ValidationError) as e:
        check_claude_qa(question="10.1.1.1 은?", answer="하나다. 둘이다. 셋이다.")
    assert "IP 주소" in e.value.message and "3문장" in e.value.message and "rules.md" in e.value.message


def test_rules_md_matches_code_limits():
    text = RULES_MD.read_text(encoding="utf-8")
    assert f"{QUESTION_MAX}자" in text and f"{ANSWER_MAX}자" in text
    assert re.search(rf"1~{ANSWER_MAX_SENTENCES}문장", text)
