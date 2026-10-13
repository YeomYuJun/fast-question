"""입력 문자열 정규화와 길이·줄 수 검사. DB 제약과 같은 값을 쓰고, 위반 시 현재 값을 알려 준다."""
import unicodedata

from kb.errors import ValidationError

QUESTION_MAX = 150
ANSWER_MAX = 400
ANSWER_MAX_LINES = 6  # 첫 줄 = 핵심 답, 나머지 = '- ' 항목 (kb.rules 가 양식을 본다)
NAME_MAX = 60


def clean(s: str) -> str:
    if not isinstance(s, str):
        raise ValidationError("문자열이 필요하다.")
    s = unicodedata.normalize("NFC", s).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in s.split("\n")).strip()


def _non_empty(s: str, what: str) -> str:
    if not s:
        raise ValidationError(f"{what}이(가) 비어 있다.")
    return s


def check_question(s: str) -> str:
    s = _non_empty(clean(s), "질문")
    if "\n" in s:
        raise ValidationError(f"질문은 한 줄로 쓴다. 지금 {s.count(chr(10)) + 1}줄이다.")
    if len(s) > QUESTION_MAX:
        raise ValidationError(f"질문은 {QUESTION_MAX}자까지 쓸 수 있다. 지금 {len(s)}자다.")
    return s


def check_answer(s: str) -> str:
    s = clean(s)
    s = "\n".join(line for line in s.split("\n") if line.strip())
    s = _non_empty(s, "답변")
    lines = s.count("\n") + 1
    if lines > ANSWER_MAX_LINES:
        raise ValidationError(f"답변은 {ANSWER_MAX_LINES}줄까지 쓸 수 있다. 지금 {lines}줄이다.")
    if len(s) > ANSWER_MAX:
        raise ValidationError(f"답변은 {ANSWER_MAX}자까지 쓸 수 있다. 지금 {len(s)}자다.")
    return s


def check_name(s: str) -> str:
    s = _non_empty(clean(s), "이름")
    if "\n" in s:
        raise ValidationError("이름은 한 줄로 쓴다.")
    if len(s) > NAME_MAX:
        raise ValidationError(f"이름은 {NAME_MAX}자까지 쓸 수 있다. 지금 {len(s)}자다.")
    if ">" in s:  # "대분류 > 중분류" 경로 구분자와 겹친다
        raise ValidationError("분류 이름에는 > 를 쓸 수 없다.")
    return s
