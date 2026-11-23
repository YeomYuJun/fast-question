"use strict";
// fast-question 화면. 검색 중심 단일 화면이다.
// DOM 은 el() 로만 만든다(사용자 문자열을 HTML 로 해석하지 않는다).
// 결과 행은 id 로 재사용한다. 새 검색 결과에 없는 행은 .out 으로 높이를 접은 뒤 지운다.

const $ = (s, root = document) => root.querySelector(s);

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "value") e.value = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) e.append(k instanceof Node ? k : String(k));
  return e;
}

// 14px 선 아이콘. el() 은 HTML 이름공간이라 SVG 는 따로 만든다
const ICONS = {
  pen: ["M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16v4z", "M13.5 6.5l4 4"],
  trash: ["M4 7h16", "M9 7V4h6v3", "M6 7l1 13h10l1-13", "M10 11v5M14 11v5"],
  check: ["M5 12.5l4.5 4.5L19 7.5"],
  cross: ["M6 6l12 12M18 6L6 18"],
  copy: ["M9 9h10v10H9z", "M5 15V5h10"],
  download: ["M12 4v11", "M7 10l5 5 5-5", "M5 20h14"],
  link: ["M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1", "M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"],
  share: ["M12 3v12", "M8 7l4-4 4 4", "M5 13v6h14v-6"],
  help: ["M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18z", "M9.5 9.5a2.5 2.5 0 1 1 3.4 2.3c-.6.3-.9.8-.9 1.4v.6", "M12 17h.01"],
};
function icon(name) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  const attrs = { width: 14, height: 14, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": name === "check" ? 2.4 : 2, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" };
  for (const [k, v] of Object.entries(attrs)) svg.setAttribute(k, v);
  for (const d of ICONS[name]) {
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return svg;
}

// 아이콘만 있는 버튼. 보이는 글자가 없어서 aria-label 이 필수다
function ibtn(kind, name, label, title, onclick, type = "button") {
  return el("button", { class: `ibtn ${kind}`, type, "aria-label": label, title, onclick }, icon(name));
}

// replaceChildren 은 null 과 배열을 글자로 넣는다. el() 과 같은 규칙으로 걸러서 넣는다
function fill(node, ...kids) {
  node.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false));
}

