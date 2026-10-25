"""Claude 가 MCP 로 쓰는 Q&A 의 형식 검사. 규칙 원본은 skill/qa/rules.md 다.

길이·줄 수는 kb.text 가 모든 쓰기 경로에서 검사한다. 여기 검사는 사람이 검토하지 않는 Claude 등록분에만 건다.
웹 화면(사람)의 편집은 이 검사를 받지 않는다.
"""
import re

from kb.errors import ValidationError

ANSWER_MAX_SENTENCES = 2  # 첫 줄(핵심 답)의 문장 수
LINE_MAX = 120  # 답변 한 줄의 글자 수. 넘으면 화면에서 덩어리로 보인다
ITEM_MARK = "- "  # 둘째 줄부터의 항목 머리

# 마침표·느낌표·물음표 뒤가 공백이나 끝일 때 문장 끝으로 센다. 2.5초, web.log, 말줄임(...)은 세지 않는다
_SENTENCE_END = re.compile(r"(?<=[^\s.])[.!?](?=\s|$)")

# 답에 넣지 않는 표현. 사실(지금 동작)만 남기게 한다
BANNED = [
    ("결정 경위(날짜와 결정·승인)", re.compile(r"\d{4}-\d{2}-\d{2}.{0,40}?(결정|승인|합의)|(결정|승인|합의).{0,40}?\d{4}-\d{2}-\d{2}"),
     "지금 동작과 그 이유만 써 주세요. 누가 언제 정했는지는 쓰지 마세요."),
    ("문서 절 번호(§)", re.compile(r"§\s*\d"), "읽는 사람이 그 문서를 찾을 수 없습니다. 내용을 풀어 써 주세요."),
    ("추정 표현", re.compile(r"추정(된다|이다|함)|것 같다|듯하다|것으로 보인다|확인(이)? 필요"),
     "확인한 사실만 써 주세요. 확인하지 못했으면 등록하지 마세요."),
    ("할 일·진행 상태", re.compile(r"TODO|미착수|할 일"), "진행 상태는 등록하지 마세요."),
]

_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")

SENSITIVE = [
    ("비밀번호·토큰 값", re.compile(
        r"(?i)(?:\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)|비밀번호|패스워드)\s*[:=]\s*"
        r"(?P<val><[^<>\n]+>|\S+)")),
    ("개인 키", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS 접근 키", re.compile(r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])")),
    ("JWT 토큰", re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}")),
    ("주민등록번호", re.compile(r"(?<!\d)\d{6}-[1-4]\d{6}(?!\d)")),
    ("전화번호", re.compile(r"(?<!\d)01[016789]-?\d{3,4}-?\d{4}(?!\d)")),
    ("이메일 주소", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
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
        out.append(("IP 주소", m.group(1) + ".…"))
        break
    return out


def _sensitive(text: str, where: str, numbered: bool = False) -> list[str]:
    """numbered 면 위치에 줄 번호를 붙인다. 번호는 답변 양식 검사처럼 빈 줄을 빼고 센다."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = []
    for i, ln in enumerate(lines, 1):
        at = f"{where} {i}번째 줄" if numbered else where
        out += [f"{at}에 {kind}({shown})이(가) 들어 있습니다. 값 대신 그 값이 적힌 위치(파일·시트)를 써 주세요."
                for kind, shown in _hits(ln)]
    return out


def _answer_form(answer: str) -> list[str]:
    """첫 줄은 질문에 바로 답하는 1~2문장, 둘째 줄부터는 '- ' 로 시작하는 한 사실씩의 개조체 항목."""
    lines = [ln.strip() for ln in answer.splitlines() if ln.strip()]
    out = []
    head, items = lines[0], lines[1:]
    if head.startswith(ITEM_MARK):
        out.append("답변 첫 줄은 질문에 바로 답하는 문장으로 써 주세요. 항목('- ')은 둘째 줄부터 씁니다.")
    n = sentence_count(head)
    if n > ANSWER_MAX_SENTENCES:
        out.append(f"답변 첫 줄은 1~{ANSWER_MAX_SENTENCES}문장으로 써 주세요. 지금 {n}문장입니다. 나머지는 둘째 줄부터 '- ' 항목으로 나눠 주세요.")
    for i, ln in enumerate(lines, 1):
        if len(ln) > LINE_MAX:
            out.append(f"답변 {i}번째 줄이 {len(ln)}자입니다. 한 줄은 {LINE_MAX}자까지 쓰고 넘치면 '- ' 항목으로 나눠 주세요.")
    for i, ln in enumerate(items, 2):
        if not ln.startswith(ITEM_MARK):
            out.append(f"답변 {i}번째 줄은 '- ' 로 시작하는 항목으로 써 주세요.")
        elif sentence_count(ln) > 1 or ln.endswith("."):
            out.append(f"답변 {i}번째 줄은 한 사실만 개조체로 써 주세요(마침표 없이).")
    return out


def problems(question: str | None = None, answer: str | None = None,
             evidence: list[dict] | None = None) -> list[str]:
    out: list[str] = []
    if question:
        out += _sensitive(question, "질문")
    if answer:
        out += _answer_form(answer)
        out += [f"답변에 {kind}이(가) 있습니다. {fix}" for kind, rx, fix in BANNED if rx.search(answer)]
        out += _sensitive(answer, "답변", numbered=True)
    for e in evidence or []:
        text = " ".join(str(e.get(k, "")) for k in ("text", "url", "anchor"))
        if text.strip():
            out += _sensitive(text, "근거")
    return out


def check_claude_qa(question: str | None = None, answer: str | None = None,
                    evidence: list[dict] | None = None) -> None:
    found = problems(question, answer, evidence)
    if found:
        raise ValidationError("형식 규칙(skill/qa/rules.md) 위반: " + " / ".join(found))
