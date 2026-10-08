"""DDL 생성. 감사 대상 테이블마다 같은 감독 트리거를 만든다.

감독 규칙(어느 진입점으로 쓰든 DB 가 강제한다)
- 생성 정보는 바꿀 수 없다. 수정 시각과 버전은 DB 가 정한다(<t>_guard, <t>_touch).
- 모든 INSERT·UPDATE 는 revision 에 전후 값이 남는다. revision 은 추가만 된다.
- 물리 DELETE 는 거부한다. 삭제는 deleted_at 을 채우는 보관 처리다.
"""

SCHEMA_VERSION = "1"
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
ACTORS = "('user','claude','system')"

# 테이블 -> revision 에 남길 열 (공통 열 deleted_at, deleted_by, change_note 는 자동 추가)
AUDITED: dict[str, list[str]] = {
    "category": ["parent_id", "name", "description"],
    "qa": ["category_id", "question", "answer", "evidence_state", "reviewed_at", "reviewed_by"],
    "evidence": ["qa_id", "kind", "path", "anchor", "repo", "commit_sha", "paths_json",
                 "captured_json", "verdict", "verdict_detail", "checked_at"],
    "qa_link": ["a_id", "b_id"],
    "synonym": ["group_id", "term"],
    "user_word": ["word", "tag"],
    "stopword": ["word"],
}

COMMON = f"""
    created_at TEXT NOT NULL DEFAULT ({NOW}),
    created_by TEXT NOT NULL CHECK (created_by IN {ACTORS}),
    updated_at TEXT NOT NULL DEFAULT ({NOW}),
    updated_by TEXT NOT NULL CHECK (updated_by IN {ACTORS}),
    deleted_at TEXT,
    deleted_by TEXT CHECK (deleted_by IS NULL OR deleted_by IN {ACTORS}),
    version INTEGER NOT NULL DEFAULT 1,
    change_note TEXT"""

# 공백·줄바꿈·탭을 지운 사본. "여유공간" 이 "여유 공간" 과 일치하게 한다
NS = "replace(replace(replace({x}, ' ', ''), char(10), ''), char(9), '')"

TABLES = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS category (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES category(id),
    name TEXT NOT NULL CONSTRAINT category_name_len
        CHECK (length(name) BETWEEN 1 AND 60 AND instr(name, char(10)) = 0),
    description TEXT NOT NULL DEFAULT '' CONSTRAINT category_description_len
        CHECK (length(description) <= 300),{COMMON}
);
CREATE UNIQUE INDEX IF NOT EXISTS category_name_uq
    ON category(ifnull(parent_id, 0), name) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS qa (
    id INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES category(id),
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    evidence_state TEXT NOT NULL DEFAULT 'none',
    reviewed_at TEXT,
    reviewed_by TEXT,{COMMON},
    CONSTRAINT qa_question_len CHECK (length(trim(question)) BETWEEN 1 AND 150),
    CONSTRAINT qa_question_line CHECK (instr(question, char(10)) = 0 AND instr(question, char(13)) = 0),
    CONSTRAINT qa_answer_len CHECK (length(trim(answer)) BETWEEN 1 AND 400),
    CONSTRAINT qa_answer_lines CHECK (
        length(answer) - length(replace(answer, char(10), '')) <= 1 AND instr(answer, char(13)) = 0),
    CONSTRAINT qa_evidence_state CHECK (evidence_state IN ('none','external','trusted','stale','unknown'))
);
CREATE INDEX IF NOT EXISTS qa_category ON qa(category_id);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY,
    qa_id INTEGER NOT NULL REFERENCES qa(id),
    kind TEXT NOT NULL CONSTRAINT evidence_kind
        CHECK (kind IN ('file','dir','git','githead','url','note')),
    path TEXT,
    anchor TEXT,
    repo TEXT,
    commit_sha TEXT,
    paths_json TEXT,
    captured_json TEXT,
    verdict TEXT CHECK (verdict IS NULL OR verdict IN ('same','changed','unknown','external')),
    verdict_detail TEXT,
    checked_at TEXT,{COMMON}
);
CREATE INDEX IF NOT EXISTS evidence_qa ON evidence(qa_id);

