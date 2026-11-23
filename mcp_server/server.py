"""fast-question MCP 서버. Claude 가 KB 를 검색·등록·수정한다.

쓰기 주체는 open_store 가 돌려주는 Store 의 actor(run_mcp.py 에서 claude)로 고정된다.
KbError 는 도구 오류로 돌려준다. 버전 충돌이면 메시지에 현재 값을 붙인다.
web_url 은 웹 화면 주소(로컬 http://127.0.0.1:<port>, 원격 public_url)다. kb_add·kb_add_many(created)·kb_update·kb_get 결과와
kb_search 의 hits·similar 항목에 그 Q&A 를 펼치는 주소 url = <web_url>/#qa/<id> 를 넣는다.
kb_search 의 hits 중 열린 정리 건(kb/cleanup.py)이 있는 항목에는 cleanup = [규칙 이름] 을 붙인다.
remote=True 는 원격 서버(web/mcp_http.py)다. 서버 파일을 읽는 kb_add_many file= 을 거부하고(도구 설명도
file 대신 items 를 쓰라고 적는다), 근거 재기록은
로컬 프록시가 보낸 캡처값(spec.captured)을 쓴다. 근거·재검증·사전의 원격 제한은 Store(remote=True)가 건다.
"""
import json
from pathlib import Path
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from kb import cleanup, public_kb
from kb.db import connect
from kb.errors import ConflictError, KbError
from kb.morph import reindex
from kb.rules import check_claude_qa
from kb.store import Store
from mcp_server.instructions import (ADD_MANY_DOC, ADD_MANY_FILE, ADD_MANY_REMOTE, PROXY_INSTRUCTIONS,
                                     RECHECK_DOC, RECHECK_REMOTE, REMOTE_INSTRUCTIONS)

