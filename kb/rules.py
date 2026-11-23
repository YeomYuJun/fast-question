"""Claude 가 MCP 로 쓰는 Q&A 의 형식 검사. 규칙 원본은 skill/qa/rules.md 다.

길이·줄 수는 kb.text 가 모든 쓰기 경로에서 검사한다. 여기 검사는 사람이 검토하지 않는 Claude 등록분에만 건다.
웹 화면(사람)의 편집은 이 검사를 받지 않는다.
"""
import re

from kb.errors import ValidationError
from kb.text import weight

ANSWER_MAX_SENTENCES = 2  # 첫 줄(핵심 답)의 문장 수
LINE_MAX = 240  # 답변 한 줄의 가중 길이. 넘으면 화면에서 덩어리로 보인다
ITEM_MARK = "- "  # 둘째 줄부터의 항목 머리

# 마침표·느낌표·물음표 뒤가 공백이나 끝일 때 문장 끝으로 센다. 2.5초, web.log, 말줄임(...)은 세지 않는다
_SENTENCE_END = re.compile(r"(?<=[^\s.])[.!?](?=\s|$)")

_DATE = r"\d{4}-\d{2}-\d{2}"
_DECIDED_EN = r"(?i:\b(?:decided|approved|agreed)\b)"

# 답에 넣지 않는 표현. 사실(지금 동작)만 남기게 한다
BANNED = [
    ("decision history (a date with who decided or approved)", re.compile(
        rf"{_DATE}.{{0,40}}?(결정|승인|합의)|(결정|승인|합의).{{0,40}}?{_DATE}"
        rf"|{_DATE}.{{0,40}}?{_DECIDED_EN}|{_DECIDED_EN}.{{0,40}}?{_DATE}"),
     "Write only the current behavior and its reason, not who decided it or when."),
    ("a document section number (§)", re.compile(r"§\s*\d"),
     "The reader cannot find that document. Write out the content instead."),
    ("speculation", re.compile(
        r"추정(된다|이다|함)|것 같다|듯하다|것으로 보인다|확인(이)? 필요"
        # 화자의 추측을 직접 나타내는 표현만 건다. might be, not verified 같은 조동사·부정형은 정상 사실 문장에도 쓰인다
        r"|(?i:\b(?:probably|presumably|seems? to|I think|I guess|not sure)\b"
        r"|\bappears? to\b(?! (?:the|a|an|users?|everyone|admins?)\b))"),
     "Write only facts you have checked. If you could not check it, do not register it."),
    ("a to-do or progress status", re.compile(
        r"TODO|미착수|할 일|\bWIP\b|\bTBD\b|(?i:\bwork in progress\b)"),
     "Do not register progress status."),
]

_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")

