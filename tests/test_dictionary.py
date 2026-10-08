import pytest

from kb.dictionary import load_stopwords, load_synonyms, load_user_words
from kb.errors import ValidationError
from kb.store import Store


@pytest.fixture
def user(conn):
    return Store(conn, "user")


def test_synonym_group_lifecycle(user, conn):
    res = user.dict_action("synonym_add", terms=["규칙", "룰", " rule "])
    group = res["snapshot"]["synonyms"][0]
    assert [t["term"] for t in group["terms"]] == ["규칙", "룰", "rule"]
    assert res["reindex_needed"] is False
    assert load_synonyms(conn)["rule"] == ["규칙", "룰", "rule"]

    user.dict_action("synonym_add", terms=["RULES"], group_id=group["group_id"])
    assert "RULES" in load_synonyms(conn)["규칙"]

    rule_id = next(t["id"] for t in group["terms"] if t["term"] == "rule")
    user.dict_action("synonym_remove", id=rule_id)
    assert "rule" not in load_synonyms(conn)


def test_synonym_new_group_needs_two_terms(user):
    with pytest.raises(ValidationError, match="2개 이상"):
        user.dict_action("synonym_add", terms=["룰"])


def test_synonym_term_in_two_groups_rejected(user):
    user.dict_action("synonym_add", terms=["규칙", "룰"])
    with pytest.raises(ValidationError, match="이미 다른 동의어 그룹"):
        user.dict_action("synonym_add", terms=["룰", "rule"])


def test_stopwords_default_and_edit(user, conn):
    assert load_stopwords(conn) == {"때", "경우"}
    res = user.dict_action("stopword_add", word="것")
    assert load_stopwords(conn) == {"때", "경우", "것"}
    sid = next(s["id"] for s in res["snapshot"]["stopwords"] if s["word"] == "때")
    user.dict_action("stopword_remove", id=sid)
    assert "때" not in load_stopwords(conn)


def test_user_word_requires_reindex(user, conn):
    res = user.dict_action("user_word_add", word="망연계")
    assert res["reindex_needed"] is True
    assert load_user_words(conn) == [("망연계", "NNP")]
    with pytest.raises(ValidationError, match="품사"):
        user.dict_action("user_word_add", word="칵테일", tag="ZZ")


def test_unknown_action_rejected(user):
    with pytest.raises(ValidationError, match="알 수 없는 사전 동작"):
        user.dict_action("drop_all")


def test_dictionary_changes_are_audited(user, conn):
    user.dict_action("synonym_add", terms=["규칙", "룰"])
    actors = {r[0] for r in conn.execute("SELECT actor FROM revision WHERE table_name = 'synonym'")}
    assert actors == {"user"}