READ = ToolAnnotations(read_only_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)

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
                 auth_server_provider=None, open_public: Callable[[], Store | None] | None = None) -> MCPServer:
    """open_public: 공개 지식 층(kb/public_kb.py)의 Store 를 여는 함수(원격 서버). 없거나 None 을 돌려주면 공개 층이 없다."""
    server = MCPServer("fast-question", instructions=REMOTE_INSTRUCTIONS if remote else PROXY_INSTRUCTIONS, auth=auth,
                       auth_server_provider=auth_server_provider)
    base = web_url.rstrip("/")

    def public() -> Store | None:
        return open_public() if open_public else None

    def public_store_or_error() -> Store:
        pub = public()
        if pub is None:
            raise KbError("This server has no public knowledge layer.")
        return pub

    def link(qa_id: int) -> str:
        return f"{base}/#qa/{qa_id}"

    def hit(h: dict) -> dict:
        return {**_hit(h), "url": link(h["id"])}

    def detail(item: dict) -> dict:
        return {**_detail(item), "url": link(item["id"])}

    def candidate(item: dict) -> dict:
        return {**hit(item), "level": item["level"]}

    def with_hint(s: Store, out: dict, force: bool = False) -> dict:
        """열린 정리 건이 있고 마지막 정리가 오래됐으면(또는 force) cleanup_hint 를 붙인다. Skill 이 사용자에게 정리를 권한다."""
        h = cleanup.hint(s.conn, force=force)
        return {**out, "cleanup_hint": h} if h else out

    def run(fn: Callable[[Store], Any]) -> Any:
        store = open_store()
        try:
            return fn(store)
        except ConflictError as e:
            raise ValueError(f"{e.message} Current value: {json.dumps(_detail(e.current), ensure_ascii=False)}") from e
        except KbError as e:
            raise ValueError(e.message) from e
        finally:
            store.conn.close()

    @server.tool(title="Search Q&As", annotations=READ)
    def kb_search(query: str = "", queries: list[str] | None = None, regex: bool = False,
                  category: str | None = None, limit: int = 20, include_archived: bool = False) -> dict:
        """Searches the KB (Q&As the user confirmed earlier) for facts about the user's projects, systems and internal
        procedures, and for an existing question before adding one. Space-separated words are ANDed. Syntax: a | b (OR),
        -a (exclude), "a b" (phrase), q:a / a:a (question only / answer only), path:<file name> (items with that file as evidence).
        queries takes several expressions in one call; each item lists the queries it matched (matched) and items are
        ordered by the number of matched queries.
        regex=true treats query as a regular expression (at most 200 characters, no nested repetition) over questions and
        answers, without syntax, synonyms or morphology.
        tier of a hit is exact (literal match), synonym, or morph (morphology: inflection and particle differences).
        When hits is empty, similar holds close results. category is "<top>" or "<top> > <sub>".
        If the server has a public knowledge layer, public.hits separately holds public Q&As loaded by the operator
        (scope=public, read-only). Read a public item with kb_get(id, public=true); add it to your own DB with kb_add."""
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
            out = {**head, "total": res["total"], "groups": res["groups"],
                   "hits": hits, "similar": [hit(h) for h in res["similar"]], "morph": res["morph"]}
            if not regex:
                public_kb.attach(out, public(), " ".join(queries) if queries else query, base, limit=limit, shape=_hit)
            return with_hint(s, out)
        return run(go)

    @server.tool(title="Get Q&A", annotations=READ)
    def kb_get(id: int, public: bool = False) -> dict:
        """Reads one Q&A with its evidence, related questions and last 5 revisions, including the version that
        kb_update and kb_archive require. public=true reads an item of the public knowledge layer (read-only; to change
        it, add it to your own DB with kb_add)."""
        def go(s: Store):
            if not public:
                return detail(s.get_qa(id))
            pub = public_store_or_error()
            try:
                return {**public_kb.detail(pub, id), "url": f"{base}/#pub/{id}"}
            finally:
                pub.conn.close()
        return run(go)

    @server.tool(title="List categories", annotations=READ)
    def kb_categories() -> dict:
        """Returns the category tree (top > sub) with the Q&A count of each category."""
        return run(lambda s: {"categories": s.categories()})

    @server.tool(title="Add category", annotations=WRITE)
    def kb_add_category(name: str, parent: str | None = None, description: str = "") -> dict:
        """Creates a category. With parent, it becomes a subcategory under that top-level category (two levels at most)."""
        return run(lambda s: s.add_category(name, parent=parent, description=description))

    @server.tool(title="Add Q&A", annotations=WRITE)
    def kb_add(category: str, question: str, answer: str, evidence: list[dict] | None = None,
               links: list[int] | None = None, force: bool = False) -> dict:
        """Adds a Q&A. If a nearly identical question exists, it adds nothing and returns status=similar_found with the
        candidates; if the question really differs from them, call again with force=true. If only somewhat similar
        questions exist, it adds the Q&A and returns status=created with those candidates in similar.
        Each candidate has level: block (nearly identical) or notice (somewhat similar).
        evidence forms: {"kind": "auto"|"file"|"dir", "path", "anchor"?}, {"kind": "git", "repo", "paths": [...]},
        {"kind": "githead", "repo"}, {"kind": "url", "url"}, {"kind": "github", "url": "https://github.com/<owner>/<repo>/blob/<ref>/<path>", "anchor"?}
        (the server detects changes through the GitHub API), {"kind": "note", "text"}."""
        def go(s: Store):
            check_claude_qa(question, answer, evidence)
            res = s.add_qa(category, question, answer, evidence=evidence, links=links, force=force)
            if res["status"] == "similar_found":
                # 비슷한 질문을 막 발견한 순간은 쌓인 중복도 함께 정리할 때라 마지막 정리 시점과 상관없이 권한다
                return with_hint(s, {"status": "similar_found", "similar": [candidate(i) for i in res["similar"]]},
                                 force=True)
            out = {"status": "created", "item": detail(res["item"])}
            if res.get("similar"):
                out["similar"] = [candidate(i) for i in res["similar"]]
            return with_hint(s, out)
        return run(go)

    @server.tool(title="Update Q&A", annotations=WRITE)
    def kb_update(id: int, version: int, question: str | None = None, answer: str | None = None,
                  category: str | None = None, reason: str | None = None) -> dict:
        """Changes a Q&A. version is the value read with kb_get; if it differs, the call is rejected with the current value."""
        def go(s: Store):
            check_claude_qa(question, answer)
            return detail(s.update_qa(id, version, question=question, answer=answer,
                                      category=category, note=reason))
        return run(go)

    @server.tool(title="Archive Q&A", annotations=DESTRUCTIVE)
    def kb_archive(id: int, version: int, reason: str | None = None) -> dict:
        """Deletes a Q&A (the same as delete in the web screen). The row stays and drops out of default search results.
        kb_restore brings it back."""
        return run(lambda s: _detail(s.archive_qa(id, version, note=reason)))

    @server.tool(title="Restore Q&A", annotations=WRITE)
    def kb_restore(id: int, version: int, reason: str | None = None, category: str | None = None) -> dict:
        """Restores a deleted Q&A. Find deleted items with kb_search(include_archived=true).
        If the item's category has been deleted, give the category to move it to in category."""
        return run(lambda s: _detail(s.restore_qa(id, version, note=reason, category=category)))

    @server.tool(title="Rename category", annotations=WRITE)
    def kb_rename_category(category: str, version: int, name: str, reason: str | None = None) -> dict:
        """Renames a category. category is an id, "<top>" or "<top> > <sub>"; version is the value from kb_categories.
        name is the single new name (the parent category does not change)."""
        def go(s: Store):
            return s.update_category(s.resolve_category(category), version, name=name, note=reason)
        return run(go)

    @server.tool(title="Delete category", annotations=DESTRUCTIVE)
    def kb_delete_category(category: str, version: int, confirm: bool = False, reason: str | None = None) -> dict:
        """Deletes a category together with all its subcategories and the Q&As in them.
        Without confirm, it deletes nothing and returns the number of subcategories and Q&As that would be deleted,
        with a warning to show the user as it is. confirm=true deletes; use it only after the user approves."""
        def go(s: Store):
            impact = s.category_impact(s.resolve_category(category))
            cat = impact["category"]
            if not confirm:
                return {"deleted": False, "category": cat["path"], "version": cat["version"],
                        "children": impact["children"], "qa": impact["qa"],
                        "warning": (f"Deleting category '{cat['path']}' also deletes its {impact['children']} "
                                    f"subcategories and {impact['qa']} Q&As. If the user approves, call again with confirm=true.")}
            res = s.archive_category(cat["id"], version, note=reason)
            return {"deleted": True, "category": res["category"]["path"],
                    "archived_children": res["archived_children"], "archived_qa": res["archived_qa"]}
        return run(go)

    @server.tool(title="Link Q&As", annotations=WRITE)
    def kb_link(a: int, b: int, remove: bool = False) -> dict:
        """Links two Q&As as related questions. remove=true removes the link."""
        return run(lambda s: s.unlink(a, b) if remove else s.link(a, b))

    @server.tool(title="Edit evidence", annotations=DESTRUCTIVE)
    def kb_evidence(action: str, id: int | None = None, evidence_id: int | None = None,
                    spec: dict | None = None) -> dict:
        """Changes evidence. action is add(id, spec), recapture(evidence_id: records the evidence again in its current
        state after you reread it) or archive(evidence_id). The result is the whole Q&A."""
        def go(s: Store):
            if action == "add":
                if id is None or not spec:
                    raise KbError("add needs id and spec.")
                check_claude_qa(evidence=[spec])
                return _detail(s.add_evidence(id, spec))
            if action in ("recapture", "archive"):
                if evidence_id is None:
                    raise KbError(f"{action} needs evidence_id.")
                if action == "archive":
                    return _detail(s.archive_evidence(evidence_id))
                return _detail(s.recapture_evidence(evidence_id, captured=(spec or {}).get("captured")))
            raise KbError(f"Unknown evidence action: {action}. Use add, recapture or archive.")
        return run(go)

    @server.tool(title="Get evidence spec", annotations=READ)
    def kb_evidence_spec(evidence_id: int) -> dict:
        """Returns the spec for capturing one evidence again."""
        return run(lambda s: {"spec": s.evidence_spec(evidence_id)})

    @server.tool(title="Recheck evidence", description=RECHECK_DOC + (RECHECK_REMOTE if remote else ""),
                 annotations=WRITE)
    def kb_recheck(id: int | None = None) -> dict:
        def go(s: Store):
            results = s.recheck(id)
            ids = sorted({r["qa_id"] for r in results})
            return {"results": results,
                    "items": [{"id": i["id"], "question": i["question"], "evidence_state": i["evidence_state"]}
                              for i in s.items(ids)]}
        return run(go)

    @server.tool(title="Get dictionary", annotations=READ)
    def kb_dict_get() -> dict:
        """Returns the search dictionary: synonym groups, stopwords and morphology user words."""
        return run(lambda s: {"snapshot": s.dictionary()})

    @server.tool(title="Edit dictionary", annotations=DESTRUCTIVE)
    def kb_dict_edit(action: str, terms: list[str] | None = None, group_id: int | None = None,
                     word: str | None = None, tag: str | None = None, id: int | None = None) -> dict:
        """Changes the search dictionary. action is synonym_add(terms, group_id?), synonym_remove(id),
        stopword_add(word), stopword_remove(id), user_word_add(word, tag?) or user_word_remove(id).
        Changing user words rebuilds the morphology index. The result has the dictionary after the change."""
        def go(s: Store):
            kw = {k: v for k, v in {"terms": terms, "group_id": group_id, "word": word, "tag": tag,
                                    "id": id}.items() if v is not None}
            res = s.dict_action(action, **kw)
            if res["reindex_needed"] and s.analyzer is not None:
                res["reindexed"] = reindex(s.conn, s.analyzer)
            return res
        return run(go)

    @server.tool(title="Add many Q&As", description=ADD_MANY_DOC + (ADD_MANY_REMOTE if remote else ADD_MANY_FILE),
                 annotations=WRITE)
    def kb_add_many(items: list[dict] | None = None, file: str | None = None) -> dict:
        if file and remote:
            raise ValueError("The remote server cannot read files on the user's PC. "
                             "The local proxy reads the file and sends it as items.")
        if file:
            # 윈도우에서 만든 JSON 은 BOM 이 붙어 온다
            items = json.loads(Path(file).read_text(encoding="utf-8-sig"))
        if not items:
            raise ValueError("kb_add_many needs items or file.")
        if len(items) > ADD_MANY_MAX:
            raise ValueError(f"kb_add_many takes at most {ADD_MANY_MAX} items per call; got {len(items)}.")

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
                    results.append({"index": n, "status": "error", "error": f"Missing key: {e.args[0]}."})
                    continue
                brief = [{k: i[k] for k in ("id", "question", "answer")} for i in res.get("similar", [])]
                if res["status"] == "similar_found":
                    results.append({"index": n, "status": "similar_found", "similar": brief})
                else:
                    results.append({"index": n, "status": "created", "id": res["item"]["id"],
                                    "question": res["item"]["question"], "url": link(res["item"]["id"]),
                                    **({"similar": brief} if brief else {})})
            return {"results": results, "created": sum(r["status"] == "created" for r in results)}
        return run(go)

    @server.tool(title="Get harvest record", annotations=READ)
    def kb_harvest_get(root: str | None = None) -> dict:
        """Reads the harvest records of the qa-harvest Skill. With root: record holds the last record of that target
        path (null if none). Without root: records holds all records."""
        def go(s: Store):
            if not root:
                return {"records": s.harvest_list()}
            return {"record": s.harvest_get(root)}
        return run(go)

    @server.tool(title="Set harvest record", annotations=WRITE)
    def kb_harvest_set(root: str, state: dict | None = None) -> dict:
        """Replaces the harvest record of a target path for the qa-harvest Skill
        (state example: {"repos": {"<repository path>": "<HEAD sha>"}, "created": 12, "note": "..."}).
        Paths are stored with / separators in lowercase."""
        def go(s: Store):
            if not root:
                raise KbError("kb_harvest_set needs root.")
            return {"record": s.harvest_set(root, state or {})}
        return run(go)

    @server.tool(title="List cleanup items", annotations=READ)
    def kb_cleanup_list(rule: str | None = None, limit: int = 20) -> dict:
        """Lists items to clean up: findings that the daily job recorded, and items whose evidence state is bad.
        rule is duplicate (similar questions; other is the one added first), rule_violation (breaks the current format
        rules), evidence_stale (the evidence file changed after it was recorded) or evidence_unknown (the evidence
        could not be judged). Close duplicate and rule_violation findings with kb_cleanup_resolve after fixing or
        archiving the item. Evidence findings (id is null) leave the list once the evidence is recorded again."""
        def go(s: Store):
            res = cleanup.open_list(s, rule=rule, limit=limit)
            for f in res["findings"]:
                for k in ("item", "other"):
                    if f.get(k):
                        f[k] = {**f[k], "url": link(f[k]["id"])}
            return res
        return run(go)

    @server.tool(title="Resolve cleanup item", annotations=WRITE)
    def kb_cleanup_resolve(finding_id: int, resolution: str, note: str | None = None) -> dict:
        """Closes a cleanup finding. resolution=fixed: the item was fixed or archived (the condition is checked again
        and the call is rejected if it still holds). kept: on inspection it is not a problem (it does not come back
        until the item's question or answer changes). note records the reason for the judgment."""
        return run(lambda s: cleanup.resolve(s, finding_id, resolution, note))

    return server


ADD_MANY_MAX = 100
TOOLS = ("kb_search", "kb_get", "kb_categories", "kb_add_category", "kb_add", "kb_update", "kb_archive",
         "kb_restore", "kb_rename_category", "kb_delete_category", "kb_link", "kb_evidence", "kb_evidence_spec",
         "kb_recheck", "kb_dict_get", "kb_dict_edit", "kb_add_many", "kb_harvest_get", "kb_harvest_set",
         "kb_cleanup_list", "kb_cleanup_resolve")
