"""fast-question MCP 서버. Claude 가 KB 를 검색·등록·수정한다.

쓰기 주체는 open_store 가 돌려주는 Store 의 actor(run_mcp.py 에서 claude)로 고정된다.
KbError 는 도구 오류로 돌려준다. 버전 충돌이면 메시지에 현재 값을 붙인다.
"""
import json
from pathlib import Path
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from kb.db import connect
from kb.errors import ConflictError, KbError
from kb.morph import reindex
from kb.store import Store

INSTRUCTIONS = """\
fast-question 은 사용자의 질문·답변 지식 DB 다.
- 질문은 한 줄(150자 이하), 답변은 1~2줄(400자 이하)로 쓴다. 사실만 쓰고 비유를 쓰지 않는다.
- 로컬 파일에서 확인한 사실은 evidence 에 {"kind": "auto", "path": "<파일 절대경로>", "anchor": "<심볼이나 찾을 문자열>"} 를 단다.
  일반 지식(k8s 개념, 수학 등)은 근거 없이 쓴다. 사람에게 확인한 사실은 {"kind": "note", "text": "누가 어떻게 확인"} 을 단다.
- 등록 전에 kb_search 로 같은 질문이 있는지 본다. 있으면 kb_get 으로 version 을 읽고 kb_update 로 고친다.
- kb_search 가 0건이면 표현을 바꿔 두 번까지 다시 검색한다(형태소·동의어로도 못 찾는 표현이 있다).
"""

HIT_KEYS = ("id", "category_path", "question", "answer", "tier", "evidence_state", "archived",
            "created_by", "updated_at", "version")


def _hit(h: dict) -> dict:
    out = {k: h[k] for k in HIT_KEYS if k in h}
    if "score" in h:
        out["score"] = h["score"]
    return out


def _detail(item: dict) -> dict:
    out = dict(item)
    out["revisions"] = item.get("revisions", [])[:5]
    return out


def store_factory(db_path: Path, analyzer) -> Callable[[], Store]:
    """호출마다 새 연결을 여는 Store 생성기. 도구 호출은 작업 스레드에서 병렬로 돌기 때문에
    연결 하나를 나눠 쓰면 트랜잭션과 lastrowid 가 섞인다. 형태소 분석기는 내부 잠금이 있어 공유한다."""
    return lambda: Store(connect(db_path), "claude", analyzer=analyzer)


