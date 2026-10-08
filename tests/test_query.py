import pytest

from kb.query import Term, parse


def texts(groups):
    return [[(t.text, t.field) for t in g] for g in groups]


def test_words_are_and_groups():
    assert texts(parse("a b").groups) == [[("a", None)], [("b", None)]]


@pytest.mark.parametrize("q", ["a | b c", "a|b c", "a |b c"])
def test_bar_joins_alternatives(q):
    assert texts(parse(q).groups) == [[("a", None), ("b", None)], [("c", None)]]


def test_quoted_phrase_keeps_spaces():
    assert texts(parse('"쓸 수" 없어').groups) == [[("쓸 수", None)], [("없어", None)]]


def test_minus_excludes():
    p = parse("PETRA -제거")
    assert texts(p.groups) == [[("PETRA", None)]]
    assert p.excludes == [Term("제거", None)]


def test_field_prefixes():
    p = parse('q:NAS a:"같은 볼륨" Q:x')
    assert texts(p.groups) == [[("NAS", "q")], [("같은 볼륨", "a")], [("x", "q")]]


def test_field_applies_to_each_alternative():
    assert texts(parse("q:a|b").groups) == [[("a", "q"), ("b", "q")]]


def test_path_terms():
    p = parse(r"path:kb\search.py NAS")
    assert p.paths == [["kb/search.py"]]
    assert texts(p.groups) == [[("NAS", None)]]


@pytest.mark.parametrize("q, expected", [
    ('"', '"'), ('nas"tmp', 'nas"tmp'), ('"abc', '"abc'), ("-", "-"), ("--install", "--install"),
    ("q:", "q:"), ("path:", "path:"),
])
def test_incomplete_syntax_is_literal(q, expected):
    p = parse(q)
    assert texts(p.groups) == [[(expected, None)]] and not p.excludes and not p.paths


def test_words_lists_positive_texts():
    assert parse('a|b -c "d e" path:x').words() == ["a", "b", "d e"]


def test_plain_reports_no_operators():
    assert parse("PETRA 복호화").plain and not parse("a|b").plain and not parse("-a b").plain
    assert not parse("q:a").plain and not parse('"a b"').plain
