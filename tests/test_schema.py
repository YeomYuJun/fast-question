import json
import sqlite3
import time
import unicodedata

import pytest

from kb.db import connect, get_meta, set_meta
from kb.errors import ValidationError, from_sqlite
from kb.text import check_answer, check_name, check_question


BUMP = "version = version + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def add_cat(conn, name, parent=None, actor="user"):
    cur = conn.execute(
        "INSERT INTO category(name, parent_id, created_by, updated_by) VALUES (?,?,?,?)",
        (name, parent, actor, actor),
    )
    return cur.lastrowid


def add_qa(conn, cat, q="질문", a="답변", actor="user"):
    cur = conn.execute(
        "INSERT INTO qa(category_id, question, answer, created_by, updated_by) VALUES (?,?,?,?,?)",
        (cat, q, a, actor, actor),
    )
    return cur.lastrowid


def row(conn, table, id_):
    return conn.execute(f"SELECT * FROM {table} WHERE id = ?", (id_,)).fetchone()


def test_update_sets_time_and_version_by_db(conn):
    cid = add_cat(conn, "가")
    before = row(conn, "category", cid)
    time.sleep(0.01)

    conn.execute("UPDATE category SET name = '나', updated_by = 'claude', version = version + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?", (cid,))

    after = row(conn, "category", cid)
    assert after["version"] == before["version"] + 1
    assert after["updated_at"] > before["updated_at"]
    assert after["created_at"] == before["created_at"]


@pytest.mark.parametrize(
    "sets",
    [
        "updated_at = '2000-01-01'",
        "name = '나'",  # 버전·시각을 올리지 않은 수정
        "name = '나', version = version + 1, updated_at = '2000-01-01T00:00:00.000Z'",  # 시각 위조
        "name = '나', version = version + 2, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        f"created_by = 'claude', {BUMP}",
    ],
)
def test_unmanaged_update_rejected(conn, sets):
    cid = add_cat(conn, "가")
    with pytest.raises(sqlite3.IntegrityError, match="managed_column"):
        conn.execute(f"UPDATE category SET {sets} WHERE id = ?", (cid,))


def test_rapid_updates_within_same_millisecond(conn):
    cid = add_cat(conn, "가")
    for i in range(300):
        conn.execute(f"UPDATE category SET name = ?, updated_by = 'user', {BUMP} WHERE id = ?", (f"이름{i}", cid))
    assert row(conn, "category", cid)["version"] == 301
    n = conn.execute("SELECT count(*) FROM revision WHERE table_name = 'category' AND row_id = ?", (cid,)).fetchone()[0]
    assert n == 301


def test_revision_records_insert_and_update_with_actor(conn):
    cid = add_cat(conn, "가", actor="claude")
    conn.execute("UPDATE category SET name = '나', updated_by = 'user', change_note = '오타', version = version + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?", (cid,))

    revs = conn.execute(
        "SELECT op, actor, before_json, after_json FROM revision WHERE table_name = 'category' AND row_id = ? ORDER BY id",
        (cid,),
    ).fetchall()
    assert [(r["op"], r["actor"]) for r in revs] == [("insert", "claude"), ("update", "user")]
    assert json.loads(revs[1]["before_json"])["name"] == "가"
    after = json.loads(revs[1]["after_json"])
    assert after["name"] == "나"
    assert after["change_note"] == "오타"


def test_archive_and_restore_ops(conn):
    cid = add_cat(conn, "가")
    qid = add_qa(conn, cid)
    conn.execute(
        "UPDATE qa SET deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), deleted_by = 'user', updated_by = 'user', version = version + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
        (qid,),
    )
    conn.execute("UPDATE qa SET deleted_at = NULL, deleted_by = NULL, updated_by = 'claude', version = version + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?", (qid,))

    ops = [r["op"] for r in conn.execute("SELECT op FROM revision WHERE table_name = 'qa' ORDER BY id")]
    assert ops == ["insert", "archive", "restore"]


def test_revision_is_append_only(conn):
    add_cat(conn, "가")
    with pytest.raises(sqlite3.IntegrityError, match="revision_append_only"):
        conn.execute("UPDATE revision SET actor = 'user'")
    with pytest.raises(sqlite3.IntegrityError, match="revision_append_only"):
        conn.execute("DELETE FROM revision")


@pytest.mark.parametrize("table", ["category", "qa", "evidence", "qa_link", "synonym", "user_word", "stopword"])
def test_physical_delete_rejected(conn, table):
    cid = add_cat(conn, "가")
    qid = add_qa(conn, cid)
    qid2 = add_qa(conn, cid, q="다른 질문")
    conn.execute("INSERT INTO evidence(qa_id, kind, created_by, updated_by) VALUES (?, 'note', 'user', 'user')", (qid,))
    conn.execute("INSERT INTO qa_link(a_id, b_id, created_by, updated_by) VALUES (?, ?, 'user', 'user')", (qid, qid2))
    conn.execute("INSERT INTO synonym(group_id, term, created_by, updated_by) VALUES (1, '룰', 'user', 'user')")
    conn.execute("INSERT INTO user_word(word, created_by, updated_by) VALUES ('망연계', 'user', 'user')")
    with pytest.raises(sqlite3.IntegrityError, match="no_physical_delete"):
        conn.execute(f"DELETE FROM {table}")


