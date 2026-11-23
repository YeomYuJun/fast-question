"""입력 문자열 정규화와 길이·줄 수 검사. DB 제약과 같은 값을 쓰고, 위반 시 현재 값을 알려 준다.

질문·답변 길이는 가중 길이(weight)로 센다. DB CHECK 는 같은 숫자를 글자 수 상한으로 둔다. 글자 수가 가중 길이 이하라
가중 상한을 지키면 DB 상한도 지킨다.
"""
import re
import unicodedata

from kb.errors import ValidationError

QUESTION_MAX = 300  # 가중 길이
ANSWER_MAX = 800  # 가중 길이
ANSWER_MAX_LINES = 6  # 첫 줄 = 핵심 답, 나머지 = '- ' 항목 (kb.rules 가 양식을 본다)
NAME_MAX = 60
SIMILAR_MIN = 0.6  # 질문 2글자 조각 겹침(dice)이 이 값 이상이면 비슷한 질문이다
# 한글이 없는 두 질문은 단어 안의 2글자 조각으로 비교하고, 이 값 이상이거나 대소문자·공백·끝 물음표만 다르면 등록을
# 막는다. SIMILAR_MIN 이상 이 값 미만은 등록하고 후보만 알린다. 한 단어만 다른 영어 질문이 0.6~0.86 에 몰려
# 0.6 에서 막으면 다른 사실을 묻는 질문도 force 로 다시 불러야 한다(tests/test_similar_en.py 표본)
BLOCK_MIN_NO_HANGUL = 0.9

# 가중 길이 2 로 세는 범위(한글 자모·음절, CJK, 전각). 웹 화면의 JS 도 같은 표를 쓴다
WIDE_RANGES = (
    (0x1100, 0x115F), (0x2E80, 0xA4CF), (0xAC00, 0xD7A3), (0xF900, 0xFAFF),
    (0xFE30, 0xFE4F), (0xFF00, 0xFF60), (0xFFE0, 0xFFE6),
)
_WEIGHT_NOTE = "Length is weighted: Hangul, CJK and full-width characters count 2, other characters 1."
_HANGUL = re.compile(r"[ᄀ-ᇿ㄰-㆏가-힣]")
_WORD = re.compile(r"[a-z0-9]+")


def weight(s: str) -> int:
    """코드 포인트마다 WIDE_RANGES 안이면 2, 밖이면 1 을 더한다."""
    return sum(2 if any(lo <= ord(c) <= hi for lo, hi in WIDE_RANGES) else 1 for c in s)


def bigrams(text: str) -> set[str]:
    s = "".join(text.lower().split())
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def dice(a: set[str], b: set[str]) -> float:
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


def word_bigrams(text: str) -> set[str]:
    return {w[i:i + 2] for w in _WORD.findall(text.lower()) for i in range(max(1, len(w) - 1))} or bigrams(text)


def _plain(s: str) -> str:
    return " ".join(s.lower().split()).rstrip("?").rstrip()


def similar_level(a: str, b: str) -> tuple[float, str | None]:
    """두 질문의 (겹침, "block" | "notice" | None). 둘 중 하나라도 한글이 있으면 2글자 조각이 SIMILAR_MIN 이상일 때 block."""
    if _HANGUL.search(a) or _HANGUL.search(b):
        score = dice(bigrams(a), bigrams(b))
        return score, "block" if score >= SIMILAR_MIN else None
    score = dice(word_bigrams(a), word_bigrams(b))
    if score >= BLOCK_MIN_NO_HANGUL or _plain(a) == _plain(b):
        return score, "block"
    return score, "notice" if score >= SIMILAR_MIN else None


def clean(s: str) -> str:
    if not isinstance(s, str):
        raise ValidationError("A string is required.")
    s = unicodedata.normalize("NFC", s).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in s.split("\n")).strip()


def _non_empty(s: str, what: str) -> str:
    if not s:
        raise ValidationError(f"The {what} is empty.")
    return s


def check_question(s: str) -> str:
    s = _non_empty(clean(s), "question")
    if "\n" in s:
        raise ValidationError(f"Write the question on one line. It has {s.count(chr(10)) + 1} lines now.",
                              code="qa_question_line")
    if (w := weight(s)) > QUESTION_MAX:
        raise ValidationError(f"The question can be up to {QUESTION_MAX} long. It is {w} now. {_WEIGHT_NOTE}",
                              code="qa_question_len")
    return s


def check_answer(s: str) -> str:
    s = clean(s)
    s = "\n".join(line for line in s.split("\n") if line.strip())
    s = _non_empty(s, "answer")
    lines = s.count("\n") + 1
    if lines > ANSWER_MAX_LINES:
        raise ValidationError(f"The answer can have up to {ANSWER_MAX_LINES} lines. It has {lines} lines now.",
                              code="qa_answer_lines")
    if (w := weight(s)) > ANSWER_MAX:
        raise ValidationError(f"The answer can be up to {ANSWER_MAX} long. It is {w} now. {_WEIGHT_NOTE}",
                              code="qa_answer_len")
    return s


def check_name(s: str) -> str:
    s = _non_empty(clean(s), "name")
    if "\n" in s:
        raise ValidationError("Write the name on one line.", code="category_name_len")
    if len(s) > NAME_MAX:
        raise ValidationError(f"The name can be up to {NAME_MAX} characters. It has {len(s)} characters now.",
                              code="category_name_len")
    if ">" in s:  # "대분류 > 중분류" 경로 구분자와 겹친다
        raise ValidationError("A category name cannot contain >.")
    return s
