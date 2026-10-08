"""Kiwi 형태소 분석기와 형태소 색인(qa_morph_fts).

- Kiwi 는 처음 쓸 때 띄운다(약 2.5초). 분석은 쓰기 트랜잭션 밖에서 한다.
- 분석기 버전은 DB 의 사용자 단어 목록에서 계산한다. web 과 MCP 가 각자 띄운 Kiwi 는
  쓰기·검색 전에 이 버전을 비교해 다르면 다시 띄운다(spec §6.4).
- 색인 행의 ver 가 현재 버전과 다르거나 행이 없으면 재색인 대상이다.
"""
import functools
import hashlib
import json
import sqlite3
import threading
from importlib import metadata
from typing import Callable, Iterable

from kb.dictionary import load_stopwords, load_user_words

KEEP_TAGS = frozenset({"NNG", "NNP", "NR", "NP", "VV", "VA", "XR", "SL", "SH", "SN"})
XSN_BASE = frozenset({"NNG", "NNP", "XR"})
RULES = "v1;keep=" + ",".join(sorted(KEEP_TAGS)) + ";xsn-join;lower"


@functools.lru_cache(maxsize=1)
def kiwi_version() -> str | None:
    try:
        return metadata.version("kiwipiepy")
    except metadata.PackageNotFoundError:
        return None


def compute_version(user_words: Iterable[tuple[str, str]]) -> str:
    payload = json.dumps({"rules": RULES, "words": sorted(user_words)}, ensure_ascii=False)
    return f"kiwipiepy-{kiwi_version()}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def _default_loader():
    from kiwipiepy import Kiwi
    return Kiwi()


def _tag(t) -> str:
    return t.tag.split("-")[0]


def _merge(spans: list[tuple[int, int]]) -> list[list[int]]:
    out: list[list[int]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


class Analyzer:
    def __init__(self, loader: Callable | None = None):
        self._loader = loader or _default_loader
        self._lock = threading.RLock()
        self._kiwi = None
        self.version: str | None = None
        self.enabled = True
        self.error: str | None = None

    def ready(self, conn: sqlite3.Connection) -> bool:
        """DB 사용자 단어 기준 버전으로 Kiwi 를 준비한다. 쓸 수 없으면 False."""
        if not self.enabled:
            return False
        words = load_user_words(conn)
        want = compute_version(words)
        with self._lock:
            if self._kiwi is None or self.version != want:
                try:
                    kiwi = self._loader()
                    for w, tag in words:
                        kiwi.add_user_word(w, tag)
                except Exception as e:  # noqa: BLE001 - 어떤 실패든 형태소 계층을 끈다
                    self.enabled, self.error, self._kiwi = False, f"{type(e).__name__}: {e}", None
                    return False
                self._kiwi, self.version = kiwi, want
        return True

    def _tokens(self, text: str):
        with self._lock:
            return self._kiwi.tokenize(text)

    def _units(self, text: str) -> list[tuple[str, int, int]]:
        """(형태, 시작, 끝) 목록. 명사 바로 뒤 XSN 은 붙인 형태도 넣는다(암호 + 화 → 암호화)."""
        toks = self._tokens(text)
        out = []
        for i, t in enumerate(toks):
            tag = _tag(t)
            if tag in KEEP_TAGS:
                out.append((t.form.lower(), t.start, t.start + t.len))
            if tag == "XSN" and i > 0:
                p = toks[i - 1]
                if _tag(p) in XSN_BASE and p.start + p.len == t.start:
                    out.append(((p.form + t.form).lower(), p.start, t.start + t.len))
        return out

    def morphs(self, text: str) -> list[str]:
        return [u[0] for u in self._units(text)]

    def index_text(self, text: str) -> str:
        return " ".join(self.morphs(text))

    def query_morphs(self, text: str) -> list[str]:
        return list(dict.fromkeys(self.morphs(text)))

    def contains_any(self, text: str, morphs: Iterable[str]) -> bool:
        return bool(set(self.morphs(text)) & set(morphs))

    def spans(self, text: str, morphs: Iterable[str]) -> list[list[int]]:
        want = set(morphs)
        return _merge([(s, e) for form, s, e in self._units(text) if form in want])

    @staticmethod
    def _phrase(m: str) -> str:
        return '"' + m.replace('"', '""') + '"'

    def match_all(self, conn: sqlite3.Connection, morphs: list[str]) -> set[int]:
        if not morphs:
            return set()
        expr = " AND ".join(self._phrase(m) for m in morphs)
        return {r[0] for r in conn.execute("SELECT rowid FROM qa_morph_fts WHERE qa_morph_fts MATCH ?", (expr,))}

    def match_ratio(self, conn: sqlite3.Connection, query: str) -> dict[int, float]:
        """질의 형태소(불용어 제외) 중 몇 개를 가졌는지의 비율."""
        ms = [m for m in self.query_morphs(query) if m not in load_stopwords(conn)]
        counts: dict[int, int] = {}
        for m in ms:
            for (rid,) in conn.execute("SELECT rowid FROM qa_morph_fts WHERE qa_morph_fts MATCH ?",
                                       (self._phrase(m),)):
                counts[rid] = counts.get(rid, 0) + 1
        return {rid: n / len(ms) for rid, n in counts.items()} if ms else {}


def write_index(conn: sqlite3.Connection, qa_id: int, body: str, version: str) -> None:
    """호출자의 트랜잭션 안에서 색인 행을 바꾼다."""
    conn.execute("DELETE FROM qa_morph_fts WHERE rowid = ?", (qa_id,))
    conn.execute("INSERT INTO qa_morph_fts(rowid, body, ver) VALUES (?, ?, ?)", (qa_id, body, version))


def _pending_sql(limit: bool) -> str:
    return ("SELECT q.id, q.question, q.answer FROM qa q LEFT JOIN qa_morph_fts m ON m.rowid = q.id "
            "WHERE m.rowid IS NULL OR m.ver IS NOT ?" + (" LIMIT ?" if limit else ""))


def pending_count(conn: sqlite3.Connection) -> int:
    want = compute_version(load_user_words(conn))
    return conn.execute(f"SELECT count(*) FROM ({_pending_sql(False)})", (want,)).fetchone()[0]


def reindex(conn: sqlite3.Connection, analyzer: Analyzer, *, batch: int = 500) -> int:
    """버전이 다르거나 색인이 없는 행을 다시 분석한다. 분석은 트랜잭션 밖, 쓰기는 묶음마다 한 번."""
    from kb.db import set_meta, tx

    done = 0
    while analyzer.ready(conn):
        version = analyzer.version
        rows = conn.execute(_pending_sql(True), (version, batch)).fetchall()
        if not rows:
            break
        bodies = [(r[0], analyzer.index_text(r[1] + "\n" + r[2])) for r in rows]
        with tx(conn):
            for qa_id, body in bodies:
                write_index(conn, qa_id, body, version)
            set_meta(conn, "analyzer_version", version)
        done += len(rows)
    return done