def test_category_depth_limited_to_two(conn):
    top = add_cat(conn, "대")
    mid = add_cat(conn, "중", parent=top)
    with pytest.raises(sqlite3.IntegrityError, match="category_depth"):
        add_cat(conn, "소", parent=mid)
    other = add_cat(conn, "다른 대")
    with pytest.raises(sqlite3.IntegrityError, match="category_depth"):
        conn.execute("UPDATE category SET parent_id = ?, updated_by = 'user', version = version + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?", (other, top))


def test_category_name_unique_under_same_parent(conn):
    top = add_cat(conn, "대")
    add_cat(conn, "중", parent=top)
    add_cat(conn, "중")  # 다른 부모(최상위)라 허용
    with pytest.raises(sqlite3.IntegrityError, match="category_name_uq"):
        add_cat(conn, "중", parent=top)


@pytest.mark.parametrize(
    "question, answer, name",
    [
        ("한\n줄", "답", "qa_question_line"),
        ("가" * 151, "답", "qa_question_len"),
        ("질문", "1\n2\n3", "qa_answer_lines"),
        ("질문", "가" * 401, "qa_answer_len"),
    ],
)
def test_qa_constraints(conn, question, answer, name):
    cid = add_cat(conn, "가")
    with pytest.raises(sqlite3.IntegrityError, match=name):
        add_qa(conn, cid, question, answer)


def test_fts_follows_question_update(conn):
    cid = add_cat(conn, "가")
    qid = add_qa(conn, cid, q="원래 질문 내용", a="답변 내용")
    conn.execute("UPDATE qa SET question = '바뀐 질문 내용', updated_by = 'user', version = version + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?", (qid,))

    hit = lambda t: [r[0] for r in conn.execute("SELECT rowid FROM qa_fts WHERE qa_fts MATCH ?", (f'"{t}"',))]
    assert hit("바뀐 질") == [qid]
    assert hit("원래 질") == []


def test_fts_ns_columns_drop_spaces(conn):
    cid = add_cat(conn, "가")
    qid = add_qa(conn, cid, q="여유 공간 검사", a="볼륨 용량")
    r = conn.execute("SELECT question_ns FROM qa_fts WHERE rowid = ?", (qid,)).fetchone()
    assert r[0] == "여유공간검사"


def test_from_sqlite_translates_constraint_names(conn):
    cid = add_cat(conn, "가")
    with pytest.raises(sqlite3.IntegrityError) as ei:
        add_qa(conn, cid, "질문", "1\n2\n3")
    err = from_sqlite(ei.value)
    assert err.code == "qa_answer_lines"
    assert "2줄" in err.message


def test_stopwords_seeded_once(tmp_path):
    path = tmp_path / "kb.sqlite3"
    connect(path).close()
    c = connect(path)
    words = sorted(r[0] for r in c.execute("SELECT word FROM stopword"))
    assert words == ["경우", "때"]
    assert get_meta(c, "schema_version") == "1"
    c.close()


def test_connect_existing_db_takes_no_write_lock(tmp_path):
    path = tmp_path / "kb.sqlite3"
    holder = connect(path)
    holder.execute("BEGIN IMMEDIATE")  # 다른 연결이 쓰기 잠금을 쥔 상태
    t0 = time.perf_counter()
    c = connect(path)
    assert time.perf_counter() - t0 < 1.0
    assert get_meta(c, "schema_version") == "1"
    holder.execute("ROLLBACK")
    c.close()
    holder.close()


def test_meta_roundtrip(conn):
    set_meta(conn, "x", "1")
    set_meta(conn, "x", "2")
    assert get_meta(conn, "x") == "2"
    assert get_meta(conn, "없음", "기본") == "기본"


def test_text_checks():
    nfd = unicodedata.normalize("NFD", "용량")
    assert check_question(f"  {nfd} 질문  ") == "용량 질문"
    assert check_answer("첫 줄\r\n\r\n둘째 줄  ") == "첫 줄\n둘째 줄"
    with pytest.raises(ValidationError, match="지금 3줄"):
        check_answer("1\n2\n3")
    with pytest.raises(ValidationError, match="한 줄"):
        check_question("가\n나")
    with pytest.raises(ValidationError, match="지금 151자"):
        check_question("가" * 151)
    with pytest.raises(ValidationError, match="비어"):
        check_name("   ")