// ---------- 화면 언어 ----------
// 언어는 theme.js 가 정한다(window.fqLang). 바꾸면 다시 읽으므로 화면이 떠 있는 동안 언어는 바뀌지 않는다.
// 값이 함수인 항목은 인자를 받아 문장을 만든다(언어마다 어순이 달라 조각을 이어 붙이지 않는다)
const LANG = window.fqLang === "en" ? "en" : "ko";
document.documentElement.lang = LANG;
const PROVIDERS = { github: "GitHub", google: "Google" };
const STR = {
  ko: {
    doc_title: "fast question - Claude 와 같이 쓰는 Q&A 저장소",
    side: "분류", keys: "/ 검색 · ↑↓ 이동 · Enter 펼치기 · E 편집 · Esc 지우기", new_q: "+ 새 질문",
    connect: "연결", logout: "로그아웃", claude_panel: "Claude 패널",
    lp_label: "사용 예시", lp_h: "Claude 와 같이 쓰는 Q&A 저장소", lp_sub: "나와 Claude 가 함께 쓰는 질문·답변 저장소.",
    ex_label: "예시: Claude Code 가 저장소에서 지난 해결 방법을 찾아 답하고, 새로 알게 된 내용을 기록합니다",
    ex_badge: "예시", ex_u1: "지난번에 CORS 오류 났을 때 어떻게 해결했지?", ex_arg1: "\"CORS 오류\"", ex_res1: "1건",
    ex_q1: "API 를 부를 때 CORS 오류가 나는 이유는?",
    ex_a1: "서버 응답에 Access-Control-Allow-Origin 이 없어서다. 개발 중에는 프록시로 같은 출처를 만든다.",
    ex_meta1: "웹 / API · #12",
    ex_c1: "기록해 둔 답이 있습니다. 서버가 허용 출처 헤더를 보내지 않았고, 개발 중에는 프록시로 같은 출처를 만들어 해결했습니다.",
    ex_u2: "방금 정리한 토큰 갱신 방법도 남겨 줘", ex_arg2: "웹 / 인증", ex_res2: "#37 등록", ex_by: "방금 · Claude 가 등록",
    ex_q2: "access 토큰이 만료되면 어떻게 다시 받나?",
    ex_a2: "401 을 받으면 refresh 토큰으로 새 access 토큰을 받고 원래 요청을 다시 보낸다.", ex_meta2: "웹 / 인증 · #37",
    rp_h: "fast question 시작하기", rp_sub: "GitHub 이나 Google 계정으로 바로 시작합니다. 따로 가입하지 않아도 됩니다.",
    agree1: "계속하면 ", terms: "이용약관", agree2: "에 동의하고 ", privacy: "개인정보 처리방침", agree3: "을 확인한 것으로 봅니다.",
    cat_new: "새 분류 이름", cat_new_ph: "이름 (중분류는 대분류 > 이름)", add: "추가", cancel: "취소", cat_add: "+ 분류 추가",
    q_ph: "무엇이 궁금한가요?  예) CORS 오류", q_label: "질문·답변 검색", help_show: "검색 문법 보기", help: "검색 문법",
    help_and: "모두 든 항목 (공백은 AND)", help_or: "둘 중 하나가 든 항목 (OR)", help_not: "a 가 든 항목 제외",
    help_phrase: "공백을 포함한 구절", help_field: "질문에서만, 답변에서만", help_path: "근거 파일 경로에 x 가 든 항목",
    since_title: "최근 24시간 안에 등록한 항목만 보기",
    cleanup_title: "주기 작업이 찾아 둔 정리 대상(비슷한 질문, 형식 위반). Claude 가 정리합니다",
    public: "공개 지식",

    net_fail: "서버에 연결하지 못했습니다.", http_fail: (s) => `요청을 처리하지 못했습니다(HTTP ${s}).`,
    all: "전체",
    export_md_of: (p) => `${p} Markdown 내보내기`, export_md: "Markdown 내보내기",
    share_of: (p) => `${p} 공개 링크 만들기`, share_short: "공개 링크",
    rename_of: (p) => `${p} 이름 변경`, rename: "이름 변경", delete_of: (p) => `${p} 삭제`, del: "삭제",
    new_name_of: (p) => `${p} 새 이름`, desc_of: (p) => `${p} 설명`, desc_ph: "설명 (선택)",
    renamed: (p) => `분류 이름을 "${p}" 로 바꿨습니다.`, redescribed: (p) => `분류 "${p}" 의 설명을 바꿨습니다.`,
    save: "저장", save_enter: "저장 (Enter)", cancel_esc: "취소 (Esc)",
    n_children: (n) => `하위 분류 ${n}개`, n_qa: (n) => `Q&A ${n}건`,
    cat_delete_confirm: (p, parts) => `분류 "${p}" 를 삭제합니다.` + (parts.length ? `\n${parts.join(", ")}도 모두 함께 삭제됩니다.` : "")
      + "\n삭제하시겠습니까?",
    cat_deleted: (p, n) => `분류 "${p}" 를 삭제했습니다.` + (n ? ` Q&A ${n}건도 함께 삭제했습니다.` : ""),
    cat_edit: "분류 편집", cat_edit_done: "편집 종료", cat_clear: (p) => `분류 필터 해제: ${p}`,
    since_chip: (h, n) => `최근 ${h}시간 ${n}건`, cleanup_chip: (n) => `정리 대상 ${n}건`,
    cleanup_ask: "정리 대상을 kb_cleanup_list 로 보고 규칙대로 정리해 줘.",
    cleanup_local: "Claude Code 에서 \"qa 정리해\" 라고 하면 정리합니다.",
    cat_depth: "분류는 대분류 > 중분류 2단계까지 만들 수 있습니다.", cat_made: (p) => `분류 "${p}" 를 만들었습니다.`,
    similar: "비슷한 결과", no_match: "일치하는 질문이 없습니다.",
    no_match_similar: "일치하는 질문이 없습니다. 비슷한 결과를 아래에 보입니다.",
    none_since: (h) => `최근 ${h}시간 안에 등록한 질문이 없습니다.`, none_yet: "아직 등록된 질문이 없습니다.",
    add_as_new: (q) => `“${q}” 질문으로 새로 등록`,
    relaxed_title: (q) => `"${q}" 로 검색`, relaxed: (word, n) => [word, ` 빼고 ${n}건`],
    sum_similar: (b, n) => [b, `건 · 비슷한 결과 ${n}건`], sum_query: (b, total, g) => [b, `건 · ${g}개 분류`],
    sum_recent: (b, n, g) => ["최근 수정 ", b, `건 · ${g}개 분류`],
    morph_off: "형태소 검색 꺼짐", top_only: (n) => `위 ${n}건만 표시`, expand: "펴기", collapse: "접기",
    open_vscode: (p) => `VS Code 로 열기: ${p}`, none: "없음",
    kind: { note: "메모" }, op: { insert: "생성", update: "수정", archive: "삭제", restore: "복원" },
    field: { question: "질문", answer: "답변", category_id: "분류", evidence_state: "근거 상태" },
    op_fields: (op, fields) => `${op}(${fields.join("·")})`,
    hist_latest: "최근 한 건만 보기", hist_all: (n) => `이력 ${n}건 모두 보기`,
    loading: "읽는 중...", evidence: "근거", links: "연결", related: "관련", history: "이력", category: "분류",
    copy: "복사", copied_qa: "Q&A 를 복사했습니다.", copy_addr: "주소 복사", copied_addr: "주소를 복사했습니다.",
    make_share: "공개 링크 만들기", make_share_title: "공개 링크(로그인 없이 보는 주소)", edit: "편집", edit_key: "편집 (E)",
    copy_fail: "복사하지 못했습니다. 브라우저가 클립보드 접근을 막았습니다.",
    count_title: (n, max, ln, lines) => `가중 길이 ${n}/${max} (한글·한자는 2, 그 밖은 1로 셉니다) · ${ln}/${lines}줄`,
    count_title_q: (n, max) => `가중 길이 ${n}/${max} (한글·한자는 2, 그 밖은 1로 셉니다)`,
    deleted_cat: (id) => `(삭제된 분류 ${id})`,
    question: "질문", answer: "답변", evidence_opt: "근거 (선택)",
    evidence_ph: "근거 (선택): 경로 #앵커 · URL · 메모: 누가 확인",
    added: "등록했습니다.", saved: "저장했습니다.", conflict_reopened: "다른 곳에서 먼저 고쳤습니다. 지금 값으로 다시 열었습니다.",
    similar_head: "비슷한 질문이 이미 있습니다. 같은 내용이면 그 항목을 고쳐 주세요.", add_anyway: "다른 질문으로 등록",
    delete_confirm: "삭제하시겠습니까?", deleted: "삭제했습니다.", undo: "되돌리기", restored: "되돌렸습니다.",
    add_btn: "등록", add_key: "등록 (Ctrl+Enter)", save_key: "저장 (Ctrl+Enter)", delete_q: "이 질문 삭제",
    pub_note: "운영자가 적재한 공개 항목입니다. 고치려면 내 저장소로 복사합니다.", copy_qa: "질문과 답 복사",
    to_mine: "내 저장소로", pub_similar: (q) => `내 저장소에 비슷한 질문이 있습니다.\n\n${q}\n\n그래도 복사하시겠습니까?`,
    pub_copied: (p) => `내 저장소에 등록했습니다: ${p}`,
    no_cats: "분류가 아직 없습니다. 왼쪽 패널의 \"+ 분류 추가\" 로 먼저 만들어 주세요.",
    not_found_id: (id) => `#${id} 를 찾지 못했습니다(삭제했거나 없는 번호입니다).`,
    to_light: "라이트 테마로 바꾸기", to_dark: "다크 테마로 바꾸기",
    login_with: (p) => `${p} 로 계속하기`, no_providers: "켜진 로그인 공급자가 없습니다.",
    login_err: {
      denied: "로그인을 취소했습니다.",
      state: "로그인 시간이 지났거나 다른 창에서 시작했습니다. 다시 로그인해 주세요.",
      provider: "로그인 공급자의 응답을 확인하지 못했습니다. 다시 시도해 주세요.",
      closed: "이 계정은 로그인할 수 없습니다.",
      limit: "이 주소에서 한 시간 안에 만든 계정이 너무 많습니다. 한 시간 뒤에 다시 시도해 주세요.",
    },
    link_err: {
      session: "로그인한 상태에서 다시 시도해 주세요.",
      state: "시간이 지났거나 그 사이 로그아웃했습니다. 다시 시도해 주세요.",
      provider: "로그인 공급자의 응답을 확인하지 못했습니다. 다시 시도해 주세요.",
      closed: "이 계정은 추가할 수 없습니다.",
      exists: "이 공급자의 다른 계정이 이미 연결되어 있습니다.",
      denied: "로그인 수단 추가를 취소했습니다.",
    },
    link_fail: "로그인 수단을 추가하지 못했습니다.",
    linked: (p, dropped) => `${p} 로그인을 추가했습니다.` + (dropped ? " 그 계정이 쓰던 저장소는 버렸습니다." : ""),
    account_of: (p) => `${p} 계정`, signed_in_with: (who) => `${who}으로 로그인`,
    copy_of: (label) => `${label} 복사`,
    token_revoke_confirm: (n) => `토큰 "${n}" 을 폐기합니다. 이 토큰을 쓰는 PC 는 더 연결되지 않습니다.\n폐기하시겠습니까?`,
    token_revoked: "토큰을 폐기했습니다.", unnamed_app: "이름 없는 앱",
    app_disconnect_confirm: (at, n) => `${at} 에 연결한 "${n}" 의 연결을 끊습니다. 그 앱은 다시 연결해야 이 저장소를 쓸 수 있습니다.\n끊으시겠습니까?`,
    app_disconnected: "연결을 끊었습니다.",
    share_copied: "공개 링크를 복사했습니다. 이 주소를 아는 사람은 로그인 없이 볼 수 있습니다.",
    share_revoke_confirm: (title) => `"${title}" 의 공개 링크를 폐기합니다. 그 주소는 바로 열리지 않게 됩니다.\n폐기하시겠습니까?`,
    share_revoked: "공개 링크를 폐기했습니다.", shares: "공개 링크", share_kind_qa: "질문 하나", created_on: (d) => ` · 만든 날 ${d}`,
    share_revoke_of: (title) => `${title} 공개 링크 폐기`, revoke: "폐기",
    shares_none: "펼친 항목의 공유 아이콘이나 분류 편집의 공유 아이콘으로 만듭니다. 주소를 아는 사람은 로그인 없이 그 내용을 읽습니다.",
    apps: "연결된 앱", app_meta_title: (a, b) => `연결 ${a} · 마지막 사용 ${b}`, app_connected: (s) => `연결 ${s}`,
    app_last_used: (s) => `마지막 사용 ${s}`, app_disconnect_of: (at, n) => `${at} 에 연결한 ${n} 연결 끊기`, disconnect: "끊기",
    apps_none: "브라우저에서 승인한 연결(한 줄 연결, claude.ai 커넥터)이 없습니다.",
    connect_touch: "Claude Code 연결(한 줄 연결, 플러그인 설치)은 Claude Code 를 쓰는 PC 의 브라우저에서 합니다. 여기서는 토큰 폐기, 연결 끊기, 로그인 수단 추가, 내보내기, 탈퇴를 할 수 있습니다.",
    connect_intro: "Claude Code 를 내 질문·답변 저장소에 연결합니다. 1단계만으로 쓸 수 있고, 2단계는 Claude Code 플러그인까지 이 PC 에 설치합니다.",
    close: "닫기", cc_connect: "Claude Code 연결", step1: "한 줄로 연결",
    step1_note: "설치 없이 터미널에서 아래 명령을 실행합니다. 그다음 Claude Code 에서 /mcp 를 열어 fast-question 을 고르고, 브라우저에서 로그인·승인합니다.",
    cmd_connect: "연결 명령", cmd_connect_done: "연결 명령을 복사했습니다.", step2: "PC 에 설치",
    step2_note: "Claude Code 플러그인을 설치하고 로그인합니다. PC 에 uv 가 있어야 합니다(Windows: winget install --id astral-sh.uv -e). 아래 명령을 차례로 실행하고, 브라우저에서 로그인·승인한 뒤 Claude Code 를 다시 시작합니다.",
    cmd_market: "마켓플레이스 추가 명령", cmd_market_done: "마켓플레이스 추가 명령을 복사했습니다.",
    cmd_plugin: "플러그인 설치 명령", cmd_plugin_done: "플러그인 설치 명령을 복사했습니다.",
    cmd_login: "로그인 명령", cmd_login_done: "로그인 명령을 복사했습니다.",
    step2_after: "1단계로 등록한 fast-question 은 플러그인과 겹치므로 claude mcp remove -s user fast-question 으로 지웁니다.",
    tokens: "만든 토큰", token_created: (d) => `만든 날 ${d}`, expired: "만료", expires: (d) => `만료 ${d}`,
    last_used: (d) => ` · 마지막 사용 ${d}`, never_used: " · 아직 안 씀", token_revoke_of: (n) => `${n} 토큰 폐기`,
    tokens_none: "아직 만든 토큰이 없습니다.", export: "내보내기",
    export_note: "내 저장소 전체를 SQLite 파일 하나로 받습니다. 로컬 모드 설정(config.json)의 db_path 로 지정하면 그대로 열립니다.",
    export_db: "저장소 파일 받기", logins: "로그인 수단", logins_now: (list) => `지금 ${list} 로 로그인할 수 있습니다.`,
    logins_more: " 다른 로그인을 추가하면 어느 쪽으로 들어와도 이 저장소가 열립니다. 추가하는 계정으로 이미 쓰던 저장소가 있으면 버려집니다(30일 뒤 삭제). 필요하면 먼저 그 계정으로 로그인해 저장소 파일을 받아 두세요.",
    login_add_confirm: (p) => `${p} 로그인을 이 계정에 추가합니다.\n그 ${p} 계정으로 쓰던 fast question 저장소가 있으면 버려집니다(30일 뒤 삭제).\n추가하시겠습니까?`,
    login_add: (p) => `${p} 로그인 추가`,
    leave: "탈퇴", leave_word: "탈퇴", leave_word_label: "탈퇴 확인 문구",
    leave_note: "계정을 지웁니다. 이 계정의 로그인·토큰이 모두 끊기고 저장소를 더 열 수 없습니다. 먼저 저장소 파일을 받아 두세요. 계속하려면 아래 칸에 \"탈퇴\" 를 입력하고 버튼을 누르세요.",
  },
  en: {
    doc_title: "fast question - A Q&A store you share with Claude",
    side: "Categories", keys: "/ search · ↑↓ move · Enter expand · E edit · Esc clear", new_q: "+ New question",
    connect: "Connect", logout: "Sign out", claude_panel: "Claude panel",
    lp_label: "Example", lp_h: "A Q&A store you share with Claude", lp_sub: "Questions and answers that you and Claude keep together.",
    ex_label: "Example: Claude Code finds a past fix in the store, answers with it, and records what it just learned",
    ex_badge: "Example", ex_u1: "How did I fix the CORS error last time?", ex_arg1: "\"CORS error\"", ex_res1: "1 hit",
    ex_q1: "Why does calling the API return a CORS error?",
    ex_a1: "The server response has no Access-Control-Allow-Origin. During development, a proxy serves both from the same origin.",
    ex_meta1: "Web / API · #12",
    ex_c1: "There is a recorded answer. The server did not send the allowed-origin header, and during development you fixed it with a proxy on the same origin.",
    ex_u2: "Save the token refresh steps we just worked out, too", ex_arg2: "Web / Auth", ex_res2: "#37 added", ex_by: "Just now · added by Claude",
    ex_q2: "How do I get a new access token when it expires?",
    ex_a2: "On a 401, get a new access token with the refresh token and resend the original request.", ex_meta2: "Web / Auth · #37",
    rp_h: "Get started with fast question", rp_sub: "Start right away with your GitHub or Google account. No separate sign-up.",
    agree1: "By continuing, you agree to the ", terms: "Terms", agree2: " and acknowledge the ", privacy: "Privacy Policy", agree3: ".",
    cat_new: "New category name", cat_new_ph: "Name (for a subcategory: Top > Name)", add: "Add", cancel: "Cancel", cat_add: "+ Add category",
    q_ph: "What do you want to know?  e.g. CORS error", q_label: "Search questions and answers", help_show: "Show search syntax",
    help: "Search syntax",
    help_and: "Items containing all (space means AND)", help_or: "Items containing either (OR)", help_not: "Exclude items containing a",
    help_phrase: "A phrase that includes spaces", help_field: "Question only, answer only", help_path: "Items whose evidence path contains x",
    since_title: "Show only items added in the last 24 hours",
    cleanup_title: "Cleanup items found by the scheduled job (similar questions, format violations). Claude resolves them",
    public: "Public knowledge",

    net_fail: "Could not reach the server.", http_fail: (s) => `The request failed (HTTP ${s}).`,
    all: "All",
    export_md_of: (p) => `Export ${p} as Markdown`, export_md: "Export as Markdown",
    share_of: (p) => `Create a public link for ${p}`, share_short: "Public link",
    rename_of: (p) => `Rename ${p}`, rename: "Rename", delete_of: (p) => `Delete ${p}`, del: "Delete",
    new_name_of: (p) => `New name for ${p}`, desc_of: (p) => `Description of ${p}`, desc_ph: "Description (optional)",
    renamed: (p) => `Renamed the category to "${p}".`, redescribed: (p) => `Updated the description of "${p}".`,
    save: "Save", save_enter: "Save (Enter)", cancel_esc: "Cancel (Esc)",
    n_children: (n) => (n === 1 ? "1 subcategory" : `${n} subcategories`), n_qa: (n) => (n === 1 ? "1 Q&A" : `${n} Q&As`),
    cat_delete_confirm: (p, parts) => `Delete the category "${p}"?` + (parts.length ? `\n${parts.join(" and ")} will also be deleted.` : ""),
    cat_deleted: (p, n) => `Deleted the category "${p}".` + (n ? ` ${n === 1 ? "1 Q&A was" : `${n} Q&As were`} deleted with it.` : ""),
    cat_edit: "Edit categories", cat_edit_done: "Done", cat_clear: (p) => `Clear category filter: ${p}`,
    since_chip: (h, n) => `Last ${h} hours: ${n}`, cleanup_chip: (n) => `To clean up: ${n}`,
    cleanup_ask: "Review the cleanup items with kb_cleanup_list and resolve them according to the rules.",
    cleanup_local: "In Claude Code, ask it to clean up your Q&A and it will resolve them.",
    cat_depth: "Categories can be two levels deep: Top > Sub.", cat_made: (p) => `Created the category "${p}".`,
    similar: "Similar results", no_match: "No matching questions.",
    no_match_similar: "No matching questions. Similar results are shown below.",
    none_since: (h) => `No questions were added in the last ${h} hours.`, none_yet: "No questions yet.",
    add_as_new: (q) => `Add “${q}” as a new question`,
    relaxed_title: (q) => `Search for "${q}"`, relaxed: (word, n) => ["without ", word, `: ${n}`],
    sum_similar: (b, n) => [b, ` results · ${n} similar`],
    sum_query: (b, total, g) => [b, ` ${total === 1 ? "result" : "results"} · ${g} ${g === 1 ? "category" : "categories"}`],
    sum_recent: (b, n, g) => ["Recently updated: ", b, ` · ${g} ${g === 1 ? "category" : "categories"}`],
    morph_off: "Morphological search off", top_only: (n) => `Showing the top ${n}`, expand: "Expand", collapse: "Collapse",
    open_vscode: (p) => `Open in VS Code: ${p}`, none: "None",
    kind: { note: "NOTE" }, op: { insert: "created", update: "edited", archive: "deleted", restore: "restored" },
    field: { question: "question", answer: "answer", category_id: "category", evidence_state: "evidence state" },
    op_fields: (op, fields) => `${op} (${fields.join(", ")})`,
    hist_latest: "Show only the latest", hist_all: (n) => `Show all ${n} entries`,
    loading: "Loading...", evidence: "Evidence", links: "Links", related: "Related", history: "History", category: "Category",
    copy: "Copy", copied_qa: "Copied the Q&A.", copy_addr: "Copy address", copied_addr: "Copied the address.",
    make_share: "Create public link", make_share_title: "Public link (viewable without signing in)", edit: "Edit", edit_key: "Edit (E)",
    copy_fail: "Could not copy. The browser blocked clipboard access.",
    count_title: (n, max, ln, lines) => `Weighted length ${n}/${max} (Hangul and CJK count 2, others 1) · ${ln}/${lines} lines`,
    count_title_q: (n, max) => `Weighted length ${n}/${max} (Hangul and CJK count 2, others 1)`,
    deleted_cat: (id) => `(deleted category ${id})`,
    question: "Question", answer: "Answer", evidence_opt: "Evidence (optional)",
    evidence_ph: "Evidence (optional): path #anchor · URL · note: who verified",
    added: "Added.", saved: "Saved.", conflict_reopened: "It was changed elsewhere first. Reopened with the current values.",
    similar_head: "A similar question already exists. If it is the same, edit that item instead.", add_anyway: "Add as a different question",
    delete_confirm: "Delete this question?", deleted: "Deleted.", undo: "Undo", restored: "Restored.",
    add_btn: "Add", add_key: "Add (Ctrl+Enter)", save_key: "Save (Ctrl+Enter)", delete_q: "Delete this question",
    pub_note: "A public item provided by the operator. Copy it to your repository to edit it.", copy_qa: "Copy question and answer",
    to_mine: "Copy to mine", pub_similar: (q) => `Your repository already has a similar question:\n\n${q}\n\nCopy anyway?`,
    pub_copied: (p) => `Added to your repository: ${p}`,
    no_cats: "There are no categories yet. Create one first with \"+ Add category\" in the left panel.",
    not_found_id: (id) => `Could not find #${id} (it was deleted or does not exist).`,
    to_light: "Switch to light theme", to_dark: "Switch to dark theme",
    login_with: (p) => `Continue with ${p}`, no_providers: "No sign-in provider is enabled.",
    login_err: {
      denied: "Sign-in was cancelled.",
      state: "The sign-in timed out or was started in another window. Please sign in again.",
      provider: "Could not verify the response from the sign-in provider. Please try again.",
      closed: "This account cannot sign in.",
      limit: "Too many accounts were created from this address in the last hour. Please try again in an hour.",
    },
    link_err: {
      session: "Sign in and try again.",
      state: "It timed out or you signed out in the meantime. Please try again.",
      provider: "Could not verify the response from the sign-in provider. Please try again.",
      closed: "This account cannot be added.",
      exists: "Another account from this provider is already linked.",
      denied: "Adding the sign-in method was cancelled.",
    },
    link_fail: "Could not add the sign-in method.",
    linked: (p, dropped) => `Added ${p} sign-in.` + (dropped ? " The repository that account used was discarded." : ""),
    account_of: (p) => `${p} account`, signed_in_with: (who) => `Signed in with ${who}`,
    copy_of: (label) => `Copy ${label}`,
    token_revoke_confirm: (n) => `Revoke the token "${n}"? PCs that use this token will no longer connect.`,
    token_revoked: "Revoked the token.", unnamed_app: "Unnamed app",
    app_disconnect_confirm: (at, n) => `Disconnect "${n}" (connected ${at})? The app has to connect again to use this repository.`,
    app_disconnected: "Disconnected.",
    share_copied: "Copied the public link. Anyone with this address can view it without signing in.",
    share_revoke_confirm: (title) => `Revoke the public link for "${title}"? The address stops working immediately.`,
    share_revoked: "Revoked the public link.", shares: "Public links", share_kind_qa: "One question", created_on: (d) => ` · created ${d}`,
    share_revoke_of: (title) => `Revoke the public link for ${title}`, revoke: "Revoke",
    shares_none: "Create one with the share icon on an expanded item or in category editing. Anyone with the address can read it without signing in.",
    apps: "Connected apps", app_meta_title: (a, b) => `Connected ${a} · last used ${b}`, app_connected: (s) => `Connected ${s}`,
    app_last_used: (s) => `Last used ${s}`, app_disconnect_of: (at, n) => `Disconnect ${n} (connected ${at})`, disconnect: "Disconnect",
    apps_none: "No connections approved in the browser (one-line connect, claude.ai connector).",
    connect_touch: "Connecting Claude Code (one-line connect, plugin install) is done in the browser on the PC that runs Claude Code. Here you can revoke tokens, disconnect apps, add sign-in methods, export, and delete your account.",
    connect_intro: "Connect Claude Code to your Q&A repository. Step 1 alone is enough; step 2 also installs the Claude Code plugin on this PC.",
    close: "Close", cc_connect: "Connect Claude Code", step1: "Connect with one line",
    step1_note: "Run the command below in a terminal, no install needed. Then open /mcp in Claude Code, pick fast-question, and sign in and approve in the browser.",
    cmd_connect: "connect command", cmd_connect_done: "Copied the connect command.", step2: "Install on this PC",
    step2_note: "Install the Claude Code plugin and sign in. The PC needs uv (Windows: winget install --id astral-sh.uv -e). Run the commands below in order, sign in and approve in the browser, then restart Claude Code.",
    cmd_market: "marketplace command", cmd_market_done: "Copied the marketplace command.",
    cmd_plugin: "plugin install command", cmd_plugin_done: "Copied the plugin install command.",
    cmd_login: "login command", cmd_login_done: "Copied the login command.",
    step2_after: "The fast-question server added in step 1 overlaps with the plugin, so remove it with claude mcp remove -s user fast-question.",
    tokens: "Tokens", token_created: (d) => `Created ${d}`, expired: "Expired", expires: (d) => `Expires ${d}`,
    last_used: (d) => ` · last used ${d}`, never_used: " · not used yet", token_revoke_of: (n) => `Revoke token ${n}`,
    tokens_none: "No tokens yet.", export: "Export",
    export_note: "Download your whole repository as one SQLite file. Set it as db_path in a local-mode config.json to open it as is.",
    export_db: "Download repository file", logins: "Sign-in methods", logins_now: (list) => `You can sign in with ${list}.`,
    logins_more: " After you add another sign-in, either one opens this repository. If the added account already has a repository, it is discarded (deleted after 30 days). If you need it, sign in with that account first and download its repository file.",
    login_add_confirm: (p) => `Add ${p} sign-in to this account?\nIf that ${p} account already has a fast question repository, it is discarded (deleted after 30 days).`,
    login_add: (p) => `Add ${p} sign-in`,
    leave: "Delete account", leave_word: "delete", leave_word_label: "Confirmation phrase",
    leave_note: "Deletes your account. All sign-ins and tokens for this account stop working and the repository can no longer be opened. Download the repository file first. To continue, type \"delete\" below and press the button.",
  },
};

