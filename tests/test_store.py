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
    with pytest.raises(NotFoundError, match="분류를 찾지 못했습니다"):
        user.resolve_category("없는 분류")


def test_third_level_category_rejected_in_korean(user):
    user.add_category("대")
    user.add_category("중", parent="대")
    with pytest.raises(ValidationError, match="2단계"):
        user.add_category("소", parent="대 > 중")


def test_add_qa_records_actor(user, claude):
    user.add_category("이관")
    res = claude.add_qa("이관", "  .part 를 쓰는 이유 ", "다 쓴 뒤 rename 으로 확정한다.")

    item = res["item"]
    assert res["status"] == "created"
    assert item["question"] == ".part 를 쓰는 이유"
    assert item["created_by"] == "claude"
    assert item["evidence_state"] == "none"


def test_review_columns_are_not_used(user, claude, conn):
    """사람 검토 기능은 없다. reviewed_at·reviewed_by 열은 남아 있지만 어느 쓰기도 채우지 않는다."""
    user.add_category("이관")
    mine = user.add_qa("이관", "사용자가 쓴 질문", "사용자가 쓴 답변")["item"]
    edited = claude.update_qa(mine["id"], mine["version"], answer="Claude 가 고친 답변")
    user.update_qa(edited["id"], edited["version"], answer="사용자가 다시 고친 답변")
    claude.add_qa("이관", "Claude 가 쓴 질문", "Claude 가 쓴 답변")

    assert conn.execute("SELECT count(*) FROM qa WHERE reviewed_at IS NOT NULL OR reviewed_by IS NOT NULL").fetchone()[0] == 0
    assert not {"reviewed", "reviewed_at", "reviewed_by"} & set(user.get_qa(mine["id"]))
    assert not hasattr(user, "review_qa") and not hasattr(user, "oversight")


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
    with pytest.raises(ValidationError, match="지금 7줄"):
        user.update_qa(item["id"], item["version"], answer="1\n2\n3\n4\n5\n6\n7")
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


def test_archive_empty_category(user):
    cat = user.add_category("이관")
    res = user.archive_category(cat["id"], cat["version"])
    assert res["category"]["archived"] is True
    assert (res["archived_children"], res["archived_qa"]) == (0, 0)
    assert user.categories() == []


def test_archive_category_takes_children_and_items(user):
    top = user.add_category("이관")
    nas = user.add_category("NAS", parent="이관")
    other = user.add_category("기타")
    a = user.add_qa("이관", "rename 을 쓰는 이유", "답변 하나")["item"]
    b = user.add_qa("이관 > NAS", "마운트 경로 확인 방법", "답변 둘")["item"]
    gone = user.add_qa("이관 > NAS", "백업 보관 개수", "답변 셋")["item"]
    user.archive_qa(gone["id"], gone["version"])
    keep = user.add_qa("기타", "포트 번호 설정 위치", "답변 넷")["item"]

    impact = user.category_impact(top["id"])
    assert (impact["children"], impact["qa"]) == (1, 2)
    assert impact["category"]["version"] == top["version"]

    res = user.archive_category(top["id"], top["version"], note="정리")
    assert (res["archived_children"], res["archived_qa"]) == (1, 2)
    assert [c["id"] for c in user.categories()] == [other["id"]]
    assert user.get_qa(a["id"])["archived"] and user.get_qa(b["id"])["archived"]
    assert not user.get_qa(keep["id"])["archived"]
    assert user._category(nas["id"])["archived"]
    # 함께 보관된 행의 이력에 어느 분류 때문인지 남는다
    assert "이관" in user.history("qa", b["id"])[0]["after"]["change_note"]
    assert user.history("qa", b["id"])[0]["op"] == "archive"
    # 이미 보관돼 있던 항목은 다시 쓰지 않는다
    assert user.get_qa(gone["id"])["version"] == gone["version"] + 1


def test_archive_category_version_conflict_changes_nothing(user):
    top = user.add_category("이관")
    item = user.add_qa("이관", "질문 하나", "답변 하나")["item"]
    user.update_category(top["id"], top["version"], name="이관2")
    with pytest.raises(ConflictError):
        user.archive_category(top["id"], top["version"])
    assert not user.get_qa(item["id"])["archived"]
    assert [c["name"] for c in user.categories()] == ["이관2"]


def test_archived_category_cannot_be_archived_or_measured_again(user):
    cat = user.add_category("이관")
    res = user.archive_category(cat["id"], cat["version"])
    with pytest.raises(NotFoundError):
        user.category_impact(cat["id"])
    with pytest.raises(NotFoundError):
        user.archive_category(cat["id"], res["category"]["version"])


def test_rename_category(user):
    top = user.add_category("이관")
    user.add_category("NAS", parent="이관")
    user.add_category("기타")
    renamed = user.update_category(top["id"], top["version"], name="이관 사업")
    assert renamed["path"] == "이관 사업"
    assert user.resolve_category("이관 사업 > NAS")
    with pytest.raises(ValidationError, match="같은 이름"):
        user.update_category(top["id"], renamed["version"], name="기타")


def test_restore_into_archived_category_needs_target(user):
    top = user.add_category("이관")
    user.add_category("기타")
    item = user.add_qa("이관", "질문 하나", "답변 하나")["item"]
    user.archive_category(top["id"], top["version"])
    archived = user.get_qa(item["id"])
    with pytest.raises(ValidationError, match="분류가 삭제"):
        user.restore_qa(item["id"], archived["version"])
    restored = user.restore_qa(item["id"], archived["version"], category="기타")
    assert restored["archived"] is False and restored["category_path"] == "기타"


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


def test_category_name_cannot_contain_path_separator(user):
    with pytest.raises(ValidationError, match=">"):
        user.add_category("CI>CD")