CREATE TABLE IF NOT EXISTS qa_link (
    id INTEGER PRIMARY KEY,
    a_id INTEGER NOT NULL REFERENCES qa(id),
    b_id INTEGER NOT NULL REFERENCES qa(id),{COMMON},
    CONSTRAINT qa_link_order CHECK (a_id < b_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS qa_link_uq ON qa_link(a_id, b_id) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS synonym (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL,
    term TEXT NOT NULL CONSTRAINT synonym_term_len CHECK (length(term) BETWEEN 1 AND 60),{COMMON}
);
CREATE UNIQUE INDEX IF NOT EXISTS synonym_term_uq ON synonym(lower(term)) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS user_word (
    id INTEGER PRIMARY KEY,
    word TEXT NOT NULL CONSTRAINT user_word_len
        CHECK (length(word) BETWEEN 1 AND 40 AND instr(word, ' ') = 0),
    tag TEXT NOT NULL DEFAULT 'NNP' CONSTRAINT user_word_tag
        CHECK (tag IN ('NNP','NNG','SL','XR','VV','VA')),{COMMON}
);
CREATE UNIQUE INDEX IF NOT EXISTS user_word_uq ON user_word(word) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS stopword (
    id INTEGER PRIMARY KEY,
    word TEXT NOT NULL CONSTRAINT stopword_len
        CHECK (length(word) BETWEEN 1 AND 20 AND instr(word, ' ') = 0),{COMMON}
);
CREATE UNIQUE INDEX IF NOT EXISTS stopword_uq ON stopword(word) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS revision (
    id INTEGER PRIMARY KEY,
    table_name TEXT NOT NULL,
    row_id INTEGER NOT NULL,
    op TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    actor TEXT NOT NULL,
    at TEXT NOT NULL DEFAULT ({NOW})
);
CREATE INDEX IF NOT EXISTS revision_row ON revision(table_name, row_id);

CREATE TRIGGER IF NOT EXISTS revision_no_update BEFORE UPDATE ON revision
BEGIN SELECT RAISE(ABORT, 'revision_append_only'); END;
CREATE TRIGGER IF NOT EXISTS revision_no_delete BEFORE DELETE ON revision
BEGIN SELECT RAISE(ABORT, 'revision_append_only'); END;

CREATE TRIGGER IF NOT EXISTS category_depth_ins BEFORE INSERT ON category
WHEN NEW.parent_id IS NOT NULL
 AND (SELECT parent_id FROM category WHERE id = NEW.parent_id) IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'category_depth'); END;
CREATE TRIGGER IF NOT EXISTS category_depth_upd BEFORE UPDATE OF parent_id ON category
WHEN NEW.parent_id IS NOT NULL AND (
    NEW.parent_id = NEW.id
    OR (SELECT parent_id FROM category WHERE id = NEW.parent_id) IS NOT NULL
    OR EXISTS (SELECT 1 FROM category WHERE parent_id = NEW.id))
BEGIN SELECT RAISE(ABORT, 'category_depth'); END;

CREATE VIRTUAL TABLE IF NOT EXISTS qa_fts USING fts5(
    question, answer, question_ns, answer_ns, tokenize = 'trigram');
CREATE TRIGGER IF NOT EXISTS qa_fts_ins AFTER INSERT ON qa BEGIN
    INSERT INTO qa_fts(rowid, question, answer, question_ns, answer_ns)
    VALUES (NEW.id, NEW.question, NEW.answer, {NS.format(x='NEW.question')}, {NS.format(x='NEW.answer')});
END;
CREATE TRIGGER IF NOT EXISTS qa_fts_upd AFTER UPDATE OF question, answer ON qa BEGIN
    DELETE FROM qa_fts WHERE rowid = OLD.id;
    INSERT INTO qa_fts(rowid, question, answer, question_ns, answer_ns)
    VALUES (NEW.id, NEW.question, NEW.answer, {NS.format(x='NEW.question')}, {NS.format(x='NEW.answer')});
END;

-- 형태소 색인은 앱이 쓴다. ver 는 그 행을 분석한 분석기 버전이다
CREATE VIRTUAL TABLE IF NOT EXISTS qa_morph_fts USING fts5(body, ver UNINDEXED, tokenize = 'unicode61');
"""

SEED = """
INSERT INTO stopword(word, created_by, updated_by)
SELECT w, 'system', 'system' FROM (SELECT '때' AS w UNION ALL SELECT '경우')
WHERE NOT EXISTS (SELECT 1 FROM meta WHERE key = 'schema_version');
INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '{version}');
"""


def _json(prefix: str, cols: list[str]) -> str:
    cols = cols + ["deleted_at", "deleted_by", "change_note"]
    return "json_object(" + ", ".join(f"'{c}', {prefix}.{c}" for c in cols) + ")"


def _audit_triggers(table: str, cols: list[str]) -> str:
    return f"""
CREATE TRIGGER IF NOT EXISTS {table}_guard BEFORE UPDATE ON {table}
WHEN NEW.created_at IS NOT OLD.created_at OR NEW.created_by IS NOT OLD.created_by
  OR ((NEW.updated_at IS NOT OLD.updated_at) <> (NEW.version IS NOT OLD.version))
BEGIN SELECT RAISE(ABORT, 'managed_column'); END;

CREATE TRIGGER IF NOT EXISTS {table}_touch AFTER UPDATE ON {table}
WHEN NEW.version = OLD.version
BEGIN UPDATE {table} SET updated_at = {NOW}, version = OLD.version + 1 WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS {table}_rev_ins AFTER INSERT ON {table} BEGIN
    INSERT INTO revision(table_name, row_id, op, before_json, after_json, actor)
    VALUES ('{table}', NEW.id, 'insert', NULL, {_json('NEW', cols)}, NEW.created_by);
END;

CREATE TRIGGER IF NOT EXISTS {table}_rev_upd AFTER UPDATE ON {table}
WHEN NEW.version = OLD.version BEGIN
    INSERT INTO revision(table_name, row_id, op, before_json, after_json, actor)
    VALUES ('{table}', NEW.id,
        CASE WHEN OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL THEN 'archive'
             WHEN OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL THEN 'restore'
             ELSE 'update' END,
        {_json('OLD', cols)}, {_json('NEW', cols)}, NEW.updated_by);
END;

CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
BEGIN SELECT RAISE(ABORT, 'no_physical_delete'); END;
"""


def ddl() -> str:
    triggers = "".join(_audit_triggers(t, cols) for t, cols in AUDITED.items())
    return TABLES + triggers + SEED.format(version=SCHEMA_VERSION)