function t(key, ...args) {
  const v = STR[LANG][key] ?? STR.ko[key];
  return typeof v === "function" ? v(...args) : v ?? key;
}

// 서버 오류의 code 를 한국어 문구로 바꾸는 표. 문장이 정해진 code 만 둔다.
// error(ValidationError 의 기본값)와 사유·숫자가 문장에 든 code(invalid, not_found, quota, db, rate_limited, too_large,
// rejected, invalid_db)는 두지 않는다. 표에 없으면 서버의 error 문장을 그대로 보인다
const CODE_KO = {
  qa_question_len: "질문은 비우지 말고 가중 길이 300 까지 써 주세요(한글·한자는 2, 그 밖은 1로 셉니다).",
  qa_question_line: "질문은 한 줄로 써 주세요.",
  qa_answer_len: "답변은 비우지 말고 가중 길이 800 까지 써 주세요(한글·한자는 2, 그 밖은 1로 셉니다).",
  qa_answer_lines: "답변은 6줄까지 쓸 수 있습니다.",
  qa_evidence_state: "근거 상태 값이 올바르지 않습니다.",
  category_name_len: "분류 이름은 1~60자 한 줄로 써 주세요.",
  category_description_len: "분류 설명은 300자까지 쓸 수 있습니다.",
  category_depth: "분류는 대분류와 중분류 2단계까지만 만들 수 있습니다.",
  category_name_uq: "같은 상위 분류 아래에 같은 이름의 분류가 이미 있습니다.",
  qa_link_order: "관련 질문 연결 값이 올바르지 않습니다.",
  qa_link_uq: "이미 연결된 질문입니다.",
  synonym_term_len: "동의어는 1~60자로 써 주세요.",
  synonym_term_uq: "이미 다른 동의어 그룹에 있는 단어입니다.",
  user_word_len: "사용자 단어는 1~40자, 공백 없이 써 주세요.",
  user_word_tag: "사용자 단어 품사는 NNP, NNG, SL, XR, VV, VA 중 하나여야 합니다.",
  user_word_uq: "이미 등록된 사용자 단어입니다.",
  stopword_len: "불용어는 1~20자, 공백 없이 써 주세요.",
  stopword_uq: "이미 등록된 불용어입니다.",
  evidence_kind: "근거 형식은 file, dir, git, githead, url, note, github 중 하나여야 합니다.",
  evidence_url_missing: "url 근거에 주소가 없습니다.",
  evidence_github_url: "github 근거는 https://github.com/<owner>/<repo>/blob/<ref>/<경로> 형식의 주소여야 합니다.",
  evidence_note_empty: "메모 근거에 내용이 없습니다. 누가 어떻게 확인했는지 적어 주세요.",
  captured_server_kind: "url, note, github 근거는 captured 없이 보내 주세요.",
  remote_path_evidence: "원격 서버는 사용자 PC 의 파일을 읽지 못해 파일·폴더·git 근거를 직접 만들 수 없습니다. 로컬 프록시(run_mcp.py --remote)로 등록하거나 url·메모 근거를 써 주세요.",
  remote_user_word: "원격 서버에서는 형태소 분석 사용자 단어를 바꿀 수 없습니다. 동의어와 불용어는 바꿀 수 있습니다.",
  no_change: "바꿀 내용이 없습니다.",
  restore_category_needed: "이 항목의 분류가 삭제되었습니다. 되살릴 때 옮길 분류를 골라 주세요.",
  qa_link_self: "질문을 자기 자신과 연결할 수 없습니다.",
  synonym_group_size: "새 동의어 그룹에는 단어가 2개 이상 있어야 합니다.",
  synonym_none: "더할 동의어가 없습니다.",
  chat_refused: "Claude 가 이 요청을 거절했습니다.",
  chat_truncated: "응답이 너무 길어 잘렸습니다. 요청을 나눠 주세요.",
  chat_unreadable: "Claude 응답을 읽지 못했습니다. 다시 보내 주세요.",
  chat_failed: "대화를 처리하지 못했습니다.",
  api_key_rejected: "API 키가 거부되었습니다. 키를 다시 등록해 주세요.",
  api_model_denied: "이 API 키로는 고른 모델을 쓸 수 없습니다.",
  api_rate_limited: "Anthropic 요청 한도에 걸렸습니다. 잠시 뒤 다시 보내 주세요.",
  api_unreachable: "Anthropic 에 연결하지 못했습니다.",
  managed_column: "생성 정보, 수정 시각, 버전은 저장소가 정합니다.",
  no_physical_delete: "삭제 대신 보관 처리를 써 주세요.",
  revision_append_only: "이력은 고치거나 지울 수 없습니다.",
  foreign_key: "가리키는 대상이 없습니다.",
  conflict: "다른 곳에서 먼저 고쳤습니다. 현재 값을 다시 읽고 고쳐 주세요.",
  unauthorized: "로그인이 필요합니다.",
  no_key: "먼저 Anthropic API 키를 등록해 주세요.",
  busy: "다른 작업을 처리하는 중입니다. 잠시 뒤 다시 시도해 주세요.",
  share_target_invalid: "공개할 대상 번호가 올바르지 않습니다.",
  share_exists: "이 대상의 공개 링크가 이미 있습니다. 연결 패널의 공개 링크 목록에서 폐기한 뒤 다시 만들어 주세요.",
  share_limit: "공개 링크는 계정마다 200개까지 둘 수 있습니다. 쓰지 않는 링크를 폐기해 주세요.",
};

function errorText(data, status) {
  if (LANG === "ko" && data && CODE_KO[data.code]) return CODE_KO[data.code];
  return (data && data.error) || t("http_fail", status);
}

function applyStatic() {
  document.title = t("doc_title");
  for (const n of document.querySelectorAll("[data-t]")) n.textContent = t(n.dataset.t);
  for (const n of document.querySelectorAll("[data-ta]")) {
    for (const pair of n.dataset.ta.split(";")) {
      const [attr, key] = pair.split(":");
      n.setAttribute(attr, t(key));
    }
  }
}

async function api(method, url, body) {
  const opt = { method, headers: { "X-FQ-Lang": LANG } }; // 원격 모드 서버가 새 계정 DB 에 이 언어의 기본 가이드를 넣는다
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(url, opt);
  } catch (e) {
    throw new Error(t("net_fail")); // 브라우저의 오류(Failed to fetch) 대신
  }
  const data = await r.json().catch(() => ({}));
  // 원격 모드에서 세션이 끝났으면 로그인 화면으로 바꾼다
  if (r.status === 401 && data.code === "unauthorized") showLogin(data.providers || []);
  if (!r.ok) {
    const err = new Error(errorText(data, r.status));
    err.status = r.status;
    err.data = data;
    throw err;
  }
  return data;
}

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (e) {
    return null; // 사이트 데이터가 막힌 브라우저에서도 화면은 동작한다
  }
  return null;
}

// ---------- 알림 ----------

let toastTimer = null;
function toast(msg, opt = {}) {
  const t = $("#toast");
  const action = opt.action
    ? el("button", { type: "button", onclick: () => { t.className = "toast"; opt.action.fn(); } }, opt.action.label)
    : null;
  fill(t, el("span", {}, msg), action);
  t.className = "toast on" + (opt.err ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), opt.err || action ? 6000 : 2500);
}

// ---------- 표시 형식 ----------

const KST = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
function kst(s) {
  const p = Object.fromEntries(KST.formatToParts(new Date(s)).map((x) => [x.type, x.value]));
  return { md: `${p.month}-${p.day}`, hm: `${p.hour}:${p.minute}`, full: `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}` };
}

const KIND = { file: "FILE", dir: "DIR", git: "GIT", githead: "HEAD", url: "URL", github: "GITHUB", ...t("kind") };
const OP = t("op");
const FIELD = t("field");

// 서버의 강조 위치는 코드 포인트 기준이다. UTF-16 으로 자르면 이모지 뒤에서 한 칸씩 밀리므로 코드 포인트로 자른다
function marked(text, spans) {
  const cps = Array.from(text);
  const f = document.createDocumentFragment();
  let pos = 0;
  for (const [s, e] of spans || []) {
    if (s > pos) f.append(cps.slice(pos, s).join(""));
    f.append(el("mark", { class: "hit" }, cps.slice(s, e).join("")));
    pos = e;
  }
  f.append(cps.slice(pos).join(""));
  return f;
}

