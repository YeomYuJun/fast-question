"""Claude 가 MCP 로 쓰는 Q&A 의 형식 검사. 규칙 원본은 skill/qa/rules.md 다.

길이·줄 수는 kb.text 가 모든 쓰기 경로에서 검사한다. 여기 검사는 사람이 검토하지 않는 Claude 등록분에만 건다.
웹 화면(사람)의 편집은 이 검사를 받지 않는다.
"""
import re

from kb.errors import ValidationError

ANSWER_MAX_SENTENCES = 2

# 마침표·느낌표·물음표 뒤가 공백이나 끝일 때 문장 끝으로 센다. 2.5초, web.log, 말줄임(...)은 세지 않는다
_SENTENCE_END = re.compile(r"(?<=[^\s.])[.!?](?=\s|$)")

_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")

SENSITIVE = [
    ("비밀번호·토큰 값", re.compile(
        r"(?i)(?:\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)|비밀번호|패스워드)\s*[:=]\s*\S+")),
    ("개인 키", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS 접근 키", re.compile(r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])")),
    ("JWT 토큰", re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}")),
    ("주민등록번호", re.compile(r"(?<!\d)\d{6}-[1-4]\d{6}(?!\d)")),
    ("전화번호", re.compile(r"(?<!\d)01[016789]-?\d{3,4}-?\d{4}(?!\d)")),
    ("이메일 주소", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
]


def _has_ip(text: str) -> bool:
    for m in _IPV4.finditer(text):
        parts = [int(p) for p in m.groups()]
        if max(parts) > 255 or parts[0] == 127 or parts == [0, 0, 0, 0]:
            continue  # 루프백·전체 바인딩 주소는 내부 망 값이 아니다
        return True
    return False


def sentence_count(text: str) -> int:
    return len(_SENTENCE_END.findall(text.strip()))


def _sensitive(text: str, where: str) -> list[str]:
    kinds = [kind for kind, rx in SENSITIVE if rx.search(text)]
    if _has_ip(text):
        kinds.append("IP 주소")
    return [f"{where}에 {k}이(가) 들어 있다. 값 대신 그 값이 적힌 위치(파일·시트)를 쓴다." for k in kinds]


def problems(question: str | None = None, answer: str | None = None,
             evidence: list[dict] | None = None) -> list[str]:
    out: list[str] = []
    if question:
        out += _sensitive(question, "질문")
    if answer:
        n = sentence_count(answer)
        if n > ANSWER_MAX_SENTENCES:
            out.append(f"답변은 1~{ANSWER_MAX_SENTENCES}문장으로 쓴다. 지금 {n}문장이다.")
        out += _sensitive(answer, "답변")
    for e in evidence or []:
        text = " ".join(str(e.get(k, "")) for k in ("text", "url"))
        if text.strip():
            out += _sensitive(text, "근거")
    return out


def check_claude_qa(question: str | None = None, answer: str | None = None,
                    evidence: list[dict] | None = None) -> None:
    found = problems(question, answer, evidence)
    if found:
        raise ValidationError("형식 규칙(skill/qa/rules.md) 위반: " + " / ".join(found))
