"""입력 문자열 정규화와 길이·줄 수 검사. DB 제약과 같은 값을 쓰고, 위반 시 현재 값을 알려 준다."""
import unicodedata

from kb.errors import ValidationError

QUESTION_MAX = 150
ANSWER_MAX = 400
ANSWER_MAX_LINES = 6  # 첫 줄 = 핵심 답, 나머지 = '- ' 항목 (kb.rules 가 양식을 본다)
NAME_MAX = 60
SIMILAR_MIN = 0.6  # 질문 2글자 조각 겹침(dice)이 이 값 이상이면 비슷한 질문이다


def bigrams(text: str) -> set[str]:
    s = "".join(text.lower().split())
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def dice(a: set[str], b: set[str]) -> float:
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


def clean(s: str) -> str:
    if not isinstance(s, str):
        raise ValidationError("문자열이 필요합니다.")
    s = unicodedata.normalize("NFC", s).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in s.split("\n")).strip()


def _non_empty(s: str, what: str) -> str:
    if not s:
        raise ValidationError(f"{what}이(가) 비어 있습니다.")
    return s


def check_question(s: str) -> str:
    s = _non_empty(clean(s), "질문")
    if "\n" in s:
        raise ValidationError(f"질문은 한 줄로 써 주세요. 지금 {s.count(chr(10)) + 1}줄입니다.")
    if len(s) > QUESTION_MAX:
        raise ValidationError(f"질문은 {QUESTION_MAX}자까지 쓸 수 있습니다. 지금 {len(s)}자입니다.")
    return s


def check_answer(s: str) -> str:
    s = clean(s)
    s = "\n".join(line for line in s.split("\n") if line.strip())
    s = _non_empty(s, "답변")
    lines = s.count("\n") + 1
    if lines > ANSWER_MAX_LINES:
        raise ValidationError(f"답변은 {ANSWER_MAX_LINES}줄까지 쓸 수 있습니다. 지금 {lines}줄입니다.")
    if len(s) > ANSWER_MAX:
        raise ValidationError(f"답변은 {ANSWER_MAX}자까지 쓸 수 있습니다. 지금 {len(s)}자입니다.")
    return s


def check_name(s: str) -> str:
    s = _non_empty(clean(s), "이름")
    if "\n" in s:
        raise ValidationError("이름은 한 줄로 써 주세요.")
    if len(s) > NAME_MAX:
        raise ValidationError(f"이름은 {NAME_MAX}자까지 쓸 수 있습니다. 지금 {len(s)}자입니다.")
    if ">" in s:  # "대분류 > 중분류" 경로 구분자와 겹친다
        raise ValidationError("분류 이름에는 > 를 쓸 수 없습니다.")
    return s
