"""쓰기·조회 진입점. web, MCP, 주기 작업이 모두 이 클래스만 쓴다.

쓰기 주체(actor)는 생성할 때 정해지고 호출자가 바꿀 수 없다.
"""
import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Literal

from kb.db import BUMP, NOW, get_meta, set_meta, tx
from kb.errors import ConflictError, NotFoundError, ValidationError, from_sqlite
from kb import dictionary as dict_mod
from kb import search as search_mod
from kb.evidence import Checker, accept_captured, capture, overall_state, spec_of
from kb.morph import write_index
from kb.schema import AUDITED
from kb.text import SIMILAR_MIN, bigrams, check_answer, check_name, check_question, clean, dice

Actor = Literal["user", "claude", "system"]
ACTORS = ("user", "claude", "system")

QA_COLS = ("id, category_id, question, answer, evidence_state, "
           "deleted_at, created_at, created_by, updated_at, updated_by, version")


# 원격 모드 계정당 상한(삭제한 행 포함, 행이 남아 공간을 쓴다)
QUOTAS = {"qa": ("Q&A", 20_000), "category": ("분류", 500), "evidence": ("근거", 100_000)}
# 원격 모드 계정당 DB 파일 크기 상한. 행 수 상한이 세지 않는 것(고칠 때마다 쌓이는 이력, 사전, 수확 기록, 근거 캡처값)을
# 막는다. 쓰기마다 PRAGMA 로 잰다(WAL 에 있는 페이지도 든다). 보관·복원·연결 끊기·근거 판정은 막지 않는다.
# 보관은 행을 지우지 않아 크기가 줄지 않는다
DB_MAX_BYTES = 200 * 1024 * 1024
MB = 1024 * 1024


def db_bytes(conn: sqlite3.Connection) -> int:
    """비어 있는 페이지를 뺀 DB 크기(바이트)."""
    used = conn.execute("SELECT (SELECT page_count FROM pragma_page_count()) - "
                        "(SELECT freelist_count FROM pragma_freelist_count())").fetchone()[0]
    return used * conn.execute("PRAGMA page_size").fetchone()[0]

REMOTE_PATH_EVIDENCE = ("원격 서버는 사용자 PC 의 파일을 읽을 수 없어 파일·폴더·git 근거를 직접 만들 수 없습니다. "
                        "로컬 프록시(run_mcp.py --remote)로 등록하거나 url·note 근거를 써 주세요.")


