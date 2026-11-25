"""MCP 서버 안내문. 원격 서버(mcp_server/server.py remote=True)는 REMOTE_INSTRUCTIONS 를, PC 의 로컬 서버(remote=False)와
로컬 프록시(mcp_server/proxy.py)는 PROXY_INSTRUCTIONS 를 쓴다. INSTRUCTIONS 는 원격 쪽이다(web/claude_chat.py 가 원격 서버를 쓴다).

프록시가 서버 모듈(형태소 분석기·저장소)을 불러오지 않도록 이 모듈은 다른 모듈을 import 하지 않는다.

안내문은 MAX_CHARS 이하로 둔다. Claude Code 는 MCP 서버 안내문을 4,096자에서 자르고 Claude 는 잘린 앞부분만 본다
(https://code.claude.com/docs/en/mcp "Tool search for MCP server authors". v2.1.296 전에는 2,048자였다. 2026-11-24 이 PC 의
세션에서 4413자 안내문이 4096자에서 잘린 것을 확인). 잘리면 끝 문장부터 전달되지 않으므로 사용자 확인이 필요한
동작(분류 삭제), 먼저 찾기, 등록 기본값을 앞쪽 2,048자 안에 둔다.
"""
MAX_CHARS = 4000

_INTRO = """fast-question is the user's question-and-answer knowledge DB. No person reviews what is added, so follow the format below. The source is rules.md of the qa Skill; this is its summary.
"""

_CONFIRM = """- kb_delete_category also deletes all its subcategories and Q&As. Call it without confirm first, show the returned warning to the user, and call with confirm=true only after approval.
"""

_FORMAT = """- A question is one line of at most 300 in weighted length: a question, a noun phrase, or the error message or symptom as seen. Word it the way it will be searched for when the fact is needed, not as a title of the answer. Include the target name; if the target is a common noun, add the system name.
  An answer is at most 800 in weighted length and 6 lines. The first line answers the question directly in 1-2 sentences; from the second line on, add "- " items only when needed, one fact per item. A line is at most 240.
  Weighted length: Hangul, CJK and fullwidth characters count 2, others 1.
  Write only facts (current behavior). End a reason answer with a "- Premise: ..." item (in the answer's language) if you confirmed the premise. No metaphors, decision or development history, guesses, progress status, secrets, personal information or IP addresses. kb_add and kb_update reject violations; fix them and call again.
"""

_EVIDENCE = """- Evidence: a fact confirmed from a local file takes {"kind": "auto", "path": "<absolute file path>", "anchor": "<symbol or string to find>"}; a fact confirmed with a person takes {"kind": "note", "text": "who confirmed it and how"}; general knowledge needs none.
- Before adding, check with kb_search whether the question exists. If it does, read its version with kb_get and change it with kb_update. On status=similar_found nothing was added: call again with force=true only if the question asks about a different fact.
- On 0 hits, retry kb_search with different wording up to two more times.
"""

_TAIL = """- When kb_search or kb_add returns cleanup_hint, finish the current task, then tell the user "N cleanup items" in one line and ask before cleaning up.
- When you tell the user about a Q&A you added, changed or found, show the result's url.
"""

PROXY_INSTRUCTIONS = _INTRO + _CONFIRM + """- This DB holds facts about the user's projects, systems and procedures that were confirmed earlier, including facts the code alone does not reveal (confirmed with a person, why a setting is the way it is, what was decided against). Search it first with kb_search at two moments, even if the user did not ask.
  When a task starts: search with the target names in the instruction (system, module, file, setting key).
  During a task: when you need a fact about the user's environment (a setting, a procedure, the cause of an error, why something behaves as it does), search before reading files or guessing.
  A search term is a target name or an error message, not a sentence; put several in queries. If an item matches, use its answer and add its url to your response. If its evidence_state is stale, read the evidence file before using it. If its premise item no longer holds, tell the user rather than follow it.
- Not adding is the default. Unasked, add a fact only if all hold: you confirmed it in this session from the project's files, command results or the user's words; someone working here later would pay a cost for not knowing it (e.g. it took several files to trace, it contradicted names or documents, it is the cause of an error, a person confirmed it, it was decided against or tried and failed); it is not the content of a file you created or edited in this session; it stays true after the task ends. Not added: where a document is or what it contains, a value written in one file.
- Add a Q&A unrelated to the current project only on /qa.
""" + _FORMAT + """- Choose a category from kb_categories. If none fits, create one with kb_add_category without asking the user (top = work topic, second = target name).
""" + _EVIDENCE + """- Clean up a kb_search hit marked cleanup on the spot: read it with kb_cleanup_list, fix or archive it, then close it with kb_cleanup_resolve.
""" + _TAIL

REMOTE_INSTRUCTIONS = _INTRO + _CONFIRM + """- This DB holds facts about the user's projects, systems and internal procedures that were confirmed earlier, including facts that reading the code alone does not reveal (confirmed with a person, why a setting is the way it is).
  Use kb_search when you need a fact about the user's projects or environment (a setting value and its location, a procedure, the cause of an error, why something behaves as it does).
  A search term is a target name or an error message, not a sentence; put several expressions together in queries. If an item matches, use its answer and add the item url to your response. For an item whose evidence_state is stale, confirm it from its evidence before using it. If its premise item no longer holds, tell the user rather than follow it.
- Call kb_add, kb_add_many and kb_update only when the user asks to add or change a Q&A. Facts about the current project are no exception.
""" + _FORMAT + """- kb_categories lists the categories. Use kb_add_category to create one (top level = work topic, second level = target name).
""" + _EVIDENCE + """- A kb_search hit marked cleanup is a cleanup target. kb_cleanup_list shows its details, and kb_cleanup_resolve closes it after the item is fixed or archived.
""" + _TAIL

INSTRUCTIONS = REMOTE_INSTRUCTIONS

# kb_add_many 도구 설명. 서버(mcp_server/server.py)와 PC 프록시(mcp_server/proxy.py)가 함께 쓴다(프록시는 서버 모듈을 import 하지 않는다)
ADD_MANY_DOC = """Adds up to 100 Q&As at once. Each item takes the same arguments as kb_add:
{"category", "question", "answer", "evidence"?, "links"?, "force"?}. Items are processed one by one, so the others are
added even if one fails. results[i].status is created / similar_found (a nearly identical question exists; not added,
with candidates) / error. A created result also carries similar candidates when somewhat similar questions exist.
Other keys in an item are ignored.
"""
ADD_MANY_FILE = """file takes the absolute path of a JSON file holding the item array and reads that file. To add a draft file as it is, use file
(copying the items again can change characters)."""
# 원격 직접 연결(claude.ai·한 줄 연결)은 사용자 PC 의 파일을 읽을 수 없다. 로컬 프록시는 ADD_MANY_FILE 설명으로 바꿔 내보낸다
ADD_MANY_REMOTE = """file is not available on this connection (it cannot read files on the user's PC). Send all items in items."""
RECHECK_DOC = """Judges evidence again. With id, only that Q&A; without it, all Q&As."""
RECHECK_REMOTE = """
On this connection only github evidence is judged (the server cannot read files on the user's PC, and url and note
evidence are never judged). results and items are empty when there is no github evidence to judge."""
