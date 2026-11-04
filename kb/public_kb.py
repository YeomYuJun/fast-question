"""공개 지식 층(원격 모드). 운영자가 따로 쌓아 적재한 공개 DB 하나를 모든 계정의 검색 결과에 읽기 전용으로 합친다.

- 파일: <data_dir>/public/kb.sqlite3. 운영자가 scripts/public_db.py set <sqlite 파일> 로 바꿔 넣는다. 파일이 없으면 공개 층은 없다.
- 읽기만 한다. 웹·MCP 어느 경로로도 공개 DB 에 쓰지 않는다(쓰기 주체 system 의 Store 를 만들지만 쓰기 메서드를 부르지 않고,
  연결은 읽기 전용 URI 로 연다). 사용자는 항목을 자기 DB 로 복사해 고친다(copy_to).
- 검색: Store.search 를 공개 DB 에 같은 질의로 돌려 hits 에 scope="public" 과 주소 #pub/<id> 를 붙인다. 공개 항목의 id 는
  사용자 DB 의 id 와 겹치므로 scope 로 구분한다. 빈 질의(최근 목록)에는 합치지 않는다.
- 운영자 적재 파일은 web/importer.validate 와 같은 검사(무결성·스키마 버전·표 목록·뷰 없음)를 받고, system 이 만든
  항목(기본 가이드)은 kb.dbcopy.prune 으로 뺀다.
"""
import sqlite3
from pathlib import Path

from kb.db import open_db
from kb.errors import NotFoundError
from kb.store import Store

PUBLIC_DIR = "public"
KB_FILE = "kb.sqlite3"
SCOPE = "public"


def public_path(data_dir: Path) -> Path:
    return Path(data_dir) / PUBLIC_DIR / KB_FILE


def open_public(data_dir: Path) -> sqlite3.Connection | None:
    """공개 DB 읽기 전용 연결. 파일이 없으면 None."""
    path = public_path(data_dir)
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5.0, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def public_store(data_dir: Path, analyzer=None) -> Store | None:
    conn = open_public(data_dir)
    return None if conn is None else Store(conn, "system", analyzer, remote=True)


def mark(hit: dict, web_url: str) -> dict:
    """검색 결과 항목에 공개 표시와 주소를 붙인다."""
    return {**hit, "scope": SCOPE, "url": f"{web_url.rstrip('/')}/#pub/{hit['id']}"}


def search(pub: Store, query: str, *, limit: int = 20, **opts) -> dict:
    """공개 DB 검색. 빈 질의면 빈 결과. 비슷한 결과(similar)는 합치지 않는다."""
    if not (query or "").strip():
        return {"total": 0, "hits": []}
    res = pub.search(query, limit=limit, **opts)
    return {"total": res["total"], "hits": res["hits"]}


def detail(pub: Store, qa_id: int) -> dict:
    """공개 항목 상세. 이력은 빼고 근거는 url·note 만 남긴다(공개 DB 의 파일 근거는 운영자 PC 의 것이다)."""
    item = pub.get_qa(qa_id)
    if item["archived"]:
        raise NotFoundError(f"공개 항목을 찾지 못했습니다: {qa_id}")
    item.pop("revisions", None)
    item["evidence"] = [e for e in item["evidence"] if e["kind"] in ("url", "note", "github")]
    return {**item, "scope": SCOPE}


def ensure_category(store: Store, path: str, descriptions: dict[str, str] | None = None) -> int:
    """사용자 DB 에 "대분류" 또는 "대분류 > 중분류" 경로를 만들어(이미 있으면 그대로) id 를 돌려준다."""
    parts = [p.strip() for p in path.split(">") if p.strip()]
    descriptions = descriptions or {}
    parent = None
    for i, name in enumerate(parts):
        full = " > ".join(parts[: i + 1])
        try:
            parent = store.resolve_category(full)
        except NotFoundError:
            parent = store.add_category(name, parent=parent, description=descriptions.get(full, ""))["id"]
    return parent


def copy_to(pub: Store, store: Store, qa_id: int, *, category: str | None = None, force: bool = False) -> dict:
    """공개 항목을 사용자 DB 에 등록한다. 분류는 category(경로)가 없으면 공개 항목과 같은 경로를 만들어 쓴다.
    url·note 근거만 옮긴다. 결과는 Store.add_qa 와 같다(비슷한 질문이 있으면 similar_found)."""
    item = detail(pub, qa_id)
    descriptions = {c["path"]: c["description"] for t in pub.categories() for c in [t, *t["children"]]}
    cid = ensure_category(store, category or item["category_path"], descriptions)
    evidence = [{"kind": "note", "text": e["text"]} if e["kind"] == "note" else {"kind": "url", "url": e["path"]}
                for e in item["evidence"]]  # github 근거는 url 로 옮긴다(복사할 때 GitHub API 를 부르지 않는다)
    return store.add_qa(cid, item["question"], item["answer"], evidence=evidence or None, force=force)
