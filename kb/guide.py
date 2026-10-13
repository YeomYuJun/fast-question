"""기본 사용 가이드 Q&A. 내용은 kb/default_guide.json 이고 저장소에 함께 배포된다.

seed() 는 빠진 항목을 넣고, 마지막으로 system 이 쓴 항목만 json 과 같게 고친다.
사람이나 Claude 가 고친 가이드 항목은 덮어쓰지 않는다. scripts/install.py 와 run_web.py 가 부른다.
근거 경로는 저장소 루트 기준 상대 경로로 적고, 넣을 때 이 저장소의 절대 경로로 바꾼다.
"""
import json
from pathlib import Path

from kb.errors import NotFoundError
from kb.store import Store

ROOT = Path(__file__).resolve().parent.parent
GUIDE_FILE = Path(__file__).resolve().parent / "default_guide.json"


def load(path: Path = GUIDE_FILE, root: Path = ROOT) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    for item in data["items"]:
        item["evidence"] = [dict(e, path=str(root / e["path"])) if e.get("path") else e
                            for e in item.get("evidence") or []]
    return data


def _category(s: Store, path: str, description: str = "") -> None:
    try:
        s.resolve_category(path)
    except NotFoundError:
        parent, _, name = path.rpartition(" > ")
        s.add_category(name or path, parent or None, description)


def seed(s: Store, data: dict | None = None) -> dict:
    data = data or load()
    top = data["category"]
    _category(s, top, data.get("description", ""))
    added = updated = same = kept = 0
    for item in data["items"]:
        path = f"{top} > {item['category']}"
        _category(s, path)
        row = s.conn.execute("SELECT id, updated_by FROM qa WHERE question = ? AND deleted_at IS NULL",
                             (item["question"],)).fetchone()
        if row is None:
            s.add_qa(path, item["question"], item["answer"], evidence=item["evidence"], force=True)
            added += 1
            continue
        cur = s.get_qa(row["id"])
        if cur["answer"] == item["answer"]:
            same += 1
        elif row["updated_by"] != "system":
            kept += 1
        else:
            s.update_qa(row["id"], cur["version"], answer=item["answer"], note="기본 가이드 갱신")
            updated += 1
    return {"added": added, "updated": updated, "same": same, "kept": kept}
