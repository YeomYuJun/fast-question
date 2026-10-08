import threading

import pytest

from kb.db import connect
from kb.errors import ConflictError, NotFoundError, ValidationError
from kb.store import Store


@pytest.fixture
def user(conn):
    return Store(conn, "user")


@pytest.fixture
def claude(conn):
    return Store(conn, "claude")


def test_category_tree_and_resolve(user):
    top = user.add_category("이관", description="사내 이관")
    mid = user.add_category("NAS", parent="이관")

    assert user.resolve_category("이관") == top["id"]
    assert user.resolve_category("이관 > NAS") == mid["id"]
    assert user.resolve_category(mid["id"]) == mid["id"]
    assert user.resolve_category(str(mid["id"])) == mid["id"]
    tree = user.categories()
    assert [c["name"] for c in tree] == ["이관"]
    assert [c["name"] for c in tree[0]["children"]] == ["NAS"]
    assert mid["path"] == "이관 > NAS"


def test_unknown_category_not_found(user):
    with pytest.raises(NotFoundError, match="분류를 찾지 못했다"):
        user.resolve_category("없는 분류")


def test_third_level_category_rejected_in_korean(user):
    user.add_category("대")
    user.add_category("중", parent="대")
    with pytest.raises(ValidationError, match="2단계"):
        user.add_category("소", parent="대 > 중")


def test_add_qa_records_actor_and_review_state(user, claude):
    user.add_category("이관")
    res = claude.add_qa("이관", "  .part 를 쓰는 이유 ", "다 쓴 뒤 rename 으로 확정한다.")

    item = res["item"]
    assert res["status"] == "created"
    assert item["question"] == ".part 를 쓰는 이유"
    assert item["created_by"] == "claude"
    assert item["reviewed"] is False
    assert item["evidence_state"] == "none"

    reviewed = user.review_qa(item["id"])
    assert reviewed["reviewed"] is True
    assert reviewed["reviewed_by"] == "user"


def test_user_writes_count_as_reviewed_and_claude_edits_reset_review(user, claude):
    user.add_category("이관")
    mine = user.add_qa("이관", "사용자가 쓴 질문", "사용자가 쓴 답변")["item"]
    assert mine["reviewed"] is True and mine["reviewed_by"] == "user"

    edited = claude.update_qa(mine["id"], mine["version"], answer="Claude 가 고친 답변")
    assert edited["reviewed"] is False

    again = user.update_qa(edited["id"], edited["version"], answer="사용자가 다시 고친 답변")
    assert again["reviewed"] is True


def test_similar_question_blocks_until_forced(user):
    user.add_category("이관")
    user.add_qa("이관", ".nas-tmp 가 NAS 안에 있는 이유", "rename 은 같은 볼륨 안에서만 원자적이다.")

    res = user.add_qa("이관", ".nas-tmp 가 NAS 안에 있는 까닭", "같은 볼륨이어야 한다.")
    assert res["status"] == "similar_found"
    assert res["similar"][0]["id"] == 1

    other = user.add_qa("이관", "PETRA 를 왜 제거하나", "대상 DB 에서 쓸 수 없다.")
    assert other["status"] == "created"

    forced = user.add_qa("이관", ".nas-tmp 가 NAS 안에 있는 까닭", "같은 볼륨이어야 한다.", force=True)
    assert forced["status"] == "created"


def test_update_conflict_returns_current(user, claude):
    user.add_category("이관")
    item = user.add_qa("이관", "질문 하나", "답변 하나")["item"]
    claude.update_qa(item["id"], item["version"], answer="Claude 가 고친 답변")

    with pytest.raises(ConflictError) as ei:
        user.update_qa(item["id"], item["version"], answer="사용자가 고친 답변")
    assert ei.value.current["answer"] == "Claude 가 고친 답변"
    assert ei.value.current["version"] == item["version"] + 1


def test_update_validation_message(user):
    user.add_category("이관")
    item = user.add_qa("이관", "질문 하나", "답변 하나")["item"]
    with pytest.raises(ValidationError, match="지금 3줄"):
        user.update_qa(item["id"], item["version"], answer="1\n2\n3")
    with pytest.raises(ValidationError, match="바꿀 값"):
        user.update_qa(item["id"], item["version"])


def test_update_moves_category_and_keeps_note_in_history(user):
    user.add_category("이관")
    user.add_category("NAS", parent="이관")
    item = user.add_qa("이관", "질문 하나", "답변 하나")["item"]
    moved = user.update_qa(item["id"], item["version"], category="이관 > NAS", note="분류 정리")

    assert moved["category_path"] == "이관 > NAS"
    rev = user.history("qa", item["id"])[0]
    assert rev["op"] == "update"
    assert rev["actor"] == "user"
    assert rev["after"]["change_note"] == "분류 정리"


def test_archive_and_restore(user):
    user.add_category("이관")
    item = user.add_qa("이관", "질문 하나", "답변 하나")["item"]
    archived = user.archive_qa(item["id"], item["version"], note="중복")
    assert archived["archived"] is True
    assert user.history("qa", item["id"])[0]["op"] == "archive"

    restored = user.restore_qa(item["id"], archived["version"])
    assert restored["archived"] is False


def test_archive_category_with_items_rejected(user):
    cat = user.add_category("이관")
    user.add_qa("이관", "질문 하나", "답변 하나")
    with pytest.raises(ValidationError, match="보관되지 않은 Q&A"):
        user.archive_category(cat["id"], cat["version"])


def test_archive_empty_category(user):
    cat = user.add_category("이관")
    archived = user.archive_category(cat["id"], cat["version"])
    assert archived["archived"] is True
    assert user.categories() == []


def test_links_are_symmetric(user):
    user.add_category("이관")
    a = user.add_qa("이관", "첫째 질문", "첫째 답변")["item"]["id"]
    b = user.add_qa("이관", "PETRA 제거 이유", "둘째 답변")["item"]["id"]

    user.link(b, a)
    assert [l["id"] for l in user.get_qa(a)["links"]] == [b]
    assert [l["id"] for l in user.get_qa(b)["links"]] == [a]

    user.unlink(a, b)
    assert user.get_qa(a)["links"] == []
    with pytest.raises(ValidationError):
        user.link(a, a)


def test_add_qa_with_links(user):
    user.add_category("이관")
    a = user.add_qa("이관", "첫째 질문", "첫째 답변")["item"]["id"]
    b = user.add_qa("이관", "PETRA 제거 이유", "둘째 답변", links=[a])["item"]["id"]
    assert [l["id"] for l in user.get_qa(a)["links"]] == [b]


def test_get_missing_qa(user):
    with pytest.raises(NotFoundError):
        user.get_qa(999)


def test_concurrent_writers_do_not_lock(tmp_path):
    path = tmp_path / "kb.sqlite3"
    connect(path).close()
    errors = []

    def work(actor):
        c = connect(path)
        s = Store(c, actor)
        try:
            for i in range(20):
                s.add_category(f"{actor}-{i}")
        except Exception as e:  # noqa: BLE001 - 어떤 오류든 기록해 실패로 본다
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=work, args=(a,)) for a in ("user", "claude")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    c = connect(path)
    assert len(Store(c, "user").categories()) == 40
    c.close()
