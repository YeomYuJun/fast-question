"""동의어, 불용어, 사용자 단어 사전.

- 동의어와 불용어는 검색할 때만 쓴다. 바꿔도 재색인하지 않는다.
- 사용자 단어는 형태소 분석 결과를 바꾸므로 재색인이 필요하다.
쓰기는 호출자(Store)가 연 트랜잭션 안에서 한다.
"""
import sqlite3

from kb.errors import ValidationError
from kb.schema import BUMP, NOW
from kb.text import clean


def load_synonyms(conn: sqlite3.Connection) -> dict[str, list[str]]:
    groups: dict[int, list[str]] = {}
    for r in conn.execute("SELECT group_id, term FROM synonym WHERE deleted_at IS NULL ORDER BY id"):
        groups.setdefault(r[0], []).append(r[1])
    return {t.lower(): terms for terms in groups.values() for t in terms}


def load_stopwords(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT word FROM stopword WHERE deleted_at IS NULL")}


def load_user_words(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT word, tag FROM user_word WHERE deleted_at IS NULL ORDER BY word, tag")]


def snapshot(conn: sqlite3.Connection) -> dict:
    groups: dict[int, list[dict]] = {}
    for r in conn.execute("SELECT id, group_id, term FROM synonym WHERE deleted_at IS NULL ORDER BY id"):
        groups.setdefault(r["group_id"], []).append({"id": r["id"], "term": r["term"]})
    return {
        "synonyms": [{"group_id": g, "terms": terms} for g, terms in sorted(groups.items())],
        "stopwords": [{"id": r["id"], "word": r["word"]} for r in conn.execute(
            "SELECT id, word FROM stopword WHERE deleted_at IS NULL ORDER BY word")],
        "user_words": [{"id": r["id"], "word": r["word"], "tag": r["tag"]} for r in conn.execute(
            "SELECT id, word, tag FROM user_word WHERE deleted_at IS NULL ORDER BY word")],
    }


def _word(value: str, what: str) -> str:
    w = clean(value or "")
    if not w:
        raise ValidationError(f"{what}이(가) 비어 있다.")
    return w


def _archive(conn: sqlite3.Connection, table: str, id: int, actor: str) -> None:
    n = conn.execute(f"UPDATE {table} SET deleted_at = {NOW}, deleted_by = ?, updated_by = ?, {BUMP} "
                     "WHERE id = ? AND deleted_at IS NULL", (actor, actor, id)).rowcount
    if n == 0:
        raise ValidationError(f"사전 항목을 찾지 못했다: {table} {id}")


def apply(conn: sqlite3.Connection, actor: str, action: str, **kw) -> bool:
    """사전 동작을 실행하고 재색인이 필요한지 돌려준다."""
    ins = "INSERT INTO {t}({cols}, created_by, updated_by) VALUES ({marks}, ?, ?)"
    if action == "synonym_add":
        terms = list(dict.fromkeys(_word(t, "동의어") for t in kw.get("terms") or []))
        group_id = kw.get("group_id")
        if group_id is None:
            if len(terms) < 2:
                raise ValidationError("새 동의어 그룹은 단어가 2개 이상이어야 한다.")
            group_id = conn.execute("SELECT ifnull(max(group_id), 0) + 1 FROM synonym").fetchone()[0]
        else:
            if not conn.execute("SELECT 1 FROM synonym WHERE group_id = ? AND deleted_at IS NULL",
                                (group_id,)).fetchone():
                raise ValidationError(f"동의어 그룹을 찾지 못했다: {group_id}")
            if not terms:
                raise ValidationError("추가할 동의어가 없다.")
        for t in terms:
            conn.execute(ins.format(t="synonym", cols="group_id, term", marks="?, ?"),
                         (group_id, t, actor, actor))
        return False
    if action == "synonym_remove":
        _archive(conn, "synonym", int(kw["id"]), actor)
        return False
    if action == "stopword_add":
        conn.execute(ins.format(t="stopword", cols="word", marks="?"), (_word(kw.get("word"), "불용어"), actor, actor))
        return False
    if action == "stopword_remove":
        _archive(conn, "stopword", int(kw["id"]), actor)
        return False
    if action in ("user_word_add", "user_word_remove"):
        if action == "user_word_add":
            conn.execute(ins.format(t="user_word", cols="word, tag", marks="?, ?"),
                         (_word(kw.get("word"), "사용자 단어"), kw.get("tag") or "NNP", actor, actor))
        else:
            _archive(conn, "user_word", int(kw["id"]), actor)
        from kb.morph import compute_version  # morph 가 이 모듈을 불러오므로 함수 안에서 불러온다
        conn.execute("INSERT INTO meta(key, value) VALUES ('analyzer_version', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (compute_version(load_user_words(conn)),))
        return True
    raise ValidationError(f"알 수 없는 사전 동작이다: {action}")
