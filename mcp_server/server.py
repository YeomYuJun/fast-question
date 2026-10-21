"""fast-question MCP 서버. Claude 가 KB 를 검색·등록·수정한다.

쓰기 주체는 open_store 가 돌려주는 Store 의 actor(run_mcp.py 에서 claude)로 고정된다.
KbError 는 도구 오류로 돌려준다. 버전 충돌이면 메시지에 현재 값을 붙인다.
web_url 은 웹 화면 주소(로컬 http://127.0.0.1:<port>, 원격 public_url)다. kb_add·kb_add_many(created)·kb_update·kb_get 결과와
kb_search 의 hits·similar 항목에 그 Q&A 를 펼치는 주소 url = <web_url>/#qa/<id> 를 넣는다.
kb_search 의 hits 중 열린 정리 건(kb/cleanup.py)이 있는 항목에는 cleanup = [규칙 이름] 을 붙인다.
remote=True 는 원격 서버(mcp_server/http.py)다. 서버 파일을 읽는 kb_add_many file= 을 거부하고(도구 설명도
file 대신 items 를 쓰라고 적는다), 근거 재기록은
로컬 프록시가 보낸 캡처값(spec.captured)을 쓴다. 근거·재검증·사전의 원격 제한은 Store(remote=True)가 건다.
"""
import json
from pathlib import Path
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from kb import cleanup
from kb.db import connect
from kb.errors import ConflictError, KbError
from kb.morph import reindex
from kb.rules import check_claude_qa
from kb.store import Store
from mcp_server.instructions import ADD_MANY_DOC, ADD_MANY_FILE, ADD_MANY_REMOTE, INSTRUCTIONS

