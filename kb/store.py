"""쓰기·조회 진입점. web, MCP, 주기 작업이 모두 이 클래스만 쓴다.

쓰기 주체(actor)는 생성할 때 정해지고 호출자가 바꿀 수 없다.
"""
import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Literal

from kb.db import BUMP, NOW, tx
from kb.errors import ConflictError, NotFoundError, ValidationError, from_sqlite
from kb.schema import AUDITED
from kb.text import check_answer, check_name, check_question, clean

Actor = Literal["user", "claude", "system"]
ACTORS = ("user", "claude", "system")
SIMILAR_MIN = 0.6

QA_COLS = ("id, category_id, question, answer, evidence_state, reviewed_at, reviewed_by, "
           "deleted_at, created_at, created_by, updated_at, updated_by, version")


def bigrams(text: str) -> set[str]:
    s = "".join(text.lower().split())
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def dice(a: set[str], b: set[str]) -> float:
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


class Store:
    def __init__(self, conn: sqlite3.Connection, actor: Actor, analyzer=None):
        if actor not in ACTORS:
            raise ValueError(f"알 수 없는 쓰기 주체: {actor}")
        self.conn = conn
        self.actor = actor
        self.analyzer = analyzer

    # ---------- 공통 ----------

    @contextmanager
    def _writing(self) -> Iterator[sqlite3.Connection]:
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
            "reviewed": row["reviewed_at"] is not None,
            "reviewed_at": row["reviewed_at"],
            "reviewed_by": row["reviewed_by"],
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
            raise NotFoundError(f"Q&A 를 찾지 못했다: {id}")
        return row

    def items(self, ids: list[int]) -> list[dict]:
        """id 순서를 유지한 item 목록."""
        if not ids:
            return []
        paths = self._paths()
        marks = ",".join("?" * len(ids))
        rows = {r["id"]: r for r in self.conn.execute(f"SELECT {QA_COLS} FROM qa WHERE id IN ({marks})", ids)}
        return [self._item(rows[i], paths) for i in ids if i in rows]

    def _update(self, table: str, id: int, version: int | None, sets: dict, current) -> None:
        """version 이 None 이면 버전 검사 없이 고친다. 0행이면 충돌 또는 없음."""
        cols = ", ".join(f"{k} = ?" for k in sets) + f", updated_by = ?, {BUMP}"
        params = list(sets.values()) + [self.actor, id]
        where = "id = ?"
        if version is not None:
            where += " AND version = ?"
            params.append(version)
        with self._writing() as c:
            n = c.execute(f"UPDATE {table} SET {cols} WHERE {where}", params).rowcount
        if n == 0:
            cur = current(id)  # 없으면 NotFoundError
            raise ConflictError("다른 곳에서 먼저 고쳤다. 현재 값을 다시 읽고 고친다.", current=cur)

    # ---------- 분류 ----------

    def _category(self, id: int, paths: dict[int, str] | None = None) -> dict:
        row = self.conn.execute(
            "SELECT id, parent_id, name, description, deleted_at, version FROM category WHERE id = ?", (id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"분류를 찾지 못했다: {id}")
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
                raise ValidationError("분류는 대분류와 중분류 2단계까지만 만들 수 있다.", code="category_depth")
        if row is None:
            raise NotFoundError(f"분류를 찾지 못했다: {ref}")
        return row[0]

    def add_category(self, name: str, parent: int | str | None = None, description: str = "") -> dict:
        name = check_name(name)
        parent_id = self.resolve_category(parent) if parent not in (None, "") else None
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
            raise ValidationError("바꿀 값이 없다.")
        sets["change_note"] = note
        self._update("category", id, version, sets, self._category)
        return self._category(id)

    def archive_category(self, id: int, version: int, note: str | None = None) -> dict:
        self._category(id)
        n_qa = self.conn.execute(
            "SELECT count(*) FROM qa WHERE category_id = ? AND deleted_at IS NULL", (id,)).fetchone()[0]
        n_child = self.conn.execute(
            "SELECT count(*) FROM category WHERE parent_id = ? AND deleted_at IS NULL", (id,)).fetchone()[0]
        if n_qa or n_child:
            raise ValidationError(
                f"분류에 보관되지 않은 Q&A {n_qa}건, 하위 분류 {n_child}개가 있다. 먼저 옮기거나 보관한다.")
        with self._writing() as c:
            n = c.execute(
                f"UPDATE category SET deleted_at = {NOW}, deleted_by = ?, updated_by = ?, change_note = ?, {BUMP} "
                "WHERE id = ? AND version = ? AND deleted_at IS NULL",
                (self.actor, self.actor, note, id, version),
            ).rowcount
        if n == 0:
            raise ConflictError("다른 곳에서 먼저 고쳤다. 현재 값을 다시 읽고 고친다.", current=self._category(id))
        return self._category(id)

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

    def add_qa(self, category: int | str, question: str, answer: str, *,
               links: list[int] | None = None, force: bool = False) -> dict:
        q, a = check_question(question), check_answer(answer)
        cid = self.resolve_category(category)
        if not force:
            sim = self.similar(q)
            if sim:
                return {"status": "similar_found", "similar": sim}
        with self._writing() as c:
            qid = c.execute(
                "INSERT INTO qa(category_id, question, answer, created_by, updated_by) VALUES (?,?,?,?,?)",
                (cid, q, a, self.actor, self.actor),
            ).lastrowid
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

    def _evidence_list(self, qa_id: int) -> list[dict]:
        return []

    def update_qa(self, id: int, version: int, *, question: str | None = None, answer: str | None = None,
                  category: int | str | None = None, note: str | None = None) -> dict:
        sets = {}
        if question is not None:
            sets["question"] = check_question(question)
        if answer is not None:
            sets["answer"] = check_answer(answer)
        if category not in (None, ""):
            sets["category_id"] = self.resolve_category(category)
        if not sets:
            raise ValidationError("바꿀 값이 없다.")
        sets["change_note"] = note
        self._update("qa", id, version, sets, lambda i: self._item(self._qa_row(i)))
        return self.get_qa(id)

    def _set_archived(self, id: int, version: int, archived: bool, note: str | None) -> dict:
        current = lambda i: self._item(self._qa_row(i))  # noqa: E731
        self._qa_row(id)
        deleted = f"deleted_at = {NOW}, deleted_by = ?" if archived else "deleted_at = NULL, deleted_by = NULL"
        params = [self.actor] if archived else []
        with self._writing() as c:
            n = c.execute(
                f"UPDATE qa SET {deleted}, updated_by = ?, change_note = ?, {BUMP} WHERE id = ? AND version = ?",
                params + [self.actor, note, id, version],
            ).rowcount
        if n == 0:
            raise ConflictError("다른 곳에서 먼저 고쳤다. 현재 값을 다시 읽고 고친다.", current=current(id))
        return self.get_qa(id)

    def archive_qa(self, id: int, version: int, note: str | None = None) -> dict:
        return self._set_archived(id, version, True, note)

    def restore_qa(self, id: int, version: int, note: str | None = None) -> dict:
        return self._set_archived(id, version, False, note)

    def review_qa(self, id: int) -> dict:
        row = self._qa_row(id)
        if row["reviewed_at"] is None:
            with self._writing() as c:
                c.execute(f"UPDATE qa SET reviewed_at = {NOW}, reviewed_by = ?, updated_by = ?, {BUMP} WHERE id = ?",
                          (self.actor, self.actor, id))
        return self.get_qa(id)

    # ---------- 관련 질문 ----------

    def _link(self, a: int, b: int) -> None:
        if a == b:
            raise ValidationError("자기 자신과는 연결할 수 없다.")
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
        with self._writing() as c:
            c.execute(f"UPDATE qa_link SET deleted_at = {NOW}, deleted_by = ?, updated_by = ?, {BUMP} "
                      "WHERE a_id = ? AND b_id = ? AND deleted_at IS NULL", (self.actor, self.actor, a, b))
        return {"a": a, "b": b, "linked": False}

    # ---------- 이력 ----------

    def history(self, table: str, row_id: int, limit: int = 20) -> list[dict]:
        if table not in AUDITED:
            raise ValidationError(f"이력이 없는 테이블이다: {table}")
        rows = self.conn.execute(
            "SELECT id, op, actor, at, before_json, after_json FROM revision "
            "WHERE table_name = ? AND row_id = ? ORDER BY id DESC LIMIT ?", (table, row_id, limit))
        return [{"id": r["id"], "op": r["op"], "actor": r["actor"], "at": r["at"],
                 "before": json.loads(r["before_json"]) if r["before_json"] else None,
                 "after": json.loads(r["after_json"]) if r["after_json"] else None} for r in rows]