SENSITIVE = [
    ("a password or token value", re.compile(
        r"(?i)(?:\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)|비밀번호|패스워드)\s*[:=]\s*"
        r"(?P<val><[^<>\n]+>|\S+)")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("an AWS access key", re.compile(r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])")),
    ("a JWT", re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}")),
    ("a Korean resident registration number", re.compile(r"(?<!\d)\d{6}-[1-4]\d{6}(?!\d)")),
    ("a phone number", re.compile(
        r"(?<!\d)01[016789]-?\d{3,4}-?\d{4}(?!\d)"
        r"|(?<![\w.+-])(?:\+1[ .-]?)?(?:\([2-9]\d{2}\) ?|[2-9]\d{2}[ .-])\d{3}[ .-]\d{4}(?![\w.-])"
        r"|(?<![\w+])\+\d{1,3}[ .-]?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,4}(?!\d)")),
    ("an email address", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
]


# 값 자리에 <pw>, <your token> 처럼 꺾쇠로 감싼 자리표시자만 있으면 실제 값이 아니다
_PLACEHOLDER = re.compile(r"<[^<>\n]+>")
SHOW = 2  # 걸린 값에서 보여 주는 앞 글자 수. 나머지는 가린다


def _ips(text: str):
    for m in _IPV4.finditer(text):
        parts = [int(p) for p in m.groups()]
        if max(parts) > 255 or parts[0] == 127 or parts == [0, 0, 0, 0]:
            continue  # 루프백·전체 바인딩 주소는 내부 망 값이 아니다
        yield m


def sentence_count(text: str) -> int:
    return len(_SENTENCE_END.findall(text.strip()))


def _hits(line: str) -> list[tuple[str, str]]:
    """한 줄에서 걸린 (종류, 앞 몇 글자만 남긴 값). 종류마다 첫 번째 것만 돌려준다."""
    out = []
    for kind, rx in SENSITIVE:
        for m in rx.finditer(line):
            val = m.groupdict().get("val")
            if val is None:
                out.append((kind, m.group(0)[:SHOW] + "…"))
                break
            nxt = line[m.end():m.end() + 1]
            if _PLACEHOLDER.fullmatch(val) and not (nxt.isalnum() or nxt == "_"):
                continue
            out.append((kind, line[m.start():m.start("val")] + val[:SHOW] + "…"))
            break
    for m in _ips(line):
        out.append(("an IP address", m.group(1) + ".…"))
        break
    return out


def _sensitive(text: str, where: str, numbered: bool = False) -> list[str]:
    """numbered 면 위치에 줄 번호를 붙인다. 번호는 답변 양식 검사처럼 빈 줄을 빼고 센다."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = []
    for i, ln in enumerate(lines, 1):
        at = f"{where} line {i}" if numbered else where
        out += [f"{at.capitalize()} contains {kind} ({shown}). "
                "Write where the value is kept (file, sheet) instead of the value."
                for kind, shown in _hits(ln)]
    return out


def _answer_form(answer: str) -> list[str]:
    """첫 줄은 질문에 바로 답하는 1~2문장, 둘째 줄부터는 '- ' 로 시작하는 한 사실씩의 개조체 항목."""
    lines = [ln.strip() for ln in answer.splitlines() if ln.strip()]
    out = []
    head, items = lines[0], lines[1:]
    if head.startswith(ITEM_MARK):
        out.append("Start the answer with a sentence that answers the question directly. "
                   "Items ('- ') start on the second line.")
    n = sentence_count(head)
    if n > ANSWER_MAX_SENTENCES:
        out.append(f"Write the first answer line in 1 to {ANSWER_MAX_SENTENCES} sentences. It has {n} sentences now. "
                   "Move the rest to '- ' items from the second line.")
    for i, ln in enumerate(lines, 1):
        if (w := weight(ln)) > LINE_MAX:
            out.append(f"Answer line {i} is {w} long. A line can be up to {LINE_MAX} "
                       "(Hangul, CJK and full-width characters count 2). Split the rest into '- ' items.")
    for i, ln in enumerate(items, 2):
        if not ln.startswith(ITEM_MARK):
            out.append(f"Answer line {i} must be an item starting with '- '.")
        elif sentence_count(ln) > 1 or ln.endswith("."):
            out.append(f"Answer line {i} must state one fact as a short item, without a final period.")
    return out


def problems(question: str | None = None, answer: str | None = None,
             evidence: list[dict] | None = None) -> list[str]:
    out: list[str] = []
    if question:
        out += _sensitive(question, "question")
    if answer:
        out += _answer_form(answer)
        out += [f"The answer contains {kind}. {fix}" for kind, rx, fix in BANNED if rx.search(answer)]
        out += _sensitive(answer, "answer", numbered=True)
    for e in evidence or []:
        text = " ".join(str(e.get(k, "")) for k in ("text", "url", "anchor"))
        if text.strip():
            out += _sensitive(text, "evidence")
    return out


def check_claude_qa(question: str | None = None, answer: str | None = None,
                    evidence: list[dict] | None = None) -> None:
    found = problems(question, answer, evidence)
    if found:
        raise ValidationError("Format rule violation (skill/qa/rules.md): " + " / ".join(found))