HIT_KEYS = ("id", "category_path", "question", "answer", "tier", "matched", "evidence_state", "archived",
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


def build_server(open_store: Callable[[], Store], *, web_url: str, remote: bool = False, auth=None,
                 auth_server_provider=None) -> MCPServer:
    server = MCPServer("fast-question", instructions=INSTRUCTIONS, auth=auth,
                       auth_server_provider=auth_server_provider)
    base = web_url.rstrip("/")

    def link(qa_id: int) -> str:
        return f"{base}/#qa/{qa_id}"

    def hit(h: dict) -> dict:
        return {**_hit(h), "url": link(h["id"])}

    def detail(item: dict) -> dict:
        return {**_detail(item), "url": link(item["id"])}

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
    def kb_search(query: str = "", queries: list[str] | None = None, regex: bool = False,
                  category: str | None = None, limit: int = 20, include_archived: bool = False) -> dict:
        """KB 를 검색한다. 공백으로 나눈 단어는 AND 다. 문법: a | b (OR), -a (제외), "a b" (구절),
        q:a / a:a (질문만 / 답변만), path:파일이름 (그 파일을 근거로 단 항목).
        queries 에 표현 여러 개를 주면 한 번에 찾고, 항목마다 걸린 질의(matched)와 걸린 질의 수 순으로 돌려준다.
        regex=true 면 query 를 정규식(200자 이하, 중첩 반복 금지)으로 질문·답변에서 찾는다. 문법·동의어·형태소는 쓰지 않는다.
        hits 의 tier 는 exact(그대로 일치), synonym(동의어), morph(형태소: 활용형·조사 차이)다.
        hits 가 0건이면 similar 에 비슷한 결과가 온다. category 는 "대분류" 또는 "대분류 > 중분류"."""
        def go(s: Store):
            cid = s.resolve_category(category) if category else None
            opts = {"category_id": cid, "include_archived": include_archived, "limit": limit}
            if queries:
                res = s.search_many(queries, regex=regex, **opts)
                head = {"queries": res["queries"]}
            else:
                res = s.search(query, regex=regex, **opts)
                head = {"query": res["query"]}
            marks = cleanup.open_rules(s.conn, [h["id"] for h in res["hits"]])
            hits = [{**hit(h), "cleanup": marks[h["id"]]} if h["id"] in marks else hit(h) for h in res["hits"]]
            return {**head, "total": res["total"], "groups": res["groups"],
                    "hits": hits, "similar": [hit(h) for h in res["similar"]],
                    "morph": res["morph"]}
        return run(go)

    @server.tool()
    def kb_get(id: int) -> dict:
        """Q&A 한 건을 근거, 관련 질문, 최근 이력 5건과 함께 읽는다. 고치기 전에 version 을 여기서 읽는다."""
        return run(lambda s: detail(s.get_qa(id)))

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
            check_claude_qa(question, answer, evidence)
            res = s.add_qa(category, question, answer, evidence=evidence, links=links, force=force)
            if res["status"] == "similar_found":
                return {"status": "similar_found", "similar": [hit(i) for i in res["similar"]]}
            return {"status": "created", "item": detail(res["item"])}
        return run(go)

    @server.tool()
    def kb_update(id: int, version: int, question: str | None = None, answer: str | None = None,
                  category: str | None = None, reason: str | None = None) -> dict:
        """Q&A 를 고친다. version 은 kb_get 으로 읽은 값이다. 다르면 현재 값과 함께 거부된다."""
        def go(s: Store):
            check_claude_qa(question, answer)
            return detail(s.update_qa(id, version, question=question, answer=answer,
                                      category=category, note=reason))
        return run(go)

    @server.tool()
    def kb_archive(id: int, version: int, reason: str | None = None) -> dict:
        """Q&A 를 삭제한다(화면의 삭제와 같다). 행은 남고 검색 기본값에서 빠진다. kb_restore 로 되살린다."""
        return run(lambda s: _detail(s.archive_qa(id, version, note=reason)))

    @server.tool()
    def kb_restore(id: int, version: int, reason: str | None = None, category: str | None = None) -> dict:
        """삭제한 Q&A 를 되살린다. 삭제한 항목은 kb_search(include_archived=true) 로 찾는다.
        그 항목의 분류가 삭제되어 있으면 category 에 옮길 분류를 준다."""
        return run(lambda s: _detail(s.restore_qa(id, version, note=reason, category=category)))

    @server.tool()
    def kb_rename_category(category: str, version: int, name: str, reason: str | None = None) -> dict:
        """분류 이름을 바꾼다. category 는 id, "대분류" 또는 "대분류 > 중분류", version 은 kb_categories 의 값이다.
        name 은 새 이름 하나다(상위 분류는 바뀌지 않는다)."""
        def go(s: Store):
            return s.update_category(s.resolve_category(category), version, name=name, note=reason)
        return run(go)

    @server.tool()
    def kb_delete_category(category: str, version: int, confirm: bool = False, reason: str | None = None) -> dict:
        """분류를 삭제한다. 하위 분류와 그 안의 Q&A 도 모두 함께 삭제된다.
        confirm 없이 부르면 아무것도 지우지 않고 함께 삭제될 하위 분류 수·Q&A 수와 경고문을 돌려준다.
        그 경고문을 사용자에게 그대로 보여 주고, 사용자가 승인했을 때만 confirm=true 로 다시 부른다."""
        def go(s: Store):
            impact = s.category_impact(s.resolve_category(category))
            cat = impact["category"]
            if not confirm:
                return {"deleted": False, "category": cat["path"], "version": cat["version"],
                        "children": impact["children"], "qa": impact["qa"],
                        "warning": (f"분류 '{cat['path']}' 를 삭제하면 하위 분류 {impact['children']}개와 "
                                    f"Q&A {impact['qa']}건이 모두 함께 삭제된다. 사용자가 승인하면 confirm=true 로 다시 부른다.")}
            res = s.archive_category(cat["id"], version, note=reason)
            return {"deleted": True, "category": res["category"]["path"],
                    "archived_children": res["archived_children"], "archived_qa": res["archived_qa"]}
        return run(go)

    @server.tool()
    def kb_link(a: int, b: int, remove: bool = False) -> dict:
        """두 Q&A 를 관련 질문으로 잇는다. remove=true 면 연결을 끊는다."""
        return run(lambda s: s.unlink(a, b) if remove else s.link(a, b))

    @server.tool()
    def kb_evidence(action: str, id: int | None = None, evidence_id: int | None = None,
                    spec: dict | None = None) -> dict:
        """근거를 다룬다. action=add(id, spec), recapture(evidence_id: 근거를 다시 읽은 뒤 지금 상태로 재기록),
        archive(evidence_id). 결과는 그 Q&A 전체다. spec(evidence_id) 은 그 근거를 다시 캡처할 spec 만 돌려준다."""
        def go(s: Store):
            if action == "add":
                if id is None or not spec:
                    raise KbError("add 에는 id 와 spec 이 필요하다.")
                check_claude_qa(evidence=[spec])
                return _detail(s.add_evidence(id, spec))
            if action in ("recapture", "archive", "spec"):
                if evidence_id is None:
                    raise KbError(f"{action} 에는 evidence_id 가 필요하다.")
                if action == "archive":
                    return _detail(s.archive_evidence(evidence_id))
                if action == "spec":
                    return {"spec": s.evidence_spec(evidence_id)}
                return _detail(s.recapture_evidence(evidence_id, captured=(spec or {}).get("captured")))
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

    @server.tool(description=ADD_MANY_DOC + (ADD_MANY_REMOTE if remote else ADD_MANY_FILE))
    def kb_add_many(items: list[dict] | None = None, file: str | None = None) -> dict:
        if file and remote:
            raise ValueError("원격 서버는 사용자 PC 의 파일을 읽을 수 없다. 로컬 프록시가 파일을 읽어 items 로 보낸다.")
        if file:
            # 윈도우에서 만든 JSON 은 BOM 이 붙어 온다
            items = json.loads(Path(file).read_text(encoding="utf-8-sig"))
        if not items:
            raise ValueError("kb_add_many 에는 items 나 file 이 필요하다.")
        if len(items) > ADD_MANY_MAX:
            raise ValueError(f"kb_add_many 는 한 번에 {ADD_MANY_MAX}건까지 받는다. 지금 {len(items)}건이다.")

        def go(s: Store):
            results = []
            for n, it in enumerate(items):
                try:
                    check_claude_qa(it.get("question"), it.get("answer"), it.get("evidence"))
                    res = s.add_qa(it["category"], it["question"], it["answer"], evidence=it.get("evidence"),
                                   links=it.get("links"), force=bool(it.get("force")))
                except KbError as e:
                    results.append({"index": n, "status": "error", "error": e.message})
                    continue
                except KeyError as e:
                    results.append({"index": n, "status": "error", "error": f"{e.args[0]} 이(가) 없다."})
                    continue
                if res["status"] == "similar_found":
                    results.append({"index": n, "status": "similar_found",
                                    "similar": [{k: i[k] for k in ("id", "question", "answer")} for i in res["similar"]]})
                else:
                    results.append({"index": n, "status": "created", "id": res["item"]["id"],
                                    "question": res["item"]["question"], "url": link(res["item"]["id"])})
            return {"results": results, "created": sum(r["status"] == "created" for r in results)}
        return run(go)

    @server.tool()
    def kb_harvest(action: str, root: str | None = None, state: dict | None = None) -> dict:
        """qa-harvest Skill 의 수확 기록. action=get(root): 그 대상 경로의 마지막 기록(없으면 null),
        set(root, state): 기록을 바꾼다(state 예: {"repos": {"<저장소 경로>": "<HEAD sha>"}, "created": 12, "note": "..."}),
        list: 모든 기록. 경로는 / 구분자·소문자로 맞춰 저장한다."""
        def go(s: Store):
            if action == "list":
                return {"records": s.harvest_list()}
            if not root:
                raise KbError(f"{action} 에는 root 가 필요하다.")
            if action == "get":
                return {"record": s.harvest_get(root)}
            if action == "set":
                return {"record": s.harvest_set(root, state or {})}
            raise KbError(f"알 수 없는 수확 기록 동작이다: {action}")
        return run(go)

    @server.tool()
    def kb_cleanup_list(rule: str | None = None, limit: int = 20) -> dict:
        """정리할 항목 목록. 주기 작업이 하루 한 번 찾아 둔 건과 근거 상태가 나쁜 항목이다.
        rule 은 duplicate(질문이 비슷한 항목. other 가 먼저 등록된 쪽), rule_violation(지금 형식 규칙 위반),
        evidence_stale(근거 파일이 기록 뒤에 바뀜), evidence_unknown(근거를 판정하지 못함)이다.
        duplicate·rule_violation 은 고치거나 보관한 뒤 kb_cleanup_resolve 로 닫는다. 근거 건(id 가 null)은 근거를
        다시 기록하면 목록에서 빠진다."""
        def go(s: Store):
            res = cleanup.open_list(s, rule=rule, limit=limit)
            for f in res["findings"]:
                for k in ("item", "other"):
                    if f.get(k):
                        f[k] = {**f[k], "url": link(f[k]["id"])}
            return res
        return run(go)

    @server.tool()
    def kb_cleanup_resolve(finding_id: int, resolution: str, note: str | None = None) -> dict:
        """정리 건을 닫는다. resolution=fixed: 항목을 고치거나 보관했다(조건을 다시 확인해 아직 걸리면 거부한다).
        kept: 확인해 보니 문제가 아니다(그 항목의 질문·답이 바뀌기 전까지 다시 올라오지 않는다). note 에 판단 근거를 적는다."""
        return run(lambda s: cleanup.resolve(s, finding_id, resolution, note))

    return server


ADD_MANY_MAX = 100
TOOLS = ("kb_search", "kb_get", "kb_categories", "kb_add_category", "kb_add", "kb_update", "kb_archive",
         "kb_restore", "kb_rename_category", "kb_delete_category", "kb_link", "kb_evidence", "kb_recheck", "kb_dict", "kb_add_many", "kb_harvest",
         "kb_cleanup_list", "kb_cleanup_resolve")
