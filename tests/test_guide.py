import json
import subprocess
import sys
from pathlib import Path

from kb import guide
from kb.config import load_config
from kb.db import connect
from kb.rules import problems
from kb.store import Store

ROOT = Path(__file__).resolve().parent.parent


def test_guide_file_is_tracked_with_relative_paths():
    raw = json.loads(guide.GUIDE_FILE.read_text(encoding="utf-8"))
    assert raw["items"]
    for item in raw["items"]:
        for e in item["evidence"]:
            assert not Path(e["path"]).is_absolute() and (ROOT / e["path"]).is_file(), e
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--error-unmatch", "kb/default_guide.json"],
                             capture_output=True)
    assert tracked.returncode == 0, "kb/default_guide.json 이 git 에 추적되지 않는다"


def test_guide_items_follow_registration_rules_and_anchors_exist():
    for item in guide.load()["items"]:
        assert problems(item["question"], item["answer"]) == [], item["question"]
        for e in item["evidence"]:
            assert e["anchor"] in Path(e["path"]).read_text(encoding="utf-8"), (item["question"], e)


def test_load_resolves_paths_against_given_root(tmp_path):
    data = guide.load(root=tmp_path)
    assert all(e["path"].startswith(str(tmp_path)) for it in data["items"] for e in it["evidence"])


def test_seed_adds_once_updates_system_items_and_keeps_edited_ones(tmp_path):
    s = Store(connect(tmp_path / "kb.sqlite3"), "system")
    n = len(guide.load()["items"])
    assert guide.seed(s) == {"added": n, "updated": 0, "same": 0, "kept": 0}
    assert guide.seed(s) == {"added": 0, "updated": 0, "same": n, "kept": 0}
    top = next(c for c in s.categories() if c["name"] == "fast-question")
    assert top["description"] and top["count"] == n

    ids = [r[0] for r in s.conn.execute("SELECT id FROM qa ORDER BY id LIMIT 2")]
    a, b = (s.get_qa(i) for i in ids)
    s.update_qa(a["id"], a["version"], answer="system 이 바꾼 옛 답이다.")
    Store(s.conn, "user").update_qa(b["id"], b["version"], answer="사용자가 고친 답이다.")
    assert guide.seed(s) == {"added": 0, "updated": 1, "same": n - 2, "kept": 1}
    assert s.get_qa(b["id"])["answer"] == "사용자가 고친 답이다."
    s.conn.close()


def test_seed_guide_cli_uses_config_db(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"db_path": str(tmp_path / "kb.sqlite3")}), encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "seed_guide.py"), "--config", str(cfg)],
                       capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    conn = connect(load_config(cfg).db_path)
    assert conn.execute("SELECT count(*) FROM qa").fetchone()[0] == len(guide.load()["items"])
    conn.close()