// ---------- 상태 ----------

const state = {
  q: "",
  cat: null, // 분류 필터 id
  cats: [], // 분류 트리
  flat: [], // 대분류·중분류를 이은 목록
  sel: null, // 펼친 항목 id
  editing: null, // 편집 중인 항목 id
  details: new Map(), // id -> 상세(근거·연결·이력)
  pin: null, // 주소(#qa/id)로 연 항목. 최근 목록에 없으면 끼워 넣는다
  counts: null, // 검색어가 있을 때 분류별 일치 건수
  empty: false, // 마지막 검색이 0건이었다
  shut: new Set(), // 접은 묶음 키
  histOpen: new Set(), // 이력을 전부 펼친 항목 id
  catEdit: false, // 분류 패널이 편집(이름 변경·삭제) 상태다
  catRenaming: null, // 이름을 고치는 중인 분류 id
  since: false, // 최근 24시간 안에 등록한 항목만 보기
};
const rows = new Map(); // id -> 행 요소(row.hit 에 검색 결과 항목)
const groups = new Map(); // 묶음 키 -> 묶음 요소

// ---------- 분류 ----------

async function refreshCats() {
  try {
    const { categories } = await api("GET", "/api/categories");
    state.cats = categories;
    state.flat = categories.flatMap((t) => [t, ...t.children]);
  } catch (e) {
    toast(e.message, { err: true });
  }
  renderTree();
  renderChip();
}

function facet(name, n, on, onclick, cls, title) {
  return el("button", { class: `facet ${cls}${on ? " on" : ""}${n ? "" : " zero"}`, type: "button", "aria-pressed": String(on), title: title || null, onclick },
    el("span", { class: "name" }, name), el("span", { class: "n" }, n));
}

// 검색어가 있으면 분류별 건수는 그 검색의 일치 건수다(state.counts). 없으면 분류 전체 건수다
function renderTree() {
  if (state.catEdit) return renderTreeEdit();
  const hits = state.counts;
  const own = (c) => (hits ? hits.byCat.get(c.id) || 0 : null);
  const countOf = (t) => (hits ? own(t) + t.children.reduce((a, c) => a + own(c), 0) : t.count);
  const total = hits ? hits.total : state.cats.reduce((a, c) => a + c.count, 0);
  fill($("#cat-tree"),
    facet(t("all"), total, state.cat === null, () => setCat(null), "top"),
    state.cats.flatMap((t) => [
      facet(t.name, countOf(t), state.cat === t.id, () => setCat(t.id), "top", t.description),
      ...t.children.map((c) => facet(c.name, hits ? own(c) : c.count, state.cat === c.id, () => setCat(c.id), "child", c.description)),
    ]));
}

// 편집 상태에서는 분류를 눌러도 거르지 않고, 줄마다 이름 변경·삭제를 둔다
function renderTreeEdit() {
  const line = (c, cls) => {
    if (state.catRenaming === c.id) return renameForm(c, cls);
    return el("div", { class: `facet-edit ${cls}` },
      el("span", { class: "name", title: c.description || null }, c.name),
      // 내보내기는 링크다. 파일 이름은 서버의 Content-Disposition 이 정한다
      el("a", { class: "ibtn dl", href: `/api/export?category=${c.id}`, download: "",
        "aria-label": t("export_md_of", c.path), title: t("export_md") }, icon("download")),
      me && me.mode === "remote" ? ibtn("rn", "share", t("share_of", c.path), t("share_short"), () => makeShare("category", c.id)) : null,
      ibtn("rn", "pen", t("rename_of", c.path), t("rename"), () => { state.catRenaming = c.id; renderTree(); }),
      ibtn("del", "trash", t("delete_of", c.path), t("del"), () => deleteCategory(c)));
  };
  fill($("#cat-tree"), state.cats.flatMap((t) => [line(t, "top"), ...t.children.map((c) => line(c, "child"))]));
}

// 이름과 설명을 함께 고친다. 바뀐 값만 보낸다
function renameForm(c, cls) {
  const input = el("input", { class: "fld", value: c.name, "aria-label": t("new_name_of", c.path), autocomplete: "off" });
  const desc = el("input", {
    class: "fld desc", value: c.description || "", "aria-label": t("desc_of", c.path), placeholder: t("desc_ph"),
    maxlength: 300, autocomplete: "off",
  });
  const cancel = () => { state.catRenaming = null; renderTree(); };
  const form = el("form", {
    class: `facet-edit rename ${cls}`,
    onsubmit: async (e) => {
      e.preventDefault();
      const name = input.value.trim();
      const description = desc.value.trim();
      const body = { version: c.version };
      if (name && name !== c.name) body.name = name;
      if (description !== (c.description || "")) body.description = description;
      if (Object.keys(body).length === 1) return cancel();
      try {
        const done = await api("PATCH", `/api/categories/${c.id}`, body);
        state.catRenaming = null;
        await refreshCats();
        runSearch();
        toast(body.name ? t("renamed", done.path) : t("redescribed", done.path));
      } catch (err) {
        toast(err.message, { err: true });
      }
    },
    onkeydown: (e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cancel(); } },
  }, input,
  ibtn("ok", "check", t("save"), t("save_enter"), null, "submit"),
  ibtn("no", "cross", t("cancel"), t("cancel_esc"), cancel), desc);
  setTimeout(() => { input.focus(); input.select(); }, 0);
  return form;
}

async function deleteCategory(c) {
  let impact;
  try {
    impact = await api("GET", `/api/categories/${c.id}/impact`);
  } catch (err) {
    toast(err.message, { err: true });
    return refreshCats();
  }
  const parts = [];
  if (impact.children) parts.push(t("n_children", impact.children));
  if (impact.qa) parts.push(t("n_qa", impact.qa));
  if (!window.confirm(t("cat_delete_confirm", c.path, parts))) return;
  try {
    const res = await api("POST", `/api/categories/${c.id}/archive`, { version: impact.category.version });
    const gone = new Set([c.id, ...(c.children || []).map((x) => x.id)]);
    if (gone.has(state.cat)) state.cat = null;
    await refreshCats();
    runSearch();
    toast(t("cat_deleted", c.path, res.archived_qa));
  } catch (err) {
    toast(err.message, { err: true });
    refreshCats();
  }
}

function setCatEdit(on) {
  state.catEdit = on;
  state.catRenaming = null;
  const b = $("#cat-edit");
  b.textContent = on ? t("cat_edit_done") : t("cat_edit");
  b.classList.toggle("on", on);
  b.setAttribute("aria-pressed", String(on));
  renderTree();
}

async function searchCounts(res) {
  if (!state.q) return null;
  let groups = res.groups;
  let total = res.total;
  if (state.cat) { // 분류로 거른 결과에는 다른 분류의 건수가 없어서, 거르지 않은 검색을 한 번 더 한다
    const all = await api("GET", "/api/search?" + searchParams({ limit: 1 }, false)).catch(() => null);
    if (!all) return null;
    groups = all.groups;
    total = all.total;
  }
  return { total, byCat: new Map(groups.map((g) => [g.category_id, g.count])) };
}

function renderChip() {
  const chip = $("#cat-chip");
  const c = state.flat.find((x) => x.id === state.cat);
  chip.hidden = !c;
  chip.textContent = c ? `${c.path} ×` : "";
  if (c) chip.setAttribute("aria-label", t("cat_clear", c.path));
  else chip.removeAttribute("aria-label");
}

// 요약 줄의 "최근 24시간 N건" 칩. 켜져 있지 않고 0건이면 숨긴다
function renderSince(n) {
  const chip = $("#since-chip");
  chip.hidden = !state.since && !n;
  chip.textContent = t("since_chip", SINCE_HOURS, n);
  chip.classList.toggle("on", state.since);
  chip.setAttribute("aria-pressed", String(state.since));
}

// 요약 줄의 "정리 대상 N" 칩. 열린 정리 건이 있을 때만 보이고, 누르면 Claude 패널(원격 모드)에 정리 요청을 채운다.
// 패널이 없는 로컬 모드는 Claude Code 에서 하는 법을 알린다
let cleanupSummary = null;
async function renderCleanup() {
  const chip = $("#cleanup-chip");
  try {
    cleanupSummary = await api("GET", "/api/cleanup/summary");
  } catch {
    chip.hidden = true;
    return;
  }
  chip.hidden = !cleanupSummary.open;
  chip.textContent = t("cleanup_chip", cleanupSummary.open);
}

function askCleanup() {
  const ask = t("cleanup_ask");
  if (!$("#claude-btn").hidden) { // 원격 모드 로그인 상태에서만 패널 아이콘이 보인다(claude.js 가 이벤트를 받는다)
    document.dispatchEvent(new CustomEvent("fq-claude-ask", { detail: ask }));
  } else {
    toast(t("cleanup_local"));
  }
}

function setSince(on) {
  state.since = on;
  state.pin = null;
  runSearch();
}

function setCat(id) {
  state.cat = state.cat === id ? null : id;
  renderTree();
  renderChip();
  runSearch();
}

function openSide(open) {
  $("#side").hidden = !open;
  const b = $("#toggle-side");
  b.classList.toggle("on", open);
  b.setAttribute("aria-pressed", String(open));
  store("fq-side", open ? "1" : "0");
}

async function addCategory(e) {
  e.preventDefault();
  const raw = $("#cat-name").value.trim();
  if (!raw) return;
  const parts = raw.split(">").map((s) => s.trim());
  if (parts.length > 2) return toast(t("cat_depth"), { err: true });
  const body = parts.length === 2 ? { name: parts[1], parent: parts[0] } : { name: parts[0] };
  try {
    const c = await api("POST", "/api/categories", body);
    $("#cat-name").value = "";
    $("#cat-form").hidden = true;
    $("#cat-add").hidden = false;
    await refreshCats();
    toast(t("cat_made", c.path));
  } catch (err) {
    toast(err.message, { err: true });
  }
}

// ---------- 검색 ----------

let searchTimer = null;
let searchSeq = 0;
function scheduleSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 150);
}

const SINCE_HOURS = 24;

// 지금 검색 조건의 요청 인자. withCat 이 거짓이면 분류 필터를 뺀다
function searchParams(extra = {}, withCat = true) {
  const p = new URLSearchParams({ q: state.q, ...extra });
  if (withCat && state.cat) p.set("category", state.cat);
  if (state.since && !("since" in extra)) p.set("since", SINCE_HOURS);
  return p;
}

// 칩 건수: 켜져 있으면 본 검색의 건수, 꺼져 있으면 같은 조건에 since 를 더해 건수만 센다
async function sinceCount(res) {
  if (state.since) return res.total;
  const r = await api("GET", "/api/search?" + searchParams({ since: SINCE_HOURS, limit: 0 })).catch(() => null);
  return r ? r.total : 0;
}

async function runSearch() {
  const my = ++searchSeq;
  syncUrl();
  let res;
  try {
    res = await api("GET", "/api/search?" + searchParams({ relax: 1, public: 1 }));
    if (state.pin && !state.q && !state.cat && !state.since && !res.hits.some((h) => h.id === state.pin)) {
      const it = await api("GET", `/api/qa/${state.pin}`).catch(() => null);
      if (it && !it.archived) {
        state.details.set(it.id, it);
        res.hits.unshift(Object.assign({}, it, { tier: "recent", spans: { question: [], answer: [] } }));
      }
    }
  } catch (e) {
    if (my === searchSeq) $("#sum-count").textContent = e.message;
    return;
  }
  const [counts, recentN] = await Promise.all([searchCounts(res), sinceCount(res)]);
  if (my !== searchSeq) return; // 늦게 도착한 이전 응답은 버린다
  renderSince(recentN);
  if (cleanupSummary === null) renderCleanup(); // 첫 검색 때 한 번. 정리는 하루 단위라 검색마다 다시 묻지 않는다
  state.counts = counts;
  renderTree();
  render(res);
}

function render(res) {
  const similar = !res.hits.length && res.similar.length > 0;
  const list = similar ? res.similar : res.hits.slice();
  if (state.editing && !list.some((h) => h.id === state.editing) && rows.has(state.editing)) {
    list.unshift(rows.get(state.editing).hit); // 편집 중인 행은 검색어와 상관없이 둔다
  }
  const shown = new Set(list.map((h) => h.id));
  for (const [id, r] of rows) if (!shown.has(id)) collapse(id, r);

  const order = [];
  const byKey = new Map();
  for (const h of list) {
    const key = similar ? "similar" : String(h.category_id);
    if (!byKey.has(key)) {
      byKey.set(key, []);
      order.push(key);
    }
    byKey.get(key).push(h);
  }
  const counts = new Map(res.groups.map((g) => [String(g.category_id), g.count]));
  const box = $("#results");
  for (const g of groups.values()) g.classList.add("out");
  for (const key of order) {
    const hits = byKey.get(key);
    const g = groupEl(key, similar ? t("similar") : hits[0].category_path, counts.get(key) || hits.length);
    g.classList.remove("out");
    box.append(g);
    const container = g.querySelector(".grp-rows");
    for (const h of hits) container.append(rowEl(h));
    markLast(g);
  }

  summarize(res, similar, order.length);
  renderPublic(res.public);
  const empty = !res.hits.length;
  state.empty = empty;
  $("#empty").hidden = !empty || Boolean($("#new-slot").firstChild);
  if (empty) {
    $("#empty-msg").textContent = res.query
      ? (similar ? t("no_match_similar") : t("no_match"))
      : state.since ? t("none_since", SINCE_HOURS) : t("none_yet");
    $("#empty-new").textContent = res.query ? t("add_as_new", res.query) : t("new_q");
  }
  // 단어를 하나 빼면 결과가 있는 질의. 누르면 그 질의로 검색한다
  fill($("#relaxed"), empty ? (res.relaxed || []).map((r) => el("button", {
    class: "chip", type: "button", title: t("relaxed_title", r.query), onclick: () => searchFor(r.query),
  }, t("relaxed", el("code", {}, r.dropped), r.total))) : null);
  if (state.sel && rows.has(state.sel) && !state.details.has(state.sel)) loadDetail(state.sel);
}