class Store:
    def __init__(self, conn: sqlite3.Connection, actor: Actor, analyzer=None, *, remote: bool = False):
        """remote: 원격 서버의 사용자 DB. 근거 spec 의 captured(로컬 프록시가 사용자 PC 에서 만든 캡처값)를
        형식만 확인하고 저장하고, 캡처값 없는 파일·폴더·git 근거와 재검증(서버 파일시스템을 읽는다)은 거부한다."""
        if actor not in ACTORS:
            raise ValueError(f"알 수 없는 쓰기 주체: {actor}")
        self.conn = conn
        self.actor = actor
        self.analyzer = analyzer
        self.remote = remote

    def _check_quota(self, table: str, adding: int = 1) -> None:
        if not self.remote or adding <= 0:
            return
        label, limit = QUOTAS[table]
        n = self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if n + adding > limit:
            raise ValidationError(f"이 계정에 {label} {n}건이 있습니다. 계정당 {limit}건(삭제한 것 포함)까지 둘 수 있습니다.", code="quota")

    def _capture(self, spec: dict) -> dict:
        if not self.remote:
            return capture(spec)
        if "captured" in spec:
            row = accept_captured(spec["captured"])
            if row["kind"] in ("url", "note"):  # 서버가 직접 만들 수 있고, 형식 규칙이 spec 의 글을 검사한다
                raise ValidationError("url·note 근거는 captured 없이 보내 주세요.")
            return row
        if (spec.get("kind") or "auto") not in ("url", "note"):
            raise ValidationError(REMOTE_PATH_EVIDENCE)
        return capture(spec)

    # ---------- 공통 ----------

    def _check_size(self) -> None:
        if not self.remote:
            return
        size = db_bytes(self.conn)
        if size >= DB_MAX_BYTES:
            raise ValidationError(
                f"이 계정의 DB 파일이 {size // MB} MB 로 계정당 상한 {DB_MAX_BYTES // MB} MB 에 닿아 더 쓸 수 없습니다. "
                "삭제해도 파일은 줄지 않습니다. 연결 패널의 저장소 파일 받기로 지금까지의 내용을 내려받을 수 있습니다.",
                code="quota")

    @contextmanager
    def _writing(self, grows: bool = True) -> Iterator[sqlite3.Connection]:
        """grows: 파일을 키우는 쓰기다. 원격 모드는 파일 크기 상한(DB_MAX_BYTES)을 먼저 본다."""
        if grows:
            self._check_size()
        try:
            with tx(self.conn) as c:
                yield c
        except sqlite3.IntegrityError as e:
            raise from_sqlite(e) from e

    def _paths(self) -> dict[int, str]:
        rows = self.conn.execute("SELECT id, parent_id, name FROM category").fetchall()
        names = {r["id"]: r["name"] for r in rows}
        return {r["id"]: (f"{names[r['parent_id']]} > {r['name']}" if r["parent_id"] else r["name"]) for r in rows}

    def _item(self, row: sqlite3.Row, paths: dict[int, str] | None = None) -> dict:
        paths = paths if paths is not None else self._paths()
        return {
            "id": row["id"],
            "category_id": row["category_id"],
            "category_path": paths.get(row["category_id"], ""),
            "question": row["question"],
            "answer": row["answer"],
            "evidence_state": row["evidence_state"],
            "archived": row["deleted_at"] is not None,
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
            "version": row["version"],
        }

    def _qa_row(self, id: int) -> sqlite3.Row:
        row = self.conn.execute(f"SELECT {QA_COLS} FROM qa WHERE id = ?", (id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Q&A 를 찾지 못했습니다: {id}")
        return row

    def items(self, ids: list[int]) -> list[dict]:
        """id 순서를 유지한 item 목록."""
        if not ids:
            return []
        paths = self._paths()
        marks = ",".join("?" * len(ids))
        rows = {r["id"]: r for r in self.conn.execute(f"SELECT {QA_COLS} FROM qa WHERE id IN ({marks})", ids)}
        return [self._item(rows[i], paths) for i in ids if i in rows]

    def _update(self, table: str, id: int, version: int | None, sets: dict, current, after=None) -> None:
        """version 이 None 이면 버전 검사 없이 고친다. 0행이면 충돌 또는 없음."""
        cols = ", ".join(f"{k} = ?" for k in sets) + f", updated_by = ?, {BUMP}"
        params = list(sets.values()) + [self.actor, id]
        where = "id = ?"
        if version is not None:
            where += " AND version = ?"
            params.append(version)
        with self._writing() as c:
            n = c.execute(f"UPDATE {table} SET {cols} WHERE {where}", params).rowcount
            if n and after:
                after(c)
        if n == 0:
            cur = current(id)  # 없으면 NotFoundError
            raise ConflictError("다른 곳에서 먼저 고쳤습니다. 현재 값을 다시 읽고 고쳐 주세요.", current=cur)

    # ---------- 분류 ----------

    def _category(self, id: int, paths: dict[int, str] | None = None) -> dict:
        row = self.conn.execute(
            "SELECT id, parent_id, name, description, deleted_at, version FROM category WHERE id = ?", (id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"분류를 찾지 못했습니다: {id}")
        paths = paths if paths is not None else self._paths()
        return {
            "id": row["id"], "parent_id": row["parent_id"], "name": row["name"],
            "path": paths[row["id"]], "description": row["description"],
            "archived": row["deleted_at"] is not None, "version": row["version"],
        }

    def categories(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, parent_id, name, description, version FROM category WHERE deleted_at IS NULL ORDER BY name"
        ).fetchall()
        counts = dict(self.conn.execute(
            "SELECT category_id, count(*) FROM qa WHERE deleted_at IS NULL GROUP BY category_id"
        ).fetchall())
        paths = self._paths()
        nodes = {
            r["id"]: {"id": r["id"], "parent_id": r["parent_id"], "name": r["name"], "path": paths[r["id"]],
                      "description": r["description"], "version": r["version"],
                      "count": counts.get(r["id"], 0), "children": []}
            for r in rows
        }
        tops = []
        for n in nodes.values():
            parent = nodes.get(n["parent_id"]) if n["parent_id"] else None
            if parent:
                parent["children"].append(n)
                parent["count"] += n["count"]
            elif n["parent_id"] is None:
                tops.append(n)
        return tops

    def resolve_category(self, ref: int | str) -> int:
        if isinstance(ref, str) and ref.strip().isdigit():
            ref = int(ref.strip())
        if isinstance(ref, int):
            row = self.conn.execute("SELECT id FROM category WHERE id = ? AND deleted_at IS NULL", (ref,)).fetchone()
        else:
            parts = [clean(p) for p in str(ref).split(">")]
            if len(parts) == 1:
                row = self.conn.execute(
                    "SELECT id FROM category WHERE parent_id IS NULL AND name = ? AND deleted_at IS NULL", parts
                ).fetchone()
            elif len(parts) == 2:
                row = self.conn.execute(
                    "SELECT c.id FROM category c JOIN category p ON p.id = c.parent_id "
                    "WHERE p.parent_id IS NULL AND p.name = ? AND c.name = ? "
                    "AND c.deleted_at IS NULL AND p.deleted_at IS NULL", parts
                ).fetchone()
            else:
                raise ValidationError("분류는 대분류와 중분류 2단계까지만 만들 수 있습니다.", code="category_depth")
        if row is None:
            raise NotFoundError(f"분류를 찾지 못했습니다: {ref}")
        return row[0]

    def add_category(self, name: str, parent: int | str | None = None, description: str = "") -> dict:
        name = check_name(name)
        parent_id = self.resolve_category(parent) if parent not in (None, "") else None
        self._check_quota("category")
        with self._writing() as c:
            cid = c.execute(
                "INSERT INTO category(name, parent_id, description, created_by, updated_by) VALUES (?,?,?,?,?)",
                (name, parent_id, clean(description), self.actor, self.actor),
            ).lastrowid
        return self._category(cid)

    def update_category(self, id: int, version: int, *, name: str | None = None,
                        description: str | None = None, note: str | None = None) -> dict:
        sets = {}
        if name is not None:
            sets["name"] = check_name(name)
        if description is not None:
            sets["description"] = clean(description)
        if not sets:
            raise ValidationError("바꿀 값이 없습니다.")
        sets["change_note"] = note
        self._update("category", id, version, sets, self._category)
        return self._category(id)

    def _live_category(self, id: int) -> dict:
        cat = self._category(id)
        if cat["archived"]:
            raise NotFoundError(f"분류를 찾지 못했습니다(이미 삭제됨): {id}")
        return cat

    def category_impact(self, id: int) -> dict:
        """분류를 보관하면 함께 보관될 하위 분류 수와 Q&A 수."""
        cat = self._live_category(id)
        n_child, n_qa = self.conn.execute(
            "SELECT (SELECT count(*) FROM category WHERE parent_id = :id AND deleted_at IS NULL), "
            "(SELECT count(*) FROM qa WHERE deleted_at IS NULL AND category_id IN "
            " (SELECT :id UNION SELECT id FROM category WHERE parent_id = :id AND deleted_at IS NULL))",
            {"id": id}).fetchone()
        return {"category": cat, "children": n_child, "qa": n_qa}

    def archive_category(self, id: int, version: int, note: str | None = None) -> dict:
        """분류를 보관한다. 하위 분류와 그 안의 보관되지 않은 Q&A 도 같은 트랜잭션에서 함께 보관한다.
        함께 보관된 행의 change_note 에는 어느 분류 때문인지 남긴다."""
        cat = self._live_category(id)
        follow = f"분류 '{cat['path']}' 삭제로 함께 삭제" + (f": {note}" if note else "")
        archive = f"deleted_at = {NOW}, deleted_by = ?, updated_by = ?, change_note = ?, {BUMP}"
        n_child = n_qa = 0
        with self._writing(grows=False) as c:
            n = c.execute(f"UPDATE category SET {archive} WHERE id = ? AND version = ? AND deleted_at IS NULL",
                          (self.actor, self.actor, note, id, version)).rowcount
            if n:
                ids = [id] + [r[0] for r in c.execute(
                    "SELECT id FROM category WHERE parent_id = ? AND deleted_at IS NULL", (id,))]
                marks = ",".join("?" * len(ids))
                n_qa = c.execute(f"UPDATE qa SET {archive} WHERE category_id IN ({marks}) AND deleted_at IS NULL",
                                 [self.actor, self.actor, follow, *ids]).rowcount
                n_child = c.execute(f"UPDATE category SET {archive} WHERE parent_id = ? AND deleted_at IS NULL",
                                    (self.actor, self.actor, follow, id)).rowcount
        if n == 0:
            raise ConflictError("다른 곳에서 먼저 고쳤습니다. 현재 값을 다시 읽고 고쳐 주세요.", current=self._category(id))
        return {"category": self._category(id), "archived_children": n_child, "archived_qa": n_qa}

    # ---------- Q&A ----------

    def similar(self, question: str, *, exclude_id: int | None = None, limit: int = 5) -> list[dict]:
        target = bigrams(question)
        scored = []
        for r in self.conn.execute("SELECT id, question FROM qa WHERE deleted_at IS NULL"):
            if r["id"] == exclude_id:
                continue
            s = dice(target, bigrams(r["question"]))
            if s >= SIMILAR_MIN:
                scored.append((s, r["id"]))
        scored.sort(key=lambda x: (-x[0], x[1]))
        top = scored[:limit]
        items = self.items([i for _, i in top])
        for item, (s, _) in zip(items, top):
            item["score"] = round(s, 2)
        return items

    def add_qa(self, category: int | str, question: str, answer: str, *, evidence: list[dict] | None = None,
               links: list[int] | None = None, force: bool = False) -> dict:
        q, a = check_question(question), check_answer(answer)
        cid = self.resolve_category(category)
        # 근거는 쓰기 전에 모두 캡처한다. 하나라도 틀리면 아무것도 쓰지 않는다
        captured = [self._capture(spec) for spec in evidence or []]
        self._check_quota("qa")
        self._check_quota("evidence", len(captured))
        if not force:
            sim = self.similar(q)
            if sim:
                return {"status": "similar_found", "similar": sim}
        verdicts = ["external" if e["kind"] in ("url", "note") else "same" for e in captured]
        morph = self._morph(q, a)
        with self._writing() as c:
            qid = c.execute(
                "INSERT INTO qa(category_id, question, answer, evidence_state, created_by, updated_by) "
                "VALUES (?,?,?,?,?,?)",
                (cid, q, a, overall_state(verdicts), self.actor, self.actor),
            ).lastrowid
            for e, v in zip(captured, verdicts):
                self._insert_evidence(qid, e, v)
            if morph:
                write_index(c, qid, *morph)
            for other in links or []:
                self._link(qid, int(other))
        return {"status": "created", "item": self.get_qa(qid)}

    def get_qa(self, id: int) -> dict:
        item = self._item(self._qa_row(id))
        link_ids = [r[0] for r in self.conn.execute(
            "SELECT CASE WHEN a_id = ? THEN b_id ELSE a_id END FROM qa_link "
            "WHERE (a_id = ? OR b_id = ?) AND deleted_at IS NULL ORDER BY id", (id, id, id))]
        item["links"] = [{"id": i["id"], "question": i["question"], "category_path": i["category_path"],
                          "archived": i["archived"]} for i in self.items(link_ids)]
        item["evidence"] = self._evidence_list(id)
        item["revisions"] = self.history("qa", id)
        return item

    # ---------- 근거 ----------

    EV_COLS = ("id, qa_id, kind, path, anchor, repo, commit_sha, paths_json, captured_json, verdict, "
               "verdict_detail, checked_at, created_at, created_by, deleted_at, version")

    def _insert_evidence(self, qa_id: int, e: dict, verdict: str) -> int:
        return self.conn.execute(
            "INSERT INTO evidence(qa_id, kind, path, anchor, repo, commit_sha, paths_json, captured_json, "
            f"verdict, checked_at, created_by, updated_by) VALUES (?,?,?,?,?,?,?,?,?,{NOW},?,?)",
            (qa_id, e["kind"], e["path"], e["anchor"], e["repo"], e["commit_sha"], e["paths_json"],
             e["captured_json"], verdict, self.actor, self.actor),
        ).lastrowid

    def _evidence_row(self, evidence_id: int) -> sqlite3.Row:
        row = self.conn.execute(f"SELECT {self.EV_COLS} FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"근거를 찾지 못했습니다: {evidence_id}")
        return row

    @staticmethod
    def _evidence_dict(r: sqlite3.Row) -> dict:
        cap = json.loads(r["captured_json"]) if r["captured_json"] else {}
        return {
            "id": r["id"], "qa_id": r["qa_id"], "kind": r["kind"], "path": r["path"], "anchor": r["anchor"],
            "repo": r["repo"], "commit": r["commit_sha"],
            "paths": json.loads(r["paths_json"]) if r["paths_json"] else None,
            "text": cap.get("text") if r["kind"] == "note" else None,
            "verdict": r["verdict"], "verdict_detail": r["verdict_detail"], "checked_at": r["checked_at"],
            "created_at": r["created_at"], "created_by": r["created_by"], "version": r["version"],
        }

    def _evidence_list(self, qa_id: int) -> list[dict]:
        rows = self.conn.execute(
            f"SELECT {self.EV_COLS} FROM evidence WHERE qa_id = ? AND deleted_at IS NULL ORDER BY id", (qa_id,))
        return [self._evidence_dict(r) for r in rows]

    def _recompute_state(self, qa_id: int, actor: str | None = None) -> None:
        verdicts = [r[0] or "unknown" for r in self.conn.execute(
            "SELECT verdict FROM evidence WHERE qa_id = ? AND deleted_at IS NULL", (qa_id,))]
        state = overall_state(verdicts)
        self.conn.execute(
            f"UPDATE qa SET evidence_state = ?, updated_by = ?, {BUMP} WHERE id = ? AND evidence_state IS NOT ?",
            (state, actor or self.actor, qa_id, state))

    def add_evidence(self, qa_id: int, spec: dict) -> dict:
        self._qa_row(qa_id)
        e = self._capture(spec)
        self._check_quota("evidence")
        with self._writing():
            self._insert_evidence(qa_id, e, "external" if e["kind"] in ("url", "note") else "same")
            self._recompute_state(qa_id)
        return self.get_qa(qa_id)

    def project_sources(self) -> dict:
        """즉석 질문 훅의 프로젝트 판정 자료: 대분류 이름과 qa-harvest 로 구축한 경로."""
        names = [r[0] for r in self.conn.execute(
            "SELECT name FROM category WHERE parent_id IS NULL AND deleted_at IS NULL ORDER BY name")]
        roots = [r[0][len("harvest:"):] for r in self.conn.execute("SELECT key FROM meta WHERE key LIKE 'harvest:%'")]
        return {"names": names, "roots": roots}

    def evidence_spec(self, evidence_id: int) -> dict:
        return spec_of(self._evidence_row(evidence_id))

    def recapture_evidence(self, evidence_id: int, captured: dict | None = None) -> dict:
        """근거를 지금 상태로 다시 기록한다. git 근거는 현재 HEAD 기준이 된다.
        원격 모드는 로컬 프록시가 그 PC 에서 다시 캡처한 값(captured)을 받는다."""
        row = self._evidence_row(evidence_id)
        e = self._capture(spec_of(row) if captured is None else {"captured": captured})
        verdict = "external" if e["kind"] in ("url", "note") else "same"
        with self._writing() as c:
            c.execute(
                "UPDATE evidence SET path = ?, repo = ?, commit_sha = ?, paths_json = ?, captured_json = ?, "
                f"verdict = ?, verdict_detail = NULL, checked_at = {NOW}, updated_by = ?, {BUMP} WHERE id = ?",
                (e["path"], e["repo"], e["commit_sha"], e["paths_json"], e["captured_json"], verdict,
                 self.actor, evidence_id))
            self._recompute_state(row["qa_id"])
        return self.get_qa(row["qa_id"])

    def archive_evidence(self, evidence_id: int) -> dict:
        row = self._evidence_row(evidence_id)
        with self._writing(grows=False) as c:
            c.execute(f"UPDATE evidence SET deleted_at = {NOW}, deleted_by = ?, updated_by = ?, {BUMP} "
                      "WHERE id = ? AND deleted_at IS NULL", (self.actor, self.actor, evidence_id))
            self._recompute_state(row["qa_id"])
        return self.get_qa(row["qa_id"])

    def recheck(self, qa_id: int | None = None) -> list[dict]:
        """근거를 다시 판정한다. 판정이 바뀐 근거만 고치고, 항목 상태가 바뀌면 qa 를 고친다.
        판정 변화는 근거 파일이 바뀌어 생긴 것이므로 누가 재검증을 눌렀든 system 주체로 기록한다."""
        if self.remote:
            raise ValidationError("원격 서버는 근거 파일을 읽을 수 없어 재검증할 수 없습니다. 사용자 PC 의 로컬 검사기가 판정합니다.")
        sql = (f"SELECT e.{self.EV_COLS.replace(', ', ', e.')} FROM evidence e JOIN qa q ON q.id = e.qa_id "
               "WHERE e.deleted_at IS NULL AND q.deleted_at IS NULL")
        params: tuple = ()
        if qa_id is not None:
            sql += " AND e.qa_id = ?"
            params = (qa_id,)
        rows = self.conn.execute(sql, params).fetchall()
        checker = Checker()
        judged = [(r, *checker.check(dict(r))) for r in rows]  # git·파일 판정은 트랜잭션 밖에서
        return self._write_verdicts(judged, mark_recheck=qa_id is None)

    def _write_verdicts(self, judged: list[tuple], mark_recheck: bool = False) -> list[dict]:
        """(근거 행, 판정, 설명) 목록을 쓴다. 판정 변화는 system 주체로 남긴다."""
        results = []
        with self._writing(grows=False) as c:
            for r, verdict, detail in judged:
                changed = verdict != r["verdict"] or (detail or None) != r["verdict_detail"]
                if changed:
                    # 판정하는 동안 다른 쪽이 재기록했으면(version 이 바뀌었으면) 옛 기록 기준 판정을 쓰지 않는다
                    changed = c.execute(
                        f"UPDATE evidence SET verdict = ?, verdict_detail = ?, checked_at = {NOW}, "
                        f"updated_by = 'system', {BUMP} WHERE id = ? AND version = ?",
                        (verdict, detail or None, r["id"], r["version"])).rowcount == 1
                results.append({"evidence_id": r["id"], "qa_id": r["qa_id"], "verdict": verdict,
                                "detail": detail, "changed": changed})
            for q in {r["qa_id"] for r, _, _ in judged}:
                self._recompute_state(q, actor="system")
            if mark_recheck:
                c.execute(f"INSERT INTO meta(key, value) VALUES ('last_recheck', {NOW}) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
        return results

    CHECKED_KINDS = ("file", "dir", "git", "githead")

    def evidence_for_check(self, origin: str, qa_id: int | None = None) -> list[dict]:
        """원격 모드: 이 PC(origin)가 캡처한 살아 있는 파일·폴더·git 근거. 로컬 검사기가 판정한다."""
        sql = (f"SELECT e.{self.EV_COLS.replace(', ', ', e.')} FROM evidence e JOIN qa q ON q.id = e.qa_id "
               "WHERE e.deleted_at IS NULL AND q.deleted_at IS NULL AND e.kind IN ('file', 'dir', 'git', 'githead') "
               "AND json_extract(e.captured_json, '$.origin') = ?")
        params: tuple = (origin,)
        if qa_id is not None:
            sql += " AND e.qa_id = ?"
            params += (qa_id,)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY e.id", params)]

    def apply_verdicts(self, results: list[dict]) -> list[dict]:
        """로컬 검사기가 보낸 [{evidence_id, version, verdict, detail}] 을 recheck 와 같은 규칙으로 쓴다.
        version 이 지금과 다른 판정(그사이 재기록됨)과 판정 대상이 아닌 근거는 쓰지 않는다."""
        judged = []
        for res in results:
            verdict = res.get("verdict")
            if verdict not in ("same", "changed", "unknown"):
                raise ValidationError(f"판정은 same·changed·unknown 중 하나여야 합니다: {verdict}")
            row = self.conn.execute(f"SELECT {self.EV_COLS} FROM evidence WHERE id = ? AND deleted_at IS NULL",
                                    (int(res["evidence_id"]),)).fetchone()
            if row is None or row["kind"] not in self.CHECKED_KINDS:
                continue
            if row["version"] != int(res["version"]):
                continue
            detail = res.get("detail")
            judged.append((row, verdict, None if detail is None else str(detail)[:500]))
        return self._write_verdicts(judged)

    def expire_verdicts(self, days: int = 30) -> int:
        """원격 모드 주기 작업: days 일 넘게 사용자 PC 가 판정하지 않은 'same' 근거를 unknown 으로 바꾼다."""
        rows = self.conn.execute(
            f"SELECT {self.EV_COLS} FROM evidence WHERE deleted_at IS NULL AND verdict = 'same' "
            "AND kind IN ('file', 'dir', 'git', 'githead') AND checked_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)",
            (f"-{int(days)} days",)).fetchall()
        detail = f"{days}일 넘게 사용자 PC 에서 판정하지 않았습니다"
        return sum(r["changed"] for r in self._write_verdicts([(r, "unknown", detail) for r in rows]))

    def update_qa(self, id: int, version: int, *, question: str | None = None, answer: str | None = None,
                  category: int | str | None = None, evidence: list[dict] | None = None,
                  note: str | None = None) -> dict:
        """evidence 를 주면 보관되지 않은 근거를 모두 보관하고 그 목록으로 바꾼다([] 는 근거 없음)."""
        sets = {}
        if question is not None:
            sets["question"] = check_question(question)
        if answer is not None:
            sets["answer"] = check_answer(answer)
        if category not in (None, ""):
            sets["category_id"] = self.resolve_category(category)
        if not sets and evidence is None:
            raise ValidationError("바꿀 값이 없습니다.")
        keep: list[int] = []
        if evidence is not None and self.remote:
            evidence, keep = self._split_kept(id, evidence)
        captured = [self._capture(spec) for spec in evidence] if evidence is not None else None  # 쓰기 전에 모두 캡처
        self._check_quota("evidence", len(captured or []))
        sets["change_note"] = note
        steps = []
        if "question" in sets or "answer" in sets:
            row = self._qa_row(id)
            morph = self._morph(sets.get("question", row["question"]), sets.get("answer", row["answer"]))
            if morph:
                steps.append(lambda c: write_index(c, id, *morph))
            else:  # 분석기가 없으면 옛 색인을 지워 재색인 대상으로 둔다
                steps.append(lambda c: c.execute("DELETE FROM qa_morph_fts WHERE rowid = ?", (id,)))
        if captured is not None:
            # 상태 재계산이 버전을 올리므로 버전을 검사하는 UPDATE 뒤, 같은 트랜잭션에서 바꾼다
            steps.append(lambda c: self._replace_evidence(c, id, captured, keep))
        self._update("qa", id, version, sets, lambda i: self._item(self._qa_row(i)),
                     after=lambda c: [step(c) for step in steps])
        return self.get_qa(id)

    def _split_kept(self, qa_id: int, specs: list[dict]) -> tuple[list[dict], list[int]]:
        """원격 모드 웹 편집: 근거 줄에 그대로 남은 파일·폴더·git 근거는 서버가 다시 캡처할 수 없으므로 기존 행을 둔다.
        (경로 또는 저장소, anchor)가 같은 살아 있는 근거가 있으면 그 행을 유지 목록으로, 나머지 spec 은 그대로 돌려준다."""
        rows = self.conn.execute("SELECT id, kind, path, repo, anchor FROM evidence WHERE qa_id = ? AND deleted_at IS NULL "
                                 "AND kind NOT IN ('url', 'note')", (qa_id,)).fetchall()
        existing = {((r["path"] or r["repo"]), r["anchor"] or None): r["id"] for r in rows}
        rest, keep = [], []
        for spec in specs:
            key = (spec.get("path") or spec.get("repo"), spec.get("anchor") or None)
            if "captured" not in spec and (spec.get("kind") or "auto") not in ("url", "note") and key in existing:
                keep.append(existing.pop(key))
            else:
                rest.append(spec)
        return rest, keep

    def _replace_evidence(self, c: sqlite3.Connection, qa_id: int, captured: list[dict], keep: list[int] = ()) -> None:
        marks = ",".join("?" * len(keep))
        c.execute(f"UPDATE evidence SET deleted_at = {NOW}, deleted_by = ?, updated_by = ?, {BUMP} "
                  f"WHERE qa_id = ? AND deleted_at IS NULL{f' AND id NOT IN ({marks})' if keep else ''}",
                  (self.actor, self.actor, qa_id, *keep))
        for e in captured:
            self._insert_evidence(qa_id, e, "external" if e["kind"] in ("url", "note") else "same")
        self._recompute_state(qa_id)

    def _set_archived(self, id: int, version: int, archived: bool, note: str | None,
                      category_id: int | None = None) -> dict:
        current = lambda i: self._item(self._qa_row(i))  # noqa: E731
        self._qa_row(id)
        deleted = f"deleted_at = {NOW}, deleted_by = ?" if archived else "deleted_at = NULL, deleted_by = NULL"
        params = [self.actor] if archived else []
        if category_id is not None:
            deleted += ", category_id = ?"
            params.append(category_id)
        with self._writing(grows=False) as c:
            n = c.execute(
                f"UPDATE qa SET {deleted}, updated_by = ?, change_note = ?, {BUMP} WHERE id = ? AND version = ?",
                params + [self.actor, note, id, version],
            ).rowcount
        if n == 0:
            raise ConflictError("다른 곳에서 먼저 고쳤습니다. 현재 값을 다시 읽고 고쳐 주세요.", current=current(id))
        return self.get_qa(id)

    def archive_qa(self, id: int, version: int, note: str | None = None) -> dict:
        return self._set_archived(id, version, True, note)

    def restore_qa(self, id: int, version: int, note: str | None = None,
                   category: int | str | None = None) -> dict:
        """분류가 삭제된 항목은 category(옮길 분류)를 주어야 복원된다."""
        if category not in (None, ""):
            cid = self.resolve_category(category)
        else:
            cid = None
            current = self._qa_row(id)["category_id"]
            if self._category(current)["archived"]:
                raise ValidationError("이 항목의 분류가 삭제되어 있습니다. 옮길 분류를 함께 주어 복원해 주세요.")
        return self._set_archived(id, version, False, note, category_id=cid)

    # ---------- 관련 질문 ----------

    def _link(self, a: int, b: int) -> None:
        if a == b:
            raise ValidationError("자기 자신과는 연결할 수 없습니다.")
        a, b = sorted((a, b))
        self._qa_row(a)
        self._qa_row(b)
        exists = self.conn.execute(
            "SELECT 1 FROM qa_link WHERE a_id = ? AND b_id = ? AND deleted_at IS NULL", (a, b)).fetchone()
        if not exists:
            self.conn.execute("INSERT INTO qa_link(a_id, b_id, created_by, updated_by) VALUES (?,?,?,?)",
                              (a, b, self.actor, self.actor))

    def link(self, a: int, b: int) -> dict:
        with self._writing():
            self._link(a, b)
        return {"a": min(a, b), "b": max(a, b), "linked": True}

    def unlink(self, a: int, b: int) -> dict:
        a, b = sorted((a, b))
        with self._writing(grows=False) as c:
            c.execute(f"UPDATE qa_link SET deleted_at = {NOW}, deleted_by = ?, updated_by = ?, {BUMP} "
                      "WHERE a_id = ? AND b_id = ? AND deleted_at IS NULL", (self.actor, self.actor, a, b))
        return {"a": a, "b": b, "linked": False}

    # ---------- 이력 ----------

    def history(self, table: str, row_id: int, limit: int = 20) -> list[dict]:
        if table not in AUDITED:
            raise ValidationError(f"이력이 없는 테이블입니다: {table}")
        rows = self.conn.execute(
            "SELECT id, op, actor, at, before_json, after_json FROM revision "
            "WHERE table_name = ? AND row_id = ? ORDER BY id DESC LIMIT ?", (table, row_id, limit))
        return [{"id": r["id"], "op": r["op"], "actor": r["actor"], "at": r["at"],
                 "before": json.loads(r["before_json"]) if r["before_json"] else None,
                 "after": json.loads(r["after_json"]) if r["after_json"] else None} for r in rows]

    # ---------- 검색·사전 ----------

    def _morph(self, question: str, answer: str) -> tuple[str, str] | None:
        """형태소 색인 본문과 분석기 버전. 쓰기 트랜잭션을 열기 전에 부른다."""
        if self.analyzer is None or not self.analyzer.ready(self.conn):
            return None
        return self.analyzer.index_text(question + "\n" + answer), self.analyzer.version

    def search(self, query: str, **opts) -> dict:
        return search_mod.search(self, query, analyzer=self.analyzer, **opts)

    def search_many(self, queries: list[str], **opts) -> dict:
        return search_mod.search_many(self, queries, analyzer=self.analyzer, **opts)

    # ---------- 수확 기록 (qa-harvest Skill 이 다음 실행에서 바뀐 것만 다루려고 남긴다) ----------

    @staticmethod
    def _harvest_root(root: str) -> str:
        return root.replace("\\", "/").rstrip("/").lower()

    def harvest_get(self, root: str) -> dict | None:
        value = get_meta(self.conn, "harvest:" + self._harvest_root(root))
        return json.loads(value) if value else None

    def harvest_set(self, root: str, state: dict) -> dict:
        norm = self._harvest_root(root)
        at = self.conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
        record = {**state, "root": norm, "at": at}
        with self._writing() as c:
            set_meta(c, "harvest:" + norm, json.dumps(record, ensure_ascii=False))
        return record

    def harvest_list(self) -> list[dict]:
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT value FROM meta WHERE key LIKE 'harvest:%' ORDER BY key")]

    def dictionary(self) -> dict:
        return dict_mod.snapshot(self.conn)

    def dict_action(self, action: str, **kw) -> dict:
        if self.remote and action.startswith("user_word"):
            # 형태소 분석기(약 600 MB)는 서버에 하나라 사용자 단어를 사용자마다 둘 수 없다
            raise ValidationError("원격 서버에서는 형태소 사용자 단어를 바꿀 수 없습니다. 동의어·불용어는 쓸 수 있습니다.")
        with self._writing() as c:
            reindex = dict_mod.apply(c, self.actor, action, **kw)
        return {"snapshot": dict_mod.snapshot(self.conn), "reindex_needed": reindex}
