"""검색창 질의 문법. 공백으로 나눈 단어는 AND 다.

    a | b, a|b   OR (대안)
    -a           a 가 든 항목을 뺀다
    "a b"        공백을 포함한 구절
    q:a, a:a     질문에서만, 답변에서만 찾는다 (q:"a b", q:a|b 도 된다)
    path:x       근거 파일 경로에 x 가 든 항목

문법이 완성되지 않은 토큰(닫히지 않은 따옴표, - 하나, 값 없는 q:)은 글자 그대로 찾는다.
"""
from dataclasses import dataclass, field

FIELDS = {"q:": "q", "a:": "a"}
PATH = "path:"


@dataclass(frozen=True)
class Term:
    text: str
    field: str | None = None  # None 이면 질문과 답변 모두, "q" 질문, "a" 답변


@dataclass
class Query:
    groups: list[list[Term]] = field(default_factory=list)  # 그룹끼리 AND, 그룹 안 대안끼리 OR
    excludes: list[Term] = field(default_factory=list)
    paths: list[list[str]] = field(default_factory=list)
    plain: bool = True  # 연산자 없이 단어만 있는 질의

    def words(self) -> list[str]:
        return [t.text for g in self.groups for t in g]


def _tokens(q: str) -> list[tuple[str, bool]]:
    """(토큰, 따옴표 구절 여부). 따옴표는 닫는 따옴표가 있을 때만 구절로 본다."""
    out: list[tuple[str, bool]] = []
    i, n = 0, len(q)
    while i < n:
        if q[i].isspace():
            i += 1
            continue
        j = i + 1 if q[i] == "-" else i
        for pre in (*FIELDS, PATH):
            if q[j:j + len(pre)].lower() == pre:
                j += len(pre)
                break
        if j < n and q[j] == '"':
            close = q.find('"', j + 1)
            if close > j + 1:
                out.append((q[i:close + 1], True))
                i = close + 1
                continue
        k = i
        while k < n and not q[k].isspace():
            k += 1
        out.append((q[i:k], False))
        i = k
    return out


def _split(token: str, quoted: bool) -> tuple[bool, str | None, bool, list[str]]:
    """(제외 여부, 범위, 경로 여부, 대안 목록). 문법이 아니면 범위 없이 토큰 전체 하나."""
    literal = (False, None, False, [token])
    neg = token.startswith("-") and not token.startswith("--")
    body = token[1:] if neg else token
    scope, is_path = None, False
    for pre, f in FIELDS.items():
        if body[:len(pre)].lower() == pre:
            scope, body = f, body[len(pre):]
            break
    else:
        if body[:len(PATH)].lower() == PATH:
            is_path, body = True, body[len(PATH):]
    if quoted:
        alts = [body[1:-1]]
    else:
        alts = [a for a in body.split("|") if a]
    if not alts or (neg and not body):
        return literal
    return neg, scope, is_path, alts


def parse(q: str) -> Query:
    out = Query()
    join_next = False  # 앞 토큰이 | 로 끝났거나 | 단독 토큰이 왔다
    quoted_any = False
    for token, quoted in _tokens(q):
        if token == "|":
            join_next = bool(out.groups)
            continue
        quoted_any |= quoted
        neg, scope, is_path, alts = _split(token, quoted)
        if is_path:
            out.paths.append([a.replace("\\", "/").lower() for a in alts])
        elif neg:
            out.excludes += [Term(a, scope) for a in alts]
        else:
            terms = [Term(a, scope) for a in alts]
            if out.groups and (join_next or (not quoted and token.startswith("|"))):
                out.groups[-1] += terms
            else:
                out.groups.append(terms)
        join_next = not quoted and len(token) > 1 and token.endswith("|") and not (neg or is_path)
    out.plain = not (quoted_any or out.excludes or out.paths
                     or any(len(g) > 1 or g[0].field for g in out.groups))
    return out
