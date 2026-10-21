"""MCP 서버 안내문. 원격 서버(mcp_server/server.py)와 PC 클라이언트의 로컬 프록시(mcp_server/proxy.py)가 같은 문장을 쓴다.

프록시가 서버 모듈(형태소 분석기·저장소)을 불러오지 않도록 이 모듈은 다른 모듈을 import 하지 않는다.
"""
INSTRUCTIONS = """\
fast-question 은 사용자의 질문·답변 지식 DB 다. 사람은 등록분을 검토하지 않으므로 아래 양식을 지킨다. 규칙 원본은 qa Skill 의 rules.md 다. Skill 이 없으면 아래 요약을 따른다.
- 질문은 한 줄(150자 이하)의 묻는 문장이다. 질문만 읽어도 대상이 식별되게 대상 이름을 넣고, 대상이 일반 명사면 시스템 이름을 붙인다.
  답변은 400자·6줄 이하다. 첫 줄은 질문에 바로 답하는 1~2문장(120자 이하), 둘째 줄부터는 필요할 때만 "- " 로 시작하는 한 사실씩의 개조체 항목이다.
  사실(지금 동작)만 쓰고 비유를 쓰지 않는다. 결정 경위·개발 이력, 확인하지 않은 추정, 진행 상태, 비밀값·개인정보·IP 주소는 넣지 않는다.
  답변 양식, 금지 표현, 민감 정보는 kb_add·kb_update 가 검사해 위반이면 거부한다.
- 지금 작업 중인 프로젝트와 무관한 평시 질문은 사용자가 /qa 로 요청할 때만 등록한다.
- 분류는 kb_categories 를 보고 고른다. 맞는 것이 없으면 사용자에게 묻지 않고 kb_add_category 로 만든다(대분류 = 작업 주제, 중분류 = 대상 이름).
- 로컬 파일에서 확인한 사실은 evidence 에 {"kind": "auto", "path": "<파일 절대경로>", "anchor": "<심볼이나 찾을 문자열>"} 를 단다.
  일반 지식(k8s 개념, 수학 등)은 근거 없이 쓴다. 사람에게 확인한 사실은 {"kind": "note", "text": "누가 어떻게 확인"} 을 단다.
- 등록 전에 kb_search 로 같은 질문이 있는지 본다. 있으면 kb_get 으로 version 을 읽고 kb_update 로 고친다.
- kb_search 가 0건이면 표현을 바꿔 두 번까지 다시 검색한다(형태소·동의어로도 못 찾는 표현이 있다).
- kb_search 결과에 cleanup(정리 대상) 표시가 붙은 항목은 사용자에게 묻지 않고 그 자리에서 정리한다. kb_cleanup_list 로 내용을 보고, 고치거나 보관한 뒤 kb_cleanup_resolve 로 닫는다.
- 분류 삭제(kb_delete_category)는 하위 분류와 Q&A 를 모두 함께 삭제한다. 먼저 confirm 없이 불러 받은 경고문을 사용자에게 보여 주고, 승인을 받은 뒤에만 confirm=true 로 부른다.
- 등록·수정·조회한 Q&A 를 사용자에게 알릴 때는 결과의 url(웹 화면에서 그 항목을 여는 주소)을 함께 보여 준다.
"""

# kb_add_many 도구 설명. 서버(mcp_server/server.py)와 PC 프록시(mcp_server/proxy.py)가 함께 쓴다(프록시는 서버 모듈을 import 하지 않는다)
ADD_MANY_DOC = """Q&A 를 한 번에 여러 건(최대 100) 등록한다. 항목은 kb_add 의 인자와 같다:
{"category", "question", "answer", "evidence"?, "links"?, "force"?}. 항목마다 따로 처리해 하나가 실패해도
나머지는 등록된다. results[i].status 는 created / similar_found(후보 포함, 등록 안 됨) / error 다. 항목의 다른 키는 무시한다.
"""
ADD_MANY_FILE = """file 에 항목 배열 JSON 파일의 절대경로를 주면 그 파일을 읽는다. 초안 파일을 그대로 넣을 때는 file 을 쓴다
(항목을 다시 옮겨 적으면 글자가 바뀔 수 있다)."""
# 원격 직접 연결(claude.ai·한 줄 연결)은 사용자 PC 의 파일을 읽을 수 없다. 로컬 프록시는 ADD_MANY_FILE 설명으로 바꿔 내보낸다
ADD_MANY_REMOTE = """이 연결에서는 file 을 쓸 수 없다(사용자 PC 의 파일을 읽지 못한다). 항목은 모두 items 로 보낸다."""