function summarize(res, similar, nGroups) {
  const count = $("#sum-count");
  if (similar) fill(count, t("sum_similar", el("b", {}, "0"), res.similar.length));
  else if (res.query) fill(count, t("sum_query", el("b", {}, res.total), res.total, res.groups.length));
  else fill(count, t("sum_recent", el("b", {}, res.hits.length), res.hits.length, nGroups));
  const notes = [];
  if (res.query && res.morph === "off") notes.push(t("morph_off"));
  if (res.total > res.hits.length && res.query) notes.push(t("top_only", res.hits.length));
  $("#sum-note").textContent = notes.join(" · ");
  $("#sum-ms").textContent = `${res.elapsed_ms} ms`;
}

// 묶음 머리는 접기·펴기 버튼이다. 접은 묶음의 행은 위아래 화살표 이동에서 건너뛴다(visibleIds)
function groupEl(key, path, n) {
  let g = groups.get(key);
  if (!g) {
    const head = el("button", { class: "grp-head", type: "button", onclick: () => fold(key) });
    g = el("section", { class: "grp" }, head,
      el("div", { class: "gbody" }, el("div", { class: "gin" }, el("div", { class: "grp-rows" }))));
    groups.set(key, g);
  }
  const i = path.indexOf(" > ");
  // 분류 설명은 경로 글자의 title 로 둔다. 머리 버튼의 title 은 접기·펴기가 쓴다
  const cat = state.flat.find((c) => String(c.id) === key);
  const top = cat && cat.parent_id ? state.flat.find((c) => c.id === cat.parent_id) : null;
  fill(g.firstChild,
    el("span", { class: "chev", "aria-hidden": "true" }, "▼"),
    i >= 0 ? el("span", { class: "top-name", title: (top && top.description) || null }, path.slice(0, i) + " /") : null,
    el("span", { title: (cat && cat.description) || null }, i >= 0 ? path.slice(i + 3) : path),
    el("span", { class: "rule" }),
    el("span", { class: "grp-n" }, n));
  paintFold(key, g);
  return g;
}

function fold(key) {
  if (state.shut.has(key)) state.shut.delete(key);
  else state.shut.add(key);
  paintFold(key, groups.get(key));
}

function paintFold(key, g) {
  const shut = state.shut.has(key);
  g.classList.toggle("shut", shut);
  g.firstChild.setAttribute("aria-expanded", String(!shut));
  g.firstChild.title = shut ? t("expand") : t("collapse");
}

// 묶음 마지막 행에는 구분선을 긋지 않는다(다음 묶음 머리의 줄과 겹친다)
function markLast(g) {
  const live = [...g.querySelectorAll(".row")].filter((r) => !r.classList.contains("out"));
  live.forEach((r, i) => r.classList.toggle("last", i === live.length - 1));
}

function rowEl(h) {
  let r = rows.get(h.id);
  if (!r) {
    r = el("div", { class: "row", "data-id": h.id }, el("div", { class: "in" }, el("div", { class: "item" })));
    rows.set(h.id, r);
  }
  clearTimeout(r.removeTimer);
  r.classList.remove("out");
  const cached = state.details.get(h.id);
  if (cached && cached.version !== h.version && state.editing !== h.id) state.details.delete(h.id); // 다른 곳에서 고쳤다
  r.hit = h;
  paint(r);
  return r;
}

function collapse(id, r) {
  if (r.classList.contains("out")) return;
  r.classList.add("out");
  const g = r.closest(".grp");
  if (g) markLast(g);
  if (state.sel === id) {
    state.sel = null;
    setHash(null);
    document.dispatchEvent(new Event("fq-select"));
  }
  r.removeTimer = setTimeout(() => {
    r.remove();
    rows.delete(id);
  }, 250);
}

function paint(r) {
  const h = r.hit;
  const item = r.querySelector(".item");
  if (state.editing === h.id) return; // 입력 중인 폼을 다시 그리지 않는다
  item.classList.remove("editing");
  r.classList.toggle("sel", state.sel === h.id);
  fill(item, viewEl(h), state.sel === h.id ? detailEl(h.id) : null);
}

function repaint(id) {
  const r = rows.get(id);
  if (r) paint(r);
}

// 행은 날짜만 오른쪽에 둔다. 작성자·근거 상태·유사도는 데이터에만 있고 행에 내지 않는다
function viewEl(h) {
  const at = kst(h.updated_at);
  const open = state.sel === h.id;
  const aSpans = (h.spans && h.spans.answer) || [];
  // 검색어가 답변 81자 이후에만 있으면 두 줄로 자르면 일치 부분이 가려진다
  const deepHit = aSpans.length > 0 && aSpans.every(([s]) => s > 80);
  return el("button", { class: "rowbtn", type: "button", "aria-expanded": String(open), onclick: () => toggle(h.id) },
    el("span", { class: "qa" },
      el("span", { class: "line" }, el("span", { class: "tag-q" }, "Q"),
        el("span", { class: "qtext" }, marked(h.question, h.spans && h.spans.question))),
      el("span", { class: "line" }, el("span", { class: "tag-a" }, "A"),
        el("span", { class: "atext" + (open || deepHit ? "" : " clamp2") }, marked(h.answer, aSpans)))),
    el("span", { class: "date", title: at.full }, at.md));
}

// ---------- 펼침 ----------

function toggle(id, force) {
  const open = force ?? state.sel !== id;
  const prev = state.sel;
  state.sel = open ? id : null;
  if (prev && prev !== id) repaint(prev);
  repaint(id);
  setHash(open ? id : null);
  document.dispatchEvent(new Event("fq-select")); // Claude 패널의 '함께 보냄' 칩이 듣는다
  if (open) {
    if (state.q) remember(state.q);
    return loadDetail(id);
  }
}

async function loadDetail(id) {
  try {
    const d = await api("GET", `/api/qa/${id}`);
    state.details.set(id, d);
    const r = rows.get(id);
    if (r && (r.hit.question !== d.question || r.hit.answer !== d.answer)) r.hit.spans = null; // 글이 바뀌면 강조 위치가 맞지 않는다
    if (r) Object.assign(r.hit, pick(d));
    if (state.editing !== id) repaint(id);
  } catch (e) {
    toast(e.message, { err: true });
  }
}

function pick(d) {
  const out = {};
  for (const k of ["question", "answer", "category_id", "category_path", "evidence_state", "created_by", "updated_at", "updated_by", "version"]) out[k] = d[k];
  return out;
}

function dline(label, value, extra) {
  const v = el("span", { class: "val ell " + (extra || "") }, value);
  v.addEventListener("click", (e) => {
    if (!e.target.closest("a")) v.classList.toggle("wrap");
  });
  return v;
}

// 로컬 경로를 VS Code 로 여는 주소. 구간마다 인코딩하고(공백·#) 드라이브 콜론은 그대로 둔다
function vscodeUrl(p) {
  const parts = p.replace(/\\/g, "/").split("/").map((s) => (/^[A-Za-z]:$/.test(s) ? s : encodeURIComponent(s)));
  return "vscode://file" + (p.startsWith("/") || p.startsWith("\\") ? "" : "/") + parts.join("/");
}

function evLink(path, ...kids) {
  return el("a", { class: "evlink", href: vscodeUrl(path), title: t("open_vscode", path), onclick: (e) => e.stopPropagation() }, ...kids);
}

// 파일 경로는 폴더와 파일 이름으로 나눈다. 폴더는 앞쪽을 줄이고 파일 이름은 남긴다
function splitPath(p) {
  const i = Math.max(p.lastIndexOf("\\"), p.lastIndexOf("/"));
  return i < 0 ? ["", p] : [p.slice(0, i + 1), p.slice(i + 1)];
}

