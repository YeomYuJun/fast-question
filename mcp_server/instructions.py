"""MCP 서버 안내문. 원격 서버(mcp_server/server.py remote=True)는 REMOTE_INSTRUCTIONS 를, PC 의 로컬 서버(remote=False)와
로컬 프록시(mcp_server/proxy.py)는 PROXY_INSTRUCTIONS 를 쓴다. INSTRUCTIONS 는 원격 쪽이다(web/claude_chat.py 가 원격 서버를 쓴다).

프록시가 서버 모듈(형태소 분석기·저장소)을 불러오지 않도록 이 모듈은 다른 모듈을 import 하지 않는다.
"""
_FORMAT = """\
- A question is a single-line interrogative sentence of at most 300 in weighted length. Include the target name so the question identifies its subject on its own; if the target is a common noun, add the system name.
  An answer is at most 800 in weighted length and 6 lines. The first line answers the question directly in 1-2 sentences; from the second line on, add "- " bullet items only when needed, one fact per item. Each line is at most 240 in weighted length.
  Weighted length counts Hangul, CJK and fullwidth characters as 2 and every other character as 1.
  Write only facts (current behavior) and no metaphors. Do not include decision history or development history, unverified guesses, progress status, secrets, personal information, or IP addresses.
  kb_add and kb_update check the answer format, banned expressions and sensitive information, and reject violations.
"""

_EVIDENCE = """\
- For a fact confirmed from a local file, attach evidence {"kind": "auto", "path": "<absolute file path>", "anchor": "<symbol or string to find>"}.
  General knowledge (k8s concepts, math, etc.) needs no evidence. For a fact confirmed with a person, attach {"kind": "note", "text": "who confirmed it and how"}.
- Before adding, use kb_search to check whether the same question exists. If it does, read its version with kb_get and change it with kb_update.
  If a nearly identical question exists, kb_add adds nothing and returns status=similar_found with the candidates; if the question asks about a different fact, call again with force=true. If only somewhat similar questions exist, kb_add adds the item and returns the candidates in similar; if a candidate asks about the same fact, merge the two with kb_update and kb_archive.
- If kb_search returns 0 hits, retry with different wording up to two more times (some expressions are not found even through morphology and synonyms).
"""

_TAIL = """\
- When kb_search or kb_add returns cleanup_hint (cleanup items have piled up and no cleanup has run for a while), finish the current task, then tell the user "N cleanup items" in one line and ask whether to clean them up. If the user agrees, handle them with kb_cleanup_list. Do not start a cleanup without asking.
- Deleting a category (kb_delete_category) also deletes all its subcategories and Q&As. First call it without confirm, show the returned warning to the user, and call it with confirm=true only after the user approves.
- When you tell the user about a Q&A you added, changed or looked up, show the result's url (the address that opens the item in the web screen).
"""

PROXY_INSTRUCTIONS = """\
fast-question is the user's question-and-answer knowledge DB. No person reviews what is added, so follow the format below. The source of the rules is rules.md of the qa Skill. Without the Skill, follow this summary.
- This DB holds facts about the user's projects, systems and internal procedures that the user confirmed earlier. It includes facts that reading the code alone does not reveal (things confirmed with a person, why a setting is the way it is), and it keeps you from investigating again what was already investigated. So search it first with kb_search at the two moments below, even if the user did not ask.
  When a task starts: search with the target names in the instruction (system, module, file, setting key).
  During a task: when you need a fact about the user's environment, such as a setting value and its location, a procedure, the cause of an error, or why something behaves as it does, search before finding and reading files or guessing, even if you think you know the answer.
  A search term is a target name or an error message, not a sentence. If there are several expressions, put them together in queries. If an item matches, use its answer and add the item url to your response. For an item whose evidence_state is stale, read the evidence file to confirm before using it. If nothing matches, confirm as usual and proceed. You need not search again for a target you already searched for in the same task.
""" + _FORMAT + """\
- Add Q&As unrelated to the current project only when the user asks with /qa.
- Choose a category from kb_categories. If none fits, create one with kb_add_category without asking the user (top level = work topic, second level = target name).
""" + _EVIDENCE + """\
- For a kb_search hit marked cleanup (cleanup target), clean it up on the spot without asking the user. Read it with kb_cleanup_list, fix or archive it, then close it with kb_cleanup_resolve.
""" + _TAIL

REMOTE_INSTRUCTIONS = """\
fast-question is the user's question-and-answer knowledge DB. No person reviews what is added, so follow the format below. The source of the rules is rules.md of the qa Skill. Without the Skill, follow this summary.
- This DB holds facts about the user's projects, systems and internal procedures that the user confirmed earlier. It includes facts that reading the code alone does not reveal (things confirmed with a person, why a setting is the way it is).
  Use kb_search when you need a fact about the user's projects or environment, such as a setting value and its location, a procedure, the cause of an error, or why something behaves as it does.
  A search term is a target name or an error message, not a sentence. If there are several expressions, put them together in queries. If an item matches, use its answer and add the item url to your response. For an item whose evidence_state is stale, confirm it from its evidence before using it.
""" + _FORMAT + """\
- Call kb_add, kb_add_many and kb_update only when the user asks to add or change a Q&A. Facts about the current project are no exception.
- kb_categories lists the categories. Use kb_add_category to create one (top level = work topic, second level = target name).
""" + _EVIDENCE + """\
- A kb_search hit marked cleanup is a cleanup target. kb_cleanup_list shows its details, and kb_cleanup_resolve closes it after the item is fixed or archived.
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
