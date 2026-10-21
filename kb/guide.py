"""기본 사용 가이드 Q&A. 내용은 kb/default_guide.json 이고 저장소에 함께 배포된다.

seed() 는 빠진 항목을 넣고, 마지막으로 system 이 쓴 항목만 json 과 같게 고친다(sync_evidence 면 근거까지).
사람이나 Claude 가 고친 가이드 항목은 덮어쓰지 않는다. scripts/install.py 와 run_web.py 가 부른다.
근거 경로는 저장소 루트 기준 상대 경로로 적고, 넣을 때 이 저장소의 절대 경로로 바꾼다.
원격 모드는 load_remote() 를 쓴다. 서버의 파일 경로는 사용자에게 뜻이 없어 근거 경로를 공개 저장소의 GitHub 주소
(SOURCE_URL, url 근거. anchor 는 그대로)로 바꾸고, 답변의 로컬 화면 주소(LOCAL_URL)를 서버의 public_url 로 바꾼다. web/app.py 가 사용자 DB 를 처음 만들 때 1회, web/importer.py 가 DB 를 통째로 바꾼 뒤 다시 넣는다.
이미 있는 사용자 DB 는 kb/jobs.py 의 주기 작업이 fingerprint() 가 meta(guide_hash)와 다를 때 다시 넣는다.
항목의 target 이 "local" 이면 load() 에만, "remote" 면 load_remote() 에만 들어간다. 없으면 둘 다다.
같은 질문을 두 모드에서 다르게 답할 때는 target 만 다른 항목 두 개를 둔다(DB 는 모드마다 따로다).
"""
import hashlib
import json
from pathlib import Path

from kb.errors import NotFoundError, ValidationError
from kb.store import Store
from kb.text import check_answer

ROOT = Path(__file__).resolve().parent.parent
GUIDE_FILE = Path(__file__).resolve().parent / "default_guide.json"
LOCAL_URL = "http://127.0.0.1:8765"
SOURCE_URL = "https://github.com/YeomYuJun/fast-question/blob/main/"  # 원격 가이드 근거. 뒤에 저장소 기준 경로를 붙인다
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
        item["evidence"] = [{"kind": "url", "url": SOURCE_URL + e["path"], "anchor": e.get("anchor")}
                            for e in item.get("evidence") or [] if e.get("path")]
        answer = item["answer"].replace(LOCAL_URL, public_url)
        try:
            check_answer(answer)
        except ValidationError:
            answer = item["answer"].replace(LOCAL_URL, REMOTE_URL_FALLBACK)
        item["answer"] = answer
    return data


def fingerprint(data: dict) -> str:
    """가이드 내용의 해시. 원격 주기 작업이 사용자 DB 에 넣은 판과 비교한다."""
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _evidence_key(evidence: list[dict]) -> list[tuple]:
    """넣을 근거(spec)와 저장된 근거를 같은 모양으로. url 근거는 저장 때 주소가 path 에 들어간다."""
    return sorted((e["kind"], e.get("url") or e.get("path"), e.get("anchor") or None) for e in evidence)


def _category(s: Store, path: str, description: str = "") -> None:
    try:
        s.resolve_category(path)
    except NotFoundError:
        parent, _, name = path.rpartition(" > ")
        s.add_category(name or path, parent or None, description)


def seed(s: Store, data: dict | None = None, *, sync_evidence: bool = False) -> dict:
    """sync_evidence: 근거도 json 과 비교해 맞춘다. 원격 가이드(url 근거)에만 쓴다. 로컬 근거는 넣을 때 종류가
    정해져(auto → file·git) json 과 그대로 비교할 수 없다."""
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
        answer_same = cur["answer"] == item["answer"]
        evidence_same = not sync_evidence or _evidence_key(cur["evidence"]) == _evidence_key(item["evidence"])
        if answer_same and evidence_same:
            same += 1
        elif row["updated_by"] != "system":
            kept += 1
        else:
            s.update_qa(row["id"], cur["version"], answer=None if answer_same else item["answer"],
                        evidence=None if evidence_same else item["evidence"], note="기본 가이드 갱신")
            updated += 1
    return {"added": added, "updated": updated, "same": same, "kept": kept}