function evidenceText(d) {
  if (!d.evidence.length) return el("span", { class: "none" }, t("none"));
  const f = document.createDocumentFragment();
  d.evidence.forEach((e, i) => {
    if (i) f.append(el("span", { class: "sep" }, "·"));
    f.append(el("span", { class: "kind" }, KIND[e.kind] || e.kind));
    if (e.kind === "file" || e.kind === "dir") {
      const [dir, file] = splitPath(e.path || "");
      // 폴더 칸은 direction: rtl 로 앞쪽을 줄인다. 끝의 구분자가 앞으로 옮겨 그려지지 않게 LRM 을 붙인다
      const spans = [dir ? el("span", { class: "dir" }, dir + "‎") : null, el("span", { class: "file" }, file)];
      if (e.kind === "file") f.append(evLink(e.path, spans)); // 폴더 근거는 링크를 두지 않는다
      else f.append(...spans.filter(Boolean));
    } else if (e.kind === "git" && e.path) {
      f.append(evLink(e.path, el("span", { class: "target" }, e.path)));
    } else if (e.kind === "git") {
      // 경로가 여럿이면 저장소 기준 상대 경로다. glob 이 든 경로는 열 수 없어 글자로 둔다
      const repo = e.repo.replace(/[\\/]$/, "");
      f.append(el("span", { class: "target" }, repo,
        (e.paths || []).flatMap((p) => [" ", /[*?[]/.test(p) ? p : evLink(`${repo}/${p}`, p)])));
    } else if (e.kind === "url" || e.kind === "github") {
      f.append(el("span", { class: "target" }, el("a", { href: e.path, target: "_blank", rel: "noopener", class: "evlink",
        onclick: (ev) => ev.stopPropagation() }, e.path)));
    } else {
      const target = e.kind === "note" ? e.text
        : e.kind === "githead" ? e.repo
        : e.path || [e.repo, ...(e.paths || [])].join(" ");
      f.append(el("span", { class: "target" }, target));
    }
    if (e.anchor) f.append(el("span", { class: "anchor" }, `#${e.anchor}`));
  });
  return f;
}

function evidenceTitle(d) {
  return d.evidence.map((e) => {
    const target = e.kind === "note" ? e.text : e.kind === "githead" ? e.repo : e.path || [e.repo, ...(e.paths || [])].join(" ");
    return [KIND[e.kind] || e.kind, target, e.anchor ? `#${e.anchor}` : "", e.commit ? e.commit.slice(0, 7) : ""].filter(Boolean).join(" ");
  }).join("\n");
}

function histItems(d) {
  return d.revisions.map((r) => {
    const at = kst(r.at);
    let op = OP[r.op] || r.op;
    if (r.op === "update" && r.before && r.after) {
      const changed = Object.keys(FIELD).filter((k) => r.before[k] !== r.after[k]).map((k) => FIELD[k]);
      if (changed.length) op = t("op_fields", op, changed);
    }
    return `${at.md} ${at.hm} ${r.actor} ${op}`;
  });
}

// 이력은 최근 한 건과 +N 버튼. 누르면 전부(최근 순)와 접기
function histLine(id, d) {
  const items = histItems(d);
  const open = state.histOpen.has(id);
  const more = items.length > 1
    ? el("button", {
      class: "more", type: "button", "aria-expanded": String(open),
      title: open ? t("hist_latest") : t("hist_all", items.length),
      onclick: () => {
        if (open) state.histOpen.delete(id);
        else state.histOpen.add(id);
        repaint(id);
      },
    }, open ? t("collapse") : `+${items.length - 1}`)
    : null;
  return el("span", { class: "val hist" + (open ? " wrap" : "") },
    el("span", { class: open ? "" : "ell" }, open ? items.join("  ·  ") : items[0] || ""), more);
}

function detailEl(id) {
  const d = state.details.get(id);
  const label = (s) => el("span", { class: "label" }, s);
  if (!d) return el("div", { class: "detail" }, el("div", { class: "dline" }, label(""), el("span", { class: "val hist" }, t("loading"))));
  const ev = dline(t("evidence"), evidenceText(d), "ev");
  if (d.evidence.length) ev.title = evidenceTitle(d);
  return el("div", { class: "detail" },
    el("div", { class: "dline" }, label(t("evidence")), ev),
    d.links.length
      ? el("div", { class: "dline" }, label(t("links")),
        dline(t("links"), d.links.map((l) => el("a", { href: `#qa/${l.id}`, class: "ell", title: l.question }, "→ " + l.question)), "links"))
      : null,
    // 같은 분류의 다른 항목(최근 수정순, 연결 항목 제외). 검색 한 번으로 그 주제의 이웃 항목까지 읽는다
    (d.related || []).length
      ? el("div", { class: "dline" }, label(t("related")),
        dline(t("related"), d.related.map((l) => el("a", { href: `#qa/${l.id}`, class: "ell", title: l.question }, "→ " + l.question)), "links"))
      : null,
    el("div", { class: "dline hline" }, label(t("history")), histLine(id, d),
      ibtn("cp", "copy", t("copy"), t("copy"), () => copy('Q:\t'+d.question+'\r\nA:\t'+ d.answer, t("copied_qa"))),
      ibtn("cp", "link", t("copy_addr"), t("copy_addr"), () => copy(`${location.origin}/#qa/${id}`, t("copied_addr"))),
      me && me.mode === "remote" ? ibtn("cp", "share", t("make_share"), t("make_share_title"), () => makeShare("qa", id)) : null,
      el("button", { class: "btn small", type: "button", title: t("edit_key"), onclick: () => startEdit(id) }, t("edit"))));
}

async function copy(text, done) {
  try {
    await navigator.clipboard.writeText(text);
    toast(done);
  } catch (e) {
    toast(t("copy_fail"), { err: true });
  }
}

// ---------- 편집 ----------

// 길이 상한은 가중 길이다. 범위 표는 kb/text.py 의 weight 와 같다(범위 안의 문자는 2, 그 밖은 1)
const WIDE = [[0x1100, 0x115F], [0x2E80, 0xA4CF], [0xAC00, 0xD7A3], [0xF900, 0xFAFF], [0xFE30, 0xFE4F], [0xFF00, 0xFF60],
  [0xFFE0, 0xFFE6]];
const QUESTION_MAX = 300;
const ANSWER_MAX = 800;
const ANSWER_LINES = 6;
const LINE_MAX = 240;

function weight(s) {
  let n = 0;
  for (const ch of s) {
    const c = ch.codePointAt(0);
    n += WIDE.some(([a, b]) => c >= a && c <= b) ? 2 : 1;
  }
  return n;
}

function counter(input, max, lines) {
  const c = el("span", { class: "count", "aria-live": "polite" });
  const update = () => {
    const v = input.value;
    const n = weight(v.trim());
    const filled = v.split("\n").map((s) => s.trim()).filter(Boolean);
    const ln = filled.length;
    c.textContent = `${n}/${max}`;
    c.title = lines ? t("count_title", n, max, ln, lines) : t("count_title_q", n, max);
    c.classList.toggle("over", n > max || Boolean(lines && (ln > lines || filled.some((s) => weight(s) > LINE_MAX))));
  };
  input.addEventListener("input", update);
  update();
  return c;
}

// 답변 칸은 글에 맞춰 2~8줄로 자란다. 8줄을 넘을 때만 스크롤이 생긴다
function grow(ta) {
  const cs = getComputedStyle(ta);
  const line = parseFloat(cs.lineHeight) || 22;
  const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
  ta.style.height = "auto";
  const border = ta.offsetHeight - ta.clientHeight;
  const h = Math.min(Math.max(ta.scrollHeight, line * 2 + pad), line * 8 + pad);
  ta.style.height = h + border + "px";
}

function catSelect(selected) {
  const s = el("select", { class: "fld", "aria-label": t("category") });
  for (const c of state.flat) s.append(el("option", { value: c.id }, c.path));
  if (selected != null && !state.flat.some((c) => c.id === selected)) s.append(el("option", { value: selected }, t("deleted_cat", selected)));
  if (selected != null) s.value = String(selected);
  return s;
}

// 서버는 메모 근거를 "메모:" 로 돌려주고 "메모:"·"note:" 를 모두 받는다. 저장 형식은 그대로 두고 en 화면에 보일 때만 바꾼다
function shownEvidenceLine(line) {
  if (LANG !== "en" || !line) return line || "";
  return line.split(" ; ").map((p) => (p.startsWith("메모:") ? "note:" + p.slice("메모:".length) : p)).join(" ; ");
}

function editForm(base, isNew, onSaved, onCancel) {
  const fq = el("input", { class: "fld fq", "aria-label": t("question"), value: base.question, autocomplete: "off" });
  const fa = el("textarea", { class: "fld fa", "aria-label": t("answer"), rows: 2 });
  fa.value = base.answer;
  fa.addEventListener("input", () => grow(fa));
  setTimeout(() => grow(fa), 0);
  const fc = catSelect(base.category_id);
  const shownLine = shownEvidenceLine(base.evidence_line);
  const fe = el("input", {
    class: "fld fe", "aria-label": t("evidence_opt"), value: shownLine, autocomplete: "off",
    spellcheck: "false", placeholder: t("evidence_ph"),
  });
  const msg = el("div", { class: "emsg", role: "alert" });
  const sim = el("div", { class: "similar" });
  const busy = (on) => form.querySelectorAll("button").forEach((b) => (b.disabled = on));

  async function submit(force) {
    msg.textContent = "";
    busy(true);
    try {
      if (isNew) {
        const body = { category: Number(fc.value), question: fq.value, answer: fa.value, force };
        if (fe.value.trim()) body.evidence_line = fe.value;
        const r = await api("POST", "/api/qa", body);
        if (r.status === "similar_found") return showSimilar(r.similar);
        return onSaved(r.item, t("added"));
      }
      const body = { version: base.version };
      if (fq.value !== base.question) body.question = fq.value;
      if (fa.value !== base.answer) body.answer = fa.value;
      if (Number(fc.value) !== base.category_id) body.category = Number(fc.value);
      if (fe.value.trim() !== shownLine) body.evidence_line = fe.value;
      if (Object.keys(body).length === 1) return onCancel();
      onSaved(await api("PATCH", `/api/qa/${base.id}`, body), t("saved"));
    } catch (e) {
      if (e.status === 409) {
        await loadDetail(base.id);
        const fresh = state.details.get(base.id);
        if (fresh) {
          form.replaceWith(editForm(fresh, false, onSaved, onCancel));
          return toast(t("conflict_reopened"), { err: true });
        }
      }
      msg.textContent = e.message;
    } finally {
      busy(false);
    }
  }

  function showSimilar(list) {
    fill(sim,
      el("div", { class: "head" }, t("similar_head")),
      list.map((h) => el("div", {},
        el("a", { href: `#qa/${h.id}` }, h.question),
        el("span", { class: "sub" }, `${h.category_path} · ${Math.round(h.score * 100)}%`))),
      el("div", {}, el("button", { class: "btn", type: "button", onclick: () => submit(true) }, t("add_anyway"))));
  }

  // 삭제는 행을 지우지 않고 보관 표시만 한다(되돌리기·MCP kb_restore 로 복원된다)
  async function remove() {
    if (!window.confirm(t("delete_confirm"))) return;
    try {
      const it = await api("POST", `/api/qa/${base.id}/archive`, { version: base.version });
      onSaved(it, null);
      toast(t("deleted"), {
        action: {
          label: t("undo"),
          fn: async () => {
            try {
              await api("POST", `/api/qa/${it.id}/restore`, { version: it.version });
              toast(t("restored"));
              refreshCats();
              runSearch();
            } catch (e) {
              toast(e.message, { err: true });
            }
          },
        },
      });
    } catch (e) {
      msg.textContent = e.message;
    }
  }

  const form = el("form", {
    class: "edit",
    onsubmit: (e) => { e.preventDefault(); submit(false); },
    onkeydown: (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(false); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onCancel(); }
    },
  },
  el("div", { class: "eline" }, el("span", { class: "tag-q" }, "Q"), fq, counter(fq, QUESTION_MAX)),
  el("div", { class: "eline multi" }, el("span", { class: "tag-a" }, "A"), fa, counter(fa, ANSWER_MAX, ANSWER_LINES)),
  el("div", { class: "erow" }, fc, fe,
    el("button", { class: "btn primary", type: "submit", title: isNew ? t("add_key") : t("save_key") }, isNew ? t("add_btn") : t("save")),
    el("button", { class: "btn", type: "button", title: t("cancel_esc"), onclick: () => onCancel() }, t("cancel")),
    isNew ? null : el("span", { class: "spacer" }),
    isNew ? null : ibtn("del", "trash", t("delete_q"), t("del"), remove)),
  msg, sim);
  setTimeout(() => (isNew && fq.value ? fa : fq).focus(), 0);
  return form;
}

function startEdit(id) {
  const d = state.details.get(id);
  const r = rows.get(id);
  if (!d || !r) return;
  state.sel = id;
  state.editing = id;
  r.classList.remove("sel");
  const item = r.querySelector(".item");
  item.classList.add("editing");
  item.replaceChildren(editForm(d, false, (it, msg) => finishEdit(id, it, msg), () => cancelEdit(id)));
}

function cancelEdit(id) {
  state.editing = null;
  repaint(id);
  const btn = rows.get(id) && rows.get(id).querySelector(".rowbtn");
  if (btn) btn.focus();
}

function finishEdit(id, it, msg) {
  state.editing = null;
  if (it.archived) {
    state.details.delete(id);
    state.sel = null;
    setHash(null);
    document.dispatchEvent(new Event("fq-select"));
    const r = rows.get(id);
    if (r) collapse(id, r);
  } else {
    state.details.set(id, it);
    const r = rows.get(id);
    if (r) Object.assign(r.hit, pick(it), { spans: null });
    repaint(id);
  }
  if (msg) toast(msg);
  refreshCats();
  runSearch();
}

// ---------- 공개 지식 ----------
// 운영자가 적재한 공개 DB 의 검색 결과. 내 결과 아래에 따로 보이고, 항목을 펼치면 내 DB 로 복사할 수 있다.
// 공개 항목의 번호는 내 항목 번호와 겹치므로 본 목록(rows)에 섞지 않는다

const pub = { open: null, details: new Map(), last: null };

function renderPublic(found) {
  pub.last = found;
  const box = $("#public");
  const hits = (found && found.hits) || [];
  box.hidden = !hits.length;
  if (!hits.length) {
    box.replaceChildren();
    return;
  }
  const head = el("div", { class: "grp-head pub-head" }, el("span", { class: "top-name" }, t("public")),
    el("span", { class: "rule" }), el("span", { class: "grp-n" }, found.total));
  fill(box, head, el("div", { class: "grp-rows" }, ...hits.map(pubRowEl)));
}

function pubRowEl(h) {
  const open = pub.open === h.id;
  const r = el("div", { class: "row pub-row" + (open ? " sel" : ""), "data-pub": h.id },
    el("div", { class: "in" }, el("div", { class: "item" },
      el("button", { class: "rowbtn", type: "button", "aria-expanded": String(open), onclick: () => togglePub(h.id) },
        el("span", { class: "qa" },
          el("span", { class: "line" }, el("span", { class: "tag-q" }, "Q"),
            el("span", { class: "qtext" }, marked(h.question, h.spans && h.spans.question))),
          el("span", { class: "line" }, el("span", { class: "tag-a" }, "A"),
            el("span", { class: "atext" + (open ? "" : " clamp2") }, marked(h.answer, (h.spans && h.spans.answer) || [])))),
        el("span", { class: "date", title: h.category_path }, h.category_path.split(" > ").pop())),
      open ? pubDetailEl(h) : null)));
  return r;
}

function togglePub(id) {
  pub.open = pub.open === id ? null : id;
  renderPublicKeep();
}

function pubDetailEl(h) {
  const label = (s) => el("span", { class: "label" }, s);
  const d = pub.details.get(h.id);
  if (!d) {
    api("GET", `/api/public/qa/${h.id}`).then((got) => {
      pub.details.set(h.id, got);
      if (pub.open === h.id) renderPublicKeep();
    }).catch((e) => toast(e.message, { err: true }));
  }
  const ev = d && d.evidence.length
    ? d.evidence.map((e) => e.kind === "url" ? el("a", { href: e.path, target: "_blank", rel: "noopener" }, e.path) : el("span", {}, e.text)).flatMap((n, i) => (i ? [" · ", n] : [n]))
    : t("none");
  return el("div", { class: "detail" },
    el("div", { class: "dline" }, label(t("category")), el("span", { class: "val" }, h.category_path)),
    el("div", { class: "dline" }, label(t("evidence")), el("span", { class: "val wrap" }, ...[].concat(ev))),
    el("div", { class: "dline hline" }, label(""), el("span", { class: "val hist" }, t("pub_note")),
      ibtn("cp", "copy", t("copy"), t("copy_qa"), () => copy("Q:\t" + h.question + "\r\nA:\t" + h.answer, t("copied_qa"))),
      el("button", { class: "btn small", type: "button", onclick: () => copyPublic(h) }, t("to_mine"))));
}

// 펼침·상세 도착 때 마지막 검색 결과의 공개 묶음을 같은 데이터로 다시 그린다
function renderPublicKeep() {
  renderPublic(pub.last);
}

async function copyPublic(h, force) {
  let res;
  try {
    res = await api("POST", `/api/public/qa/${h.id}/copy`, force ? { force: true } : {});
  } catch (e) {
    toast(e.message, { err: true });
    return;
  }
  if (res.status === "similar_found") {
    const first = res.similar[0];
    if (window.confirm(t("pub_similar", first.question))) return copyPublic(h, true);
    return;
  }
  const it = res.item;
  toast(t("pub_copied", it.category_path));
  state.details.set(it.id, it);
  refreshCats();
  runSearch();
}