def build_server(open_store: Callable[[], Store]) -> MCPServer:
    server = MCPServer("fast-question", instructions=INSTRUCTIONS)

    def run(fn: Callable[[Store], Any]) -> Any:
        store = open_store()
        try:
            return fn(store)
        except ConflictError as e:
            raise ValueError(f"{e.message} 현재 값: {json.dumps(_detail(e.current), ensure_ascii=False)}") from e
        except KbError as e:
            raise ValueError(e.message) from e
        finally:
            store.conn.close()

    @server.tool()
    def kb_search(query: str, category: str | None = None, limit: int = 20, include_archived: bool = False) -> dict:
        """KB 를 검색한다. 공백으로 나눈 단어는 AND 로 찾는다.
        hits 의 tier 는 exact(그대로 일치), synonym(동의어), morph(형태소: 활용형·조사 차이)다.
        hits 가 0건이면 similar 에 비슷한 결과가 온다. category 는 "대분류" 또는 "대분류 > 중분류"."""
        def go(s: Store):
            cid = s.resolve_category(category) if category else None
            res = s.search(query, category_id=cid, include_archived=include_archived, limit=limit)
            return {"query": res["query"], "total": res["total"], "groups": res["groups"],
                    "hits": [_hit(h) for h in res["hits"]], "similar": [_hit(h) for h in res["similar"]],
                    "morph": res["morph"]}
        return run(go)

    @server.tool()
    def kb_get(id: int) -> dict:
        """Q&A 한 건을 근거, 관련 질문, 최근 이력 5건과 함께 읽는다. 고치기 전에 version 을 여기서 읽는다."""
        return run(lambda s: _detail(s.get_qa(id)))

    @server.tool()
    def kb_categories() -> dict:
        """분류 트리(대분류 > 중분류)와 분류별 Q&A 수."""
        return run(lambda s: {"categories": s.categories()})

    @server.tool()
    def kb_add_category(name: str, parent: str | None = None, description: str = "") -> dict:
        """분류를 만든다. parent 를 주면 그 대분류 아래 중분류가 된다(2단계까지)."""
        return run(lambda s: s.add_category(name, parent=parent, description=description))

    @server.tool()
    def kb_add(category: str, question: str, answer: str, evidence: list[dict] | None = None,
               links: list[int] | None = None, force: bool = False) -> dict:
        """Q&A 를 등록한다. 비슷한 질문이 있으면 등록하지 않고 status=similar_found 와 후보를 돌려준다.
        후보와 다른 질문이 맞으면 force=true 로 다시 부른다.
        evidence 형식: {"kind": "auto"|"file"|"dir", "path", "anchor"?}, {"kind": "git", "repo", "paths": [...]},
        {"kind": "githead", "repo"}, {"kind": "url", "url"}, {"kind": "note", "text"}."""
        def go(s: Store):
            res = s.add_qa(category, question, answer, evidence=evidence, links=links, force=force)
            if res["status"] == "similar_found":
                return {"status": "similar_found", "similar": [_hit(i) for i in res["similar"]]}
            return {"status": "created", "item": _detail(res["item"])}
        return run(go)

    @server.tool()
    def kb_update(id: int, version: int, question: str | None = None, answer: str | None = None,
                  category: str | None = None, reason: str | None = None) -> dict:
        """Q&A 를 고친다. version 은 kb_get 으로 읽은 값이다. 다르면 현재 값과 함께 거부된다."""
        return run(lambda s: _detail(s.update_qa(id, version, question=question, answer=answer,
                                                 category=category, note=reason)))

    @server.tool()
    def kb_archive(id: int, version: int, reason: str | None = None) -> dict:
        """Q&A 를 보관한다(삭제하지 않는다). 검색 기본값에서 빠진다."""
        return run(lambda s: _detail(s.archive_qa(id, version, note=reason)))

    @server.tool()
    def kb_link(a: int, b: int, remove: bool = False) -> dict:
        """두 Q&A 를 관련 질문으로 잇는다. remove=true 면 연결을 끊는다."""
        return run(lambda s: s.unlink(a, b) if remove else s.link(a, b))

    @server.tool()
    def kb_evidence(action: str, id: int | None = None, evidence_id: int | None = None,
                    spec: dict | None = None) -> dict:
        """근거를 다룬다. action=add(id, spec), recapture(evidence_id: 근거를 다시 읽은 뒤 지금 상태로 재기록),
        archive(evidence_id). 결과는 그 Q&A 전체다."""
        def go(s: Store):
            if action == "add":
                if id is None or not spec:
                    raise KbError("add 에는 id 와 spec 이 필요하다.")
                return _detail(s.add_evidence(id, spec))
            if action in ("recapture", "archive"):
                if evidence_id is None:
                    raise KbError(f"{action} 에는 evidence_id 가 필요하다.")
                fn = s.recapture_evidence if action == "recapture" else s.archive_evidence
                return _detail(fn(evidence_id))
            raise KbError(f"알 수 없는 근거 동작이다: {action}")
        return run(go)

    @server.tool()
    def kb_recheck(id: int | None = None) -> dict:
        """근거를 다시 판정한다. id 를 주면 그 Q&A 만, 없으면 전체."""
        def go(s: Store):
            results = s.recheck(id)
            ids = sorted({r["qa_id"] for r in results})
            return {"results": results,
                    "items": [{"id": i["id"], "question": i["question"], "evidence_state": i["evidence_state"]}
                              for i in s.items(ids)]}
        return run(go)

    @server.tool()
    def kb_dict(action: str, terms: list[str] | None = None, group_id: int | None = None,
                word: str | None = None, tag: str | None = None, id: int | None = None) -> dict:
        """사전을 다룬다. action 은 snapshot, synonym_add(terms, group_id?), synonym_remove(id),
        stopword_add(word), stopword_remove(id), user_word_add(word, tag?), user_word_remove(id).
        사용자 단어를 바꾸면 형태소 색인을 다시 만든다."""
        def go(s: Store):
            if action == "snapshot":
                return {"snapshot": s.dictionary(), "reindex_needed": False}
            kw = {k: v for k, v in {"terms": terms, "group_id": group_id, "word": word, "tag": tag,
                                    "id": id}.items() if v is not None}
            res = s.dict_action(action, **kw)
            if res["reindex_needed"] and s.analyzer is not None:
                res["reindexed"] = reindex(s.conn, s.analyzer)
            return res
        return run(go)

    return server


TOOLS = ("kb_search", "kb_get", "kb_categories", "kb_add_category", "kb_add", "kb_update", "kb_archive",
         "kb_link", "kb_evidence", "kb_recheck", "kb_dict")
