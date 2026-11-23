"""오류 형식과 SQLite 오류의 메시지 변환."""
import re
import sqlite3

MESSAGES = {
    "qa_question_len": "The question must be 1-300 characters.",
    "qa_question_line": "The question must be a single line.",
    "qa_answer_len": "The answer must be 1-800 characters.",
    "qa_answer_lines": "The answer can have at most 6 lines.",
    "qa_evidence_state": "The evidence state value is invalid.",
    "category_name_len": "The category name must be a single line of 1-60 characters.",
    "category_description_len": "The category description can be at most 300 characters.",
    "category_depth": "Categories can only be two levels deep (top level and subcategory).",
    "category_name_uq": "A category with the same name already exists under the same parent.",
    "qa_link_order": "The related-question link value is invalid.",
    "qa_link_uq": "These questions are already linked.",
    "synonym_term_len": "A synonym must be 1-60 characters.",
    "synonym_term_uq": "This word is already in another synonym group.",
    "user_word_len": "A user word must be 1-40 characters with no spaces.",
    "user_word_tag": "The user word tag must be one of NNP, NNG, SL, XR, VV, VA.",
    "user_word_uq": "This user word is already registered.",
    "stopword_len": "A stopword must be 1-20 characters with no spaces.",
    "stopword_uq": "This stopword is already registered.",
    "evidence_kind": "The evidence kind must be one of file, dir, git, githead, url, note, github.",
    "managed_column": "Creation info, update time and version are set by the store.",
    "no_physical_delete": "Rows cannot be deleted. Archive them instead.",
    "revision_append_only": "History cannot be changed or deleted.",
    "foreign_key": "The referenced target does not exist.",
}


class KbError(Exception):
    def __init__(self, message: str, *, code: str = "error"):
        super().__init__(message)
        self.message = message
        self.code = code


class ValidationError(KbError):
    pass


class NotFoundError(KbError):
    def __init__(self, message: str):
        super().__init__(message, code="not_found")


class ConflictError(KbError):
    def __init__(self, message: str, current: dict):
        super().__init__(message, code="conflict")
        self.current = current


_CHECK = re.compile(r"CHECK constraint failed: (\w+)")
_UNIQUE_INDEX = re.compile(r"UNIQUE constraint failed: index '(\w+)'")


def from_sqlite(exc: sqlite3.Error) -> KbError:
    msg = str(exc)
    code = None
    if msg in MESSAGES:
        code = msg  # 트리거의 RAISE(ABORT, '<code>')
    elif m := _CHECK.search(msg):
        code = m.group(1)
    elif m := _UNIQUE_INDEX.search(msg):
        code = m.group(1)
    elif "FOREIGN KEY constraint failed" in msg:
        code = "foreign_key"
    if code in MESSAGES:
        return ValidationError(MESSAGES[code], code=code)
    return KbError(f"Database error: {msg}", code="db")