// ---------- 새 질문 ----------

function openNew(question) {
  const slot = $("#new-slot");
  if (slot.firstChild) {
    const q = slot.querySelector(".fq");
    if (question && !q.value) q.value = question;
    q.focus();
    return;
  }
  if (!state.flat.length) {
    openSide(true);
    toast(t("no_cats"), { err: true });
    return;
  }
  const firstHit = rows.size ? [...rows.values()].find((r) => !r.classList.contains("out")) : null;
  const cat = state.cat || (firstHit && firstHit.hit.category_id) || state.flat[0].id;
  const close = () => {
    slot.replaceChildren();
    $("#empty").hidden = !state.empty;
  };
  const base = { question: question || "", answer: "", category_id: cat, evidence_line: "" };
  $("#empty").hidden = true; // 새 질문 폼이 열려 있으면 0건 안내를 숨긴다
  slot.append(el("div", { class: "newform" }, editForm(base, true, (it, msg) => {
    close();
    toast(msg);
    state.q = "";
    $("#q").value = "";
    syncRecentList();
    state.cat = null;
    state.pin = it.id;
    state.sel = it.id;
    state.details.set(it.id, it);
    setHash(it.id);
    document.dispatchEvent(new Event("fq-select"));
    refreshCats();
    runSearch().then(() => scrollTo(it.id));
  }, close)));
}

// ---------- 이동·주소 ----------

function setHash(id) {
  history.replaceState(null, "", id ? `#qa/${id}` : location.pathname + location.search);
}

// 검색어·분류 필터는 주소의 ?q=...&cat=... 로 남긴다. 펼친 항목 #qa/<id> 는 그대로 둔다
function syncUrl() {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  if (state.cat) p.set("cat", state.cat);
  if (state.since) p.set("since", SINCE_HOURS);
  const s = p.toString();
  history.replaceState(null, "", location.pathname + (s ? `?${s}` : "") + location.hash);
}

// 첫 로드 때 주소의 검색 상태를 되살린다. 없는 분류 id 는 버린다(칩 없이 필터만 걸리지 않게)
function restoreFromUrl() {
  const p = new URLSearchParams(location.search);
  const q = (p.get("q") || "").trim();
  const cat = Number(p.get("cat"));
  state.q = q;
  $("#q").value = q;
  syncRecentList();
  state.cat = state.flat.some((c) => c.id === cat) ? cat : null;
  state.since = p.has("since");
  renderTree();
  renderChip();
}

// 화면 밖이면 그 행이 보이게 스크롤한다. 위는 붙는 묶음 머리 높이(44px), 아래는 16px 를 비운다.
// Claude 패널이 열려 있으면 페이지 대신 목록 칸(#app)이 스크롤한다
function scrollTo(id) {
  const r = rows.get(id);
  if (!r) return;
  const box = $("#app");
  const own = getComputedStyle(box).overflowY === "auto";
  const view = own ? box.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
  const b = r.getBoundingClientRect();
  const top = view.top + 44;
  const bottom = view.bottom - 16;
  const by = (dy) => (own ? box : window).scrollBy({ top: dy, behavior: "smooth" });
  if (b.top < top) by(b.top - top);
  else if (b.bottom > bottom) by(Math.min(b.bottom - bottom, b.top - top));
}

