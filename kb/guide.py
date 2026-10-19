"""기본 사용 가이드 Q&A. 내용은 kb/default_guide.json 이고 저장소에 함께 배포된다.

seed() 는 빠진 항목을 넣고, 마지막으로 system 이 쓴 항목만 json 과 같게 고친다.
사람이나 Claude 가 고친 가이드 항목은 덮어쓰지 않는다. scripts/install.py 와 run_web.py 가 부른다.
근거 경로는 저장소 루트 기준 상대 경로로 적고, 넣을 때 이 저장소의 절대 경로로 바꾼다.
원격 모드는 load_remote() 를 쓴다. 서버의 파일 경로는 사용자에게 뜻이 없고 저장소가 공개되지 않아 근거 없이 넣고,
답변의 로컬 화면 주소(LOCAL_URL)를 서버의 public_url 로 바꾼다. web/app.py 가 사용자 DB 를 처음 만들 때 1회, web/importer.py 가 DB 를 통째로 바꾼 뒤 다시 넣는다.
항목의 target 이 "local" 이면 load() 에만, "remote" 면 load_remote() 에만 들어간다. 없으면 둘 다다.
같은 질문을 두 모드에서 다르게 답할 때는 target 만 다른 항목 두 개를 둔다(DB 는 모드마다 따로다).
"""
import json
from pathlib import Path

from kb.errors import NotFoundError, ValidationError
from kb.store import Store
from kb.text import check_answer

ROOT = Path(__file__).resolve().parent.parent
GUIDE_FILE = Path(__file__).resolve().parent / "default_guide.json"
LOCAL_URL = "http://127.0.0.1:8765"
REMOTE_URL_FALLBACK = "이 서버 주소"  # public_url 로 바꾸면 답변 글자 수 상한을 넘을 때 쓴다


def _read(path: Path, skip: str) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    data["items"] = [item for item in data["items"] if item.pop("target", None) != skip]
    return data


def load(path: Path = GUIDE_FILE, root: Path = ROOT) -> dict:
    data = _read(path, "remote")
    for item in data["items"]:
        item["evidence"] = [dict(e, path=str(root / e["path"])) if e.get("path") else e
                            for e in item.get("evidence") or []]
    return data


def load_remote(public_url: str, path: Path = GUIDE_FILE) -> dict:
    data = _read(path, "local")
    for item in data["items"]:
        item["evidence"] = []
        answer = item["answer"].replace(LOCAL_URL, public_url)
        try:
            check_answer(answer)
        except ValidationError:
            answer = item["answer"].replace(LOCAL_URL, REMOTE_URL_FALLBACK)
        item["answer"] = answer
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
