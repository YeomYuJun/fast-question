from kb.db import connect
from kb.rules import problems
from kb.store import Store
from scripts import seed_guide


def test_guide_items_follow_registration_rules():
    for _, question, answer, _ in seed_guide.GUIDE:
        assert problems(question, answer) == [], (question, problems(question, answer))


def test_guide_evidence_files_exist():
    for _, question, _, evidence in seed_guide.GUIDE:
        for e in evidence:
            text = open(e["path"], encoding="utf-8").read()
            assert e["anchor"] in text, (question, e)


def test_seed_is_idempotent_and_updates_changed_answers(tmp_path):
    s = Store(connect(tmp_path / "kb.sqlite3"), "system")
    assert seed_guide.seed(s) == {"added": len(seed_guide.GUIDE), "updated": 0, "same": 0}
    assert seed_guide.seed(s) == {"added": 0, "updated": 0, "same": len(seed_guide.GUIDE)}
    top = next(c for c in s.categories() if c["name"] == "fast-question")
    assert top["description"] == seed_guide.TOP_DESCRIPTION and top["count"] == len(seed_guide.GUIDE)
    first = s.conn.execute("SELECT id, version FROM qa ORDER BY id").fetchone()
    s.update_qa(first[0], first[1], answer="다른 답이다.")
    assert seed_guide.seed(s)["updated"] == 1
    s.conn.close()