async function openFromHash() {
  const pm = location.hash.match(/^#pub\/(\d+)$/);
  if (pm) {
    const id = Number(pm[1]);
    try {
      const d = await api("GET", `/api/public/qa/${id}`);
      pub.details.set(id, d);
      pub.open = id;
      renderPublic({ total: 1, hits: [d] });
      $("#public").scrollIntoView({ block: "center" });
    } catch (e) {
      toast(e.message, { err: true });
    }
    return;
  }
  const m = location.hash.match(/^#qa\/(\d+)$/);
  if (!m) return;
  const id = Number(m[1]);
  state.pin = id;
  if (!rows.has(id) || rows.get(id).classList.contains("out")) {
    state.q = "";
    $("#q").value = "";
    syncRecentList();
    state.cat = null;
    state.since = false;
    renderTree();
    renderChip();
    await runSearch();
  }
  if (rows.has(id)) {
    toggle(id, true);
    scrollTo(id);
  } else {
    toast(t("not_found_id", id), { err: true });
  }
}

function visibleIds() {
  return [...document.querySelectorAll("#results .grp:not(.out):not(.shut) .row:not(.out)")].map((r) => Number(r.dataset.id));
}

// 이동하면서 바로 펼친다
function move(dir) {
  if (state.editing) return;
  const ids = visibleIds();
  if (!ids.length) return;
  const i = ids.indexOf(state.sel);
  const next = ids[i < 0 ? (dir > 0 ? 0 : ids.length - 1) : Math.min(ids.length - 1, Math.max(0, i + dir))];
  revealAfterOpen(next);
}

// 펼치면서 보이게 스크롤한다. 근거와 이력 줄은 상세를 읽은 뒤 늘어나므로 그때 한 번 더 맞춘다
function revealAfterOpen(id) {
  const loaded = toggle(id, true);
  requestAnimationFrame(() => scrollTo(id));
  loaded?.then(() => {
    if (state.sel === id) requestAnimationFrame(() => scrollTo(id));
  });
}

// 최근 검색어 10개(localStorage). 입력 중간값이 쌓이지 않게 입력이 2초 멈췄을 때, Enter, 결과 행 펼침, 칩 클릭 때만 남긴다
const RECENT_MAX = 10;
let rememberTimer = null;

function recentList() {
  try {
    const v = JSON.parse(store("fq-recent") || "[]");
    return Array.isArray(v) ? v.filter((s) => typeof s === "string") : [];
  } catch (e) {
    return [];
  }
}

function renderRecent(list = recentList()) {
  fill($("#recent-q"), list.map((s) => el("option", { value: s })));
}

// 최근 검색어 제안은 검색창이 비었을 때만 붙인다(빈 검색창을 누르면 열린다).
// 글자가 있을 때 브라우저의 제안 목록이 열려 있으면 그 목록이 위아래 화살표를 가져가 결과 행 이동이 막힌다
function syncRecentList() {
  const q = $("#q");
  if (q.value) q.removeAttribute("list");
  else q.setAttribute("list", "recent-q");
}

function remember(query) {
  clearTimeout(rememberTimer);
  const q = (query || "").trim();
  if (!q) return;
  const list = [q, ...recentList().filter((s) => s !== q)].slice(0, RECENT_MAX);
  store("fq-recent", JSON.stringify(list));
  renderRecent(list);
}

function searchFor(query) {
  remember(query);
  const q = $("#q");
  q.value = query;
  syncRecentList();
  state.q = query.trim();
  state.pin = null;
  q.focus();
  runSearch();
}

function clearSearch() {
  const q = $("#q");
  q.value = "";
  syncRecentList();
  state.q = "";
  state.cat = null;
  state.since = false;
  renderTree();
  renderChip();
  runSearch();
}

// 검색 문법 팝오버. `?` 키 단축은 두지 않는다(검색어에 ? 가 들어갈 수 있다)
function openHelp(open) {
  $("#help").hidden = !open;
  $("#help-btn").setAttribute("aria-expanded", String(open));
}

// ---------- 테마 ----------

// 저장은 사용자가 상단에서 바꿨을 때만 한다. 저장값이 없으면 계속 OS 설정을 따른다
function setTheme(theme, save) {
  document.documentElement.setAttribute("data-theme", theme);
  if (save) store("fq-theme", theme);
  $("#theme-btn").setAttribute("aria-label", theme === "dark" ? t("to_light") : t("to_dark"));
}

// ---------- 시작 ----------

// ---------- 원격 모드 로그인 ----------
const LOGIN_ERRORS = t("login_err");

function showLogin(providers) {
  if (!$("#login").hidden) return;
  // 세션이 끝나면 Claude 패널도 닫는다. 열어 둔 설정(fq-claude-open)은 두어 다시 로그인하면 다시 열린다
  $("#claude").hidden = true;
  document.body.classList.remove("claude-open");
  const p = new URLSearchParams(location.search);
  const code = p.get("login_error");
  p.delete("login_error");
  const rest = p.toString();
  if (code) history.replaceState(null, "", location.pathname + (rest ? `?${rest}` : "") + location.hash);
  // 로그인 뒤 지금 주소(검색어·펼친 항목)로 돌아온다
  const next = encodeURIComponent(location.pathname + location.search + location.hash);
  fill($("#login-btns"), providers.map((name) =>
    el("a", { class: "pbtn", href: `/auth/login/${name}?next=${next}` }, t("login_with", PROVIDERS[name] || name))));
  if (!providers.length) fill($("#login-btns"), el("p", { class: "login-none" }, t("no_providers")));
  $("#login-err").hidden = !code;
  $("#login-err").textContent = LOGIN_ERRORS[code] || "";
  for (const id of ["#app", "#toggle-side", "#new-btn", "#account"]) $(id).hidden = true;
  $(".keys").hidden = true;
  document.body.classList.add("login-mode"); // 상단은 테마 버튼만 남아 오른쪽 열 위에 뜬다
  $("#login").hidden = false;
}

// 로그인 수단 추가 결과(/auth/link 콜백이 붙인 쿼리)
const LINK_ERRORS = t("link_err");

function linkResult() {
  const p = new URLSearchParams(location.search);
  const ok = p.get("linked"), err = p.get("link_error"), dropped = p.get("discarded");
  if (!ok && !err) return;
  for (const k of ["linked", "link_error", "discarded"]) p.delete(k);
  const rest = p.toString();
  history.replaceState(null, "", location.pathname + (rest ? `?${rest}` : "") + location.hash);
  if (err) toast(LINK_ERRORS[err] || t("link_fail"), { err: true });
  else toast(t("linked", PROVIDERS[ok] || ok, dropped));
}

let me = null;  // 원격 모드 로그인 정보(/api/me)

function showAccount(account) {
  const fallback = account.providers.map((n) => t("account_of", PROVIDERS[n] || n)).join(", ");
  $("#account-name").textContent = account.display_name || fallback;
  $("#account-name").title = t("signed_in_with", fallback);
  $("#account").hidden = false;
}

async function logout() {
  try {
    await api("POST", "/auth/logout");
  } catch (e) {
    toast(e.message, { err: true });
    return;
  }
  document.dispatchEvent(new Event("fq-logout")); // Claude 패널 대화 기록을 지운다
  location.reload();
}

// ---------- 원격 모드 연결: 한 줄 연결과 플러그인 설치 명령 ----------
// 상자와 복사 내용은 명령 한 줄뿐이다(붙여 넣어 바로 실행되게). 안내는 상자 밖 문장으로 둔다.

// 설치 없는 한 줄 연결. 등록한 뒤 Claude Code 의 /mcp 에서 브라우저로 로그인·승인한다(OAuth, web/oauth_as.py)
const ONE_LINE = (url) => `claude mcp add --transport http -s user fast-question ${url}/mcp`;

// 플러그인 설치(.claude-plugin/marketplace.json)와 PC 로그인(fq_client/login.py). 셸마다 같은 명령이다.
// 로그인은 버전을 고정하지 않는다. 받은 패키지가 uv 캐시에 남아 플러그인의 첫 MCP·훅 실행이 빨라진다
const PLUGIN_CMDS = (url) => [
  "claude plugin marketplace add YeomYuJun/fast-question",
  "claude plugin install fast-question@fast-question",
  `uvx fast-question login --url ${url}`,
];

function cmdBox(command, label, done) {
  return el("div", { class: "cmd-box" }, el("pre", { class: "mono" }, command),
    ibtn("cp", "copy", t("copy_of", label), t("copy_of", label), () => copy(command, done)));
}

// 번호 단계 하나. 번호는 장식이라 읽지 않는다
function cnStep(n, title, ...body) {
  return el("div", { class: "cn-step" }, el("span", { class: "cn-step-n", "aria-hidden": "true" }, n),
    el("div", { class: "cn-step-body" }, el("span", { class: "cn-step-title" }, title), ...body));
}

function openConnect(open) {
  $("#connect").hidden = !open;
  $("#connect-btn").classList.toggle("on", open);
  $("#connect-btn").setAttribute("aria-expanded", String(open));
  if (open) renderConnect();
}

async function revokeToken(tk) {
  if (!window.confirm(t("token_revoke_confirm", tk.name))) return;
  try {
    await api("DELETE", `/api/account/tokens/${tk.id}`);
  } catch (e) {
    toast(e.message, { err: true });
    return;
  }
  toast(t("token_revoked"));
  renderConnect();
}

// 연결된 앱: OAuth 로 승인한 연결(한 줄 연결, claude.ai 커넥터). 다시 연결할 때마다 클라이언트가 새로 등록되어
// 이름이 같은 줄이 생기므로 연결한 시각까지 보인다
const appName = (a) => a.name || t("unnamed_app");
const stamp = (iso) => {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};

async function disconnectApp(a) {
  if (!window.confirm(t("app_disconnect_confirm", stamp(a.connected_at), appName(a)))) return;
  try {
    await api("DELETE", `/api/account/apps/${encodeURIComponent(a.client_id)}`);
  } catch (e) {
    toast(e.message, { err: true });
    return;
  }
  toast(t("app_disconnected"));
  renderConnect();
}

// 공개 링크: 항목·분류 하나를 로그인 없이 보는 주소. 만들면 바로 복사하고, 목록·폐기는 연결 패널에 있다
async function makeShare(kind, id) {
  try {
    const made = await api("POST", "/api/account/shares", { kind, id });
    await copy(made.url, t("share_copied"));
  } catch (e) {
    toast(e.message, { err: true });
  }
}

async function revokeShare(s) {
  if (!window.confirm(t("share_revoke_confirm", s.title))) return;
  try {
    await api("DELETE", `/api/account/shares/${s.id}`);
    toast(t("share_revoked"));
    renderConnect();
  } catch (e) {
    toast(e.message, { err: true });
  }
}

function sharesBlock(shares) {
  return el("div", { class: "cn-sec tokens" },
    el("div", { class: "cn-label-row" }, el("span", { class: "cn-label" }, t("shares")), el("span", { class: "cn-count" }, shares.length)),
    shares.length
      ? el("ul", { class: "token-list" }, shares.map((s) => el("li", { class: "token-item" },
        el("div", { class: "tk" }, el("span", { class: "name" }, s.title),
          el("span", { class: "meta mono" }, s.kind === "qa" ? t("share_kind_qa") : t("category"), t("created_on", (s.created_at || "").slice(0, 10)))),
        el("button", { class: "btn small", type: "button", "aria-label": t("share_revoke_of", s.title), onclick: () => revokeShare(s) }, t("revoke")))))
      : el("p", { class: "connect-note" }, t("shares_none")));
}

function appsBlock(apps) {
  return el("div", { class: "cn-sec tokens" },
    el("div", { class: "cn-label-row" }, el("span", { class: "cn-label" }, t("apps")), el("span", { class: "cn-count" }, apps.length)),
    apps.length
      ? el("ul", { class: "token-list" }, apps.map((a) => el("li", { class: "token-item" },
        el("div", { class: "tk" }, el("span", { class: "name" }, appName(a)),
          el("span", { class: "meta mono", title: t("app_meta_title", a.connected_at, a.last_used_at) },
            el("span", { class: "nw" }, t("app_connected", stamp(a.connected_at))), " · ",
            el("span", { class: "nw" }, t("app_last_used", stamp(a.last_used_at))))),
        el("button", { class: "btn small", type: "button", "aria-label": t("app_disconnect_of", stamp(a.connected_at), appName(a)),
          onclick: () => disconnectApp(a) }, t("disconnect")))))
      : el("p", { class: "connect-note" }, t("apps_none")));
}

async function renderConnect() {
  let tokens = [];
  let apps = [];
  let shares = [];
  const [tk, ap, sh] = await Promise.allSettled([api("GET", "/api/account/tokens"), api("GET", "/api/account/apps"),
    api("GET", "/api/account/shares")]);
  if (tk.status === "fulfilled") tokens = tk.value.tokens;
  else toast(tk.reason.message, { err: true });
  if (ap.status === "fulfilled") apps = ap.value.apps;
  else if (tk.status === "fulfilled") toast(ap.reason.message, { err: true });
  if (sh.status === "fulfilled") shares = sh.value.shares;
  const day = (iso) => (iso || "").slice(0, 10);
  // 휴대폰, 태블릿(마우스 없이 터치만)에서는 Claude Code 를 돌릴 수 없어 연결 명령을 빼고 안내만 둔다.
  // 브라우저 이름(UA) 대신 입력 장치로 판단한다. Safari, Chrome, Edge, Firefox 모두 이 미디어 쿼리를 지원한다
  const touchOnly = window.matchMedia("(hover: none) and (pointer: coarse)").matches;
  const oneLine = ONE_LINE(location.origin);
  const pluginCmds = PLUGIN_CMDS(location.origin);
  fill($("#connect"),
    el("div", { class: "cn-head" },
      el("div", {}, el("h2", {}, t("connect")),
        el("p", { class: "connect-note" }, touchOnly ? t("connect_touch") : t("connect_intro"))),
      el("button", { class: "btn small", type: "button", onclick: () => openConnect(false) }, t("close"))),
    touchOnly ? null : el("div", { class: "cn-sec code" }, el("span", { class: "cn-label" }, t("cc_connect")),
      cnStep("1", t("step1"),
        el("p", { class: "connect-note" }, t("step1_note")),
        cmdBox(oneLine, t("cmd_connect"), t("cmd_connect_done"))),
      cnStep("2", t("step2"),
        el("p", { class: "connect-note" }, t("step2_note")),
        cmdBox(pluginCmds[0], t("cmd_market"), t("cmd_market_done")),
        cmdBox(pluginCmds[1], t("cmd_plugin"), t("cmd_plugin_done")),
        cmdBox(pluginCmds[2], t("cmd_login"), t("cmd_login_done")),
        el("p", { class: "connect-note" }, t("step2_after")))),
    el("div", { class: "cn-sec tokens" },
      el("div", { class: "cn-label-row" }, el("span", { class: "cn-label" }, t("tokens")), el("span", { class: "cn-count" }, tokens.length)),
      tokens.length
        ? el("ul", { class: "token-list" }, tokens.map((tk) => el("li", { class: "token-item" + (tk.expired ? " expired" : "") },
          el("div", { class: "tk" }, el("span", { class: "name" }, tk.name),
            el("span", { class: "meta mono", title: t("token_created", tk.created_at) },
              tk.expired ? t("expired") : t("expires", day(tk.expires_at)), tk.last_used_at ? t("last_used", day(tk.last_used_at)) : t("never_used"))),
          ibtn("del", "trash", t("token_revoke_of", tk.name), t("revoke"), () => revokeToken(tk)))))
        : el("p", { class: "connect-note" }, t("tokens_none"))),
    appsBlock(apps),
    sharesBlock(shares),
    loginBlock(),
    el("div", { class: "cn-sec" }, el("span", { class: "cn-label" }, t("export")),
      el("p", { class: "connect-note" }, t("export_note")),
      el("div", { class: "cn-row" }, el("a", { class: "btn", href: "/api/export/db", download: "" }, t("export_db")))),
    leaveForm(),
    el("div", { class: "cn-foot" }, el("a", { href: "/terms" }, t("terms")), el("a", { href: "/privacy" }, t("privacy"))));
}

// 로그인 수단: 지금 계정에 다른 공급자 로그인을 붙인다. 그 공급자 계정이 이미 쓰던 DB 는 버린다
function loginBlock() {
  const mine = me?.account?.providers || [];
  const more = (me?.available || []).filter((n) => !mine.includes(n));
  const label = (n) => PROVIDERS[n] || n;
  return el("div", { class: "cn-sec" }, el("span", { class: "cn-label" }, t("logins")),
    el("p", { class: "connect-note" }, t("logins_now", mine.map(label).join(" · ")) + (more.length ? t("logins_more") : "")),
    more.length ? el("div", { class: "cn-row" }, more.map((n) =>
      el("form", { method: "post", action: `/auth/link/${n}`, onsubmit: (ev) => {
        if (!window.confirm(t("login_add_confirm", label(n)))) ev.preventDefault();
      } }, el("button", { class: "btn", type: "submit" }, t("login_add", label(n)))))) : null);
}

// 탈퇴: 확인 문구를 입력해야 버튼이 동작한다. 서버(web/auth.py 의 LEAVE_WORDS)가 "탈퇴" 와 "delete" 를 모두 받는다
function leaveForm() {
  const word = el("input", { class: "fld", "aria-label": t("leave_word_label"), placeholder: t("leave_word"), autocomplete: "off" });
  return el("form", { class: "cn-sec", onsubmit: async (ev) => {
    ev.preventDefault();
    try {
      await api("POST", "/api/account/delete", { confirm: word.value.trim() });
    } catch (e) {
      toast(e.message, { err: true });
      return;
    }
    document.dispatchEvent(new Event("fq-logout"));
    location.href = "/";
  } },
  el("span", { class: "cn-label" }, t("leave")),
  el("p", { class: "connect-note" }, t("leave_note")),
  el("div", { class: "cn-row" }, word, el("button", { class: "btn danger", type: "submit" }, t("leave"))));
}

// 로컬 모드는 그대로 시작한다. 원격 모드는 로그인했을 때만 true
async function whoami() {
  try {
    me = await api("GET", "/api/me");
    if (me.account) {
      showAccount(me.account);
      linkResult();
    }
    return true;
  } catch (e) {
    return e.status !== 401;  // 그 밖의 실패는 화면을 띄우고 각 요청의 오류로 보인다
  }
}

function init() {
  applyStatic();
  window.fqLangButtons();
  const q = $("#q");
  // 검색어를 입력하면 펼친 행과 편집을 닫는다
  q.addEventListener("input", () => {
    syncRecentList();
    state.q = q.value.trim();
    if (state.q) state.pin = null;
    if (state.editing) {
      const id = state.editing;
      state.editing = null;
      repaint(id);
    }
    if (state.sel) toggle(state.sel, false);
    scheduleSearch();
    clearTimeout(rememberTimer);
    rememberTimer = setTimeout(() => remember(q.value), 2000);
  });

  document.addEventListener("keydown", (e) => {
    const a = document.activeElement;
    const typing = a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT");
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    if (e.key === "/" && !typing) {
      e.preventDefault();
      q.focus();
      q.select();
      return;
    }
    if (typing && a !== q) return; // 편집 폼 안의 키는 폼이 처리한다
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      move(e.key === "ArrowDown" ? 1 : -1);
    } else if (e.key === "Enter" && a === q) {
      // 펼친 행이 없을 때만 첫 결과를 펼친다
      e.preventDefault();
      remember(q.value);
      const ids = visibleIds();
      if (ids.length && !ids.includes(state.sel)) revealAfterOpen(ids[0]);
    } else if (e.key === "Escape") {
      // 문법 도움말이 열려 있으면 그것만 닫는다. 아니면 펼친 행을 먼저 접고, 없으면 검색어·분류 필터·24시간 보기를 지운다
      if (!$("#help").hidden) openHelp(false);
      else if (state.sel && rows.has(state.sel) && !rows.get(state.sel).classList.contains("out")) toggle(state.sel, false);
      else if (q.value || state.cat || state.since) clearSearch();
    } else if ((e.key === "e" || e.key === "E") && !typing && state.sel && !state.editing && state.details.has(state.sel)) {
      e.preventDefault();
      startEdit(state.sel);
    }
  });

  $("#help-btn").append(icon("help"));
  $("#help-btn").addEventListener("click", () => openHelp($("#help").hidden));
  // 검색창 빈 곳을 누르면 입력칸에 커서를 둔다(label 대신 div 라 직접 처리한다)
  $("#search").addEventListener("click", (e) => { if (!e.target.closest("button, input, #help")) q.focus(); });
  document.addEventListener("click", (e) => { if (!e.target.closest("#help, #help-btn")) openHelp(false); });

  $("#new-btn").addEventListener("click", () => openNew(state.q));
  $("#empty-new").addEventListener("click", () => openNew(state.q));
  $("#cat-chip").addEventListener("click", () => setCat(null));
  $("#since-chip").addEventListener("click", () => setSince(!state.since));
  $("#cleanup-chip").addEventListener("click", askCleanup);
  $("#toggle-side").addEventListener("click", () => openSide($("#side").hidden));
  $("#cat-add").addEventListener("click", () => {
    $("#cat-form").hidden = false;
    $("#cat-add").hidden = true;
    $("#cat-name").focus();
  });
  $("#cat-cancel").addEventListener("click", () => {
    $("#cat-form").hidden = true;
    $("#cat-add").hidden = false;
  });
  $("#cat-form").addEventListener("submit", addCategory);
  $("#cat-edit").addEventListener("click", () => setCatEdit(!state.catEdit));
  $("#theme-btn").addEventListener("click", () => {
    setTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark", true);
  });
  setTheme(document.documentElement.getAttribute("data-theme") || "light");
  openSide(store("fq-side") === "1");
  renderRecent();

  $("#logout-btn").addEventListener("click", logout);
  $("#connect-btn").addEventListener("click", () => openConnect($("#connect").hidden));

  window.addEventListener("hashchange", openFromHash);
  // 다른 창(Claude 등록 등)에서 바뀐 내용을 화면으로 돌아올 때 다시 읽는다
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && $("#login").hidden && !state.editing && !state.catRenaming
        && !$("#new-slot").firstChild) {
      refreshCats();
      runSearch();
    }
  });

  whoami().then((ok) => ok && refreshCats().then(() => {
    restoreFromUrl();
    if (!location.hash) return runSearch();
    // 검색 상태가 있으면 그 결과를 먼저 그린다. 펼칠 항목이 결과에 없을 때만 openFromHash 가 검색을 푼다
    return (state.q || state.cat || state.since ? runSearch() : Promise.resolve()).then(openFromHash);
  }));
}

init();
