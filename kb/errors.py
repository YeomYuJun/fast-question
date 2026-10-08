"""오류 형식과 SQLite 오류의 한국어 변환."""
import re
import sqlite3

MESSAGES = {
    "qa_question_len": "질문은 1~150자로 쓴다.",
    "qa_question_line": "질문은 한 줄로 쓴다.",
    "qa_answer_len": "답변은 1~400자로 쓴다.",
    "qa_answer_lines": "답변은 2줄까지 쓸 수 있다.",
    "qa_evidence_state": "근거 상태 값이 올바르지 않다.",
    "category_name_len": "분류 이름은 1~60자 한 줄로 쓴다.",
    "category_description_len": "분류 설명은 300자까지 쓸 수 있다.",
    "category_depth": "분류는 대분류와 중분류 2단계까지만 만들 수 있다.",
    "category_name_uq": "같은 상위 분류 아래에 같은 이름의 분류가 이미 있다.",
    "qa_link_order": "관련 질문 연결 값이 올바르지 않다.",
    "qa_link_uq": "이미 연결된 질문이다.",
    "synonym_term_len": "동의어는 1~60자로 쓴다.",
    "synonym_term_uq": "이미 다른 동의어 그룹에 있는 단어다.",
    "user_word_len": "사용자 단어는 1~40자, 공백 없이 쓴다.",
    "user_word_tag": "사용자 단어 품사는 NNP, NNG, SL, XR, VV, VA 중 하나다.",
    "user_word_uq": "이미 등록된 사용자 단어다.",
    "stopword_len": "불용어는 1~20자, 공백 없이 쓴다.",
    "stopword_uq": "이미 등록된 불용어다.",
    "evidence_kind": "근거 형식은 file, dir, git, githead, url, note 중 하나다.",
    "managed_column": "생성 정보, 수정 시각, 버전은 DB 가 정한다.",
    "no_physical_delete": "삭제 대신 보관 처리를 쓴다.",
    "revision_append_only": "이력은 고치거나 지울 수 없다.",
    "foreign_key": "가리키는 대상이 없다.",
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
    return KbError(f"DB 오류: {msg}", code="db")
