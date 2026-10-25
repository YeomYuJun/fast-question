"use strict";
// 웹 Claude 패널(원격 모드). 서버(web/claude_chat.py)가 사용자 API 키로 대화 반복을 돌리고 진행을 SSE 로 보낸다.
// app.js 의 $, el, fill, icon, ICONS, api, toast, store, state, refreshCats, runSearch 를 쓴다(app.js 뒤에 읽힌다).
// 대화 기록(API 형식 메시지)은 sessionStorage 에만 두고 매번 서버에 보낸다. 생각, 대체 모델 블록은 받은 그대로 돌려보낸다.
// Claude 의 글은 mdRender() 로만 그린다(HTML 로 해석하지 않는다). 도구 결과는 MCP 도구가 돌려준 JSON 문자열이다.
// 패널은 머리, 대화, 입력 세 띠다. 대화 띠만 스크롤하고, 패널이 열린 동안 페이지 대신 목록 칸(#app)이 스크롤한다(app.css).

const CL_HISTORY = "fq-claude-history";
const CL_THINK = "fq-claude-think";
const CL_MODELS = { "claude-opus-5-5": "Opus 5.5", "claude-sonnet-5-5": "Sonnet 5.5", "claude-haiku-5-5": "Haiku 5.5" };
const CL_WRITES = new Set(["kb_add", "kb_update", "kb_archive", "kb_restore", "kb_link", "kb_add_category",
  "kb_rename_category"]);
const CL_SUGS = ["최근 1주일 동안 등록한 질문을 정리해 줘", "비슷한 질문끼리 합칠 것을 찾아 줘", "열어 둔 항목의 답을 더 짧게 고쳐 줘"];
// 목록에서 열어 둔 항목을 함께 보낼 때 사용자 말 앞에 붙이는 한 줄. 말풍선에는 보이지 않는다
const CL_CTX = /^\[열어 둔 항목 #\d+\]\n/;
const cl = { key: null, history: [], think: {}, running: false, abort: null, menu: null, ctxOff: null, draft: "" };

Object.assign(ICONS, {
  search: ["M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14z", "M20 20l-3.5-3.5"],
  doc: ["M7 3h7l5 5v13H7z", "M14 3v5h5"],
  list: ["M9 6h11", "M9 12h11", "M9 18h11", "M4 6h.01", "M4 12h.01", "M4 18h.01"],
  folder: ["M3 6h6l2 2h10v11H3z"],
  plus: ["M12 5v14", "M5 12h14"],
  undo: ["M4 9h11a5 5 0 0 1 0 10H9", "M8 5L4 9l4 4"],
  compose: ["M12 20h9", "M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"],
  more: ["M5 12h.01", "M12 12h.01", "M19 12h.01"],
  up: ["M12 19V5", "M5 12l7-7 7 7"],
  down: ["M12 5v14", "M6 13l6 6 6-6"],
  chev: ["M6 9l6 6 6-6"],
  next: ["M5 12h14", "M13 6l6 6-6 6"],
});

// 도구 줄의 화면 이름(busy 는 진행 중), 아이콘, 인자. MCP 이름(kb_search 등)은 '원본 보기' 안에만 보인다.
// 인자는 모델이 만든 값이라 모양을 믿지 않는다(clArg 가 글자만 받는다)
const clRef = (v) => (Number.isInteger(v) ? `#${v}` : "");
const CL_TOOLS = {
  kb_search: { label: "저장소 검색", busy: "저장소 검색 중", icon: "search",
    arg: (i) => i.query || (Array.isArray(i.queries) ? i.queries.join(", ") : "") || (i.category ? `분류: ${i.category}` : "") },
  kb_get: { label: "항목 읽기", busy: "항목 읽는 중", icon: "doc", arg: (i) => clRef(i.id) },
  kb_categories: { label: "분류 보기", busy: "분류 읽는 중", icon: "folder" },
  kb_add: { label: "새 Q&A 등록", busy: "새 Q&A 등록 중", icon: "plus", arg: (i) => i.category },
  kb_update: { label: "Q&A 수정", busy: "Q&A 수정 중", icon: "pen", arg: (i) => clRef(i.id) },
  kb_archive: { label: "Q&A 삭제", busy: "Q&A 삭제 중", icon: "trash", arg: (i) => clRef(i.id) },
  kb_restore: { label: "Q&A 복원", busy: "Q&A 복원 중", icon: "undo", arg: (i) => clRef(i.id) },
  kb_link: { label: "관련 질문 연결", busy: "관련 질문 잇는 중", icon: "link",
    arg: (i) => [clRef(i.a), clRef(i.b)].filter(Boolean).join(" · ") },
  kb_add_category: { label: "분류 추가", busy: "분류 추가 중", icon: "folder", arg: (i) => (i.parent ? `${i.parent} > ${i.name}` : i.name) },
  kb_rename_category: { label: "분류 이름 변경", busy: "분류 이름 바꾸는 중", icon: "pen", arg: (i) => i.category },
  kb_cleanup_list: { label: "정리 대상 보기", busy: "정리 대상 찾는 중", icon: "list" },
  kb_cleanup_resolve: { label: "정리 완료", busy: "정리 완료 처리 중", icon: "check", arg: (i) => clRef(i.finding_id) },
};

function clLoad() {
  try {
    cl.history = JSON.parse(sessionStorage.getItem(CL_HISTORY) || "[]");
    cl.think = JSON.parse(sessionStorage.getItem(CL_THINK) || "{}");
  } catch (e) {
    cl.history = [];
    cl.think = {};
  }
}

function clSave() {
  try {
    sessionStorage.setItem(CL_HISTORY, JSON.stringify(cl.history));
    sessionStorage.setItem(CL_THINK, JSON.stringify(cl.think));
  } catch (e) {
    // 저장 공간이 모자라도 이 탭에서는 대화가 이어진다
  }
}

// ---------- 아이콘 ----------

function clIcon(name, size, width) {
  const svg = icon(name);
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  if (width) svg.setAttribute("stroke-width", width);
  return svg;
}

function clSvg(size, shapes) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  for (const [k, v] of Object.entries({ width: size, height: size, viewBox: "0 0 24 24", fill: "currentColor", "aria-hidden": "true" })) {
    svg.setAttribute(k, v);
  }
  for (const [tag, attrs] of shapes) {
    const s = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) s.setAttribute(k, v);
    svg.append(s);
  }
  return svg;
}

const clStar = (size) => clSvg(size, [["path", { d: "M12 2.5l1.9 6.1 6.1 1.9-6.1 1.9L12 18.5l-1.9-6.1L4 10.5l6.1-1.9z" }],
  ["path", { d: "M18.5 15l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z" }]]);
const clStop = () => clSvg(11, [["rect", { x: 5, y: 5, width: 14, height: 14, rx: 2 }]]);
const clDots = () => el("span", { class: "cl-dots", role: "img", "aria-label": "진행 중" }, el("i"), el("i"), el("i"));

// ---------- 마크다운(작은 부분집합): 문단, 제목, 목록, 표, 코드, 굵게, 인라인 코드, 링크 ----------

// 같은 서버의 항목 주소(<origin>/#qa/<id>)는 #qa/<id> 로 바꿔 화면을 다시 읽지 않고 그 항목을 연다
function clHref(url) {
  const local = url.startsWith(location.origin + "/#qa/") ? url.slice(location.origin.length + 1) : url;
  if (/^#qa\/\d+$/.test(local)) return { href: local };
  if (/^https?:\/\//.test(url)) return { href: url, target: "_blank", rel: "noopener noreferrer" };
  return null;
}

// 글 속의 항목 참조 #N 은 그 항목을 여는 mono 링크로
function mdRefs(text) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(/(?<![\w#&/])#(\d+)\b/g)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(el("a", { class: "ref", href: `#qa/${m[1]}` }, m[0]));
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function mdInline(text) {
  const out = [];
  const re = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)\s]+)\)/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) out.push(...mdRefs(text.slice(last, m.index)));
    if (m[1] != null) out.push(el("code", {}, m[1]));
    else if (m[2] != null) out.push(el("strong", {}, m[2]));
    else {
      const link = clHref(m[4]);
      out.push(link ? el("a", { ...link, class: /^#\d+$/.test(m[3]) ? "ref" : null }, m[3]) : m[0]);
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(...mdRefs(text.slice(last)));
  return out;
}

// 표 한 줄을 칸으로 나눈다. 인라인 코드(`a | b`) 안과 \| 의 | 는 칸 구분이 아니다
function mdCells(line) {
  const s = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  const cells = [];
  let cur = "";
  let code = false;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (ch === "\\" && s[i + 1] === "|") {
      cur += "|";
      i++;
    } else if (ch === "`") {
      code = !code;
      cur += ch;
    } else if (ch === "|" && !code) {
      cells.push(cur.trim());
      cur = "";
    } else {
      cur += ch;
    }
  }
  cells.push(cur.trim());
  return cells;
}

function mdTable(lines) {
  const cells = mdCells;
  const head = cells(lines[0]);
  return el("div", { class: "cl-table" }, el("table", {},
    el("thead", {}, el("tr", {}, head.map((c) => el("th", {}, mdInline(c))))),
    el("tbody", {}, lines.slice(2).map((l) => el("tr", {}, cells(l).map((c) => el("td", {}, mdInline(c))))))));
}

function mdRender(text) {
  const out = [];
  const parts = text.split(/^```[^\n]*\n?/m);
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      out.push(el("pre", { class: "cl-code" }, el("code", {}, part.replace(/\n$/, ""))));
      return;
    }
    const lines = part.split("\n");
    let para = [];
    let list = null;
    const flushPara = () => {
      if (para.length) out.push(el("p", {}, para.flatMap((l, j) => (j ? [el("br"), ...mdInline(l)] : mdInline(l)))));
      para = [];
    };
    const flushList = () => {
      if (list) out.push(list);
      list = null;
    };
    for (let k = 0; k < lines.length; k++) {
      const line = lines[k];
      const item = line.match(/^\s*(?:[-*]|(\d+)\.)\s+(.*)$/);
      if (line.trim().startsWith("|") && /^\s*\|?\s*:?-{3,}/.test(lines[k + 1] || "")) {
        flushPara();
        flushList();
        const rows = [];
        while (k < lines.length && lines[k].trim().startsWith("|")) rows.push(lines[k++]);
        k--;
        out.push(mdTable(rows));
      } else if (/^#{1,4}\s/.test(line)) {
        flushPara();
        flushList();
        out.push(el("p", { class: "cl-h" }, mdInline(line.replace(/^#+\s/, ""))));
      } else if (item) {
        flushPara();
        const tag = item[1] ? "ol" : "ul";
        if (!list || list.tagName.toLowerCase() !== tag) {
          flushList();
          list = el(tag);
        }
        list.append(el("li", {}, mdInline(item[2])));
      } else if (!line.trim()) {
        flushPara();
        flushList();
      } else {
        flushList();
        para.push(line);
      }
    }
    flushPara();
    flushList();
  });
  return out;
}

// ---------- 도구 줄: 한 줄 + 왼쪽 가는 줄 뒤의 결과 ----------

function clParse(text) {
  try {
    const v = JSON.parse(text);
    return v && typeof v === "object" && !Array.isArray(v) ? v : null;
  } catch (e) {
    return null;
  }
}

const clIsQa = (v) => v && Number.isInteger(v.id) && typeof v.question === "string";

function clHits(list, link = true) {
  return (Array.isArray(list) ? list : []).filter(clIsQa).map((h) => el(link ? "a" : "span",
    { class: "cl-hit", href: link ? `#qa/${h.id}` : null, title: h.question }, el("span", { class: "q" }, h.question),
    el("span", { class: "id" }, `#${h.id}`)));
}

// 수정 결과의 바로 앞 이력이 답을 바꾼 것이고 새 답이 옛 답 뒤에 글을 붙인 모양이면 [옛 답, 더한 글]
function clAdded(item) {
  const rev = Array.isArray(item.revisions) ? item.revisions[0] : null;
  const before = rev && rev.op === "update" && rev.before ? rev.before.answer : null;
  if (typeof before !== "string" || !before || item.answer === before || !item.answer.startsWith(before)) return null;
  return [before, item.answer.slice(before.length)];
}

function clQa(item, diff) {
  const added = diff && typeof item.answer === "string" ? clAdded(item) : null;
  return el("div", { class: "cl-qa" },
    el("div", { class: "l" }, el("span", { class: "t" }, "Q"), el("span", { class: "q" }, item.question)),
    el("div", { class: "l" }, el("span", { class: "t" }, "A"),
      el("span", { class: "a" }, added ? [added[0], el("span", { class: "chg" }, added[1])] : item.answer || "")));
}

// 결과 JSON 을 한 줄 요약(res), 목록에서 열기(go), 결과 줄(body)로. 모양이 다르면 아무것도 돌려주지 않는다
function clSummary(name, data) {
  if (!data) return {};
  if (name === "kb_search" && Array.isArray(data.hits)) {
    return { res: `· ${Number.isInteger(data.total) ? data.total : data.hits.length}건`, body: clHits(data.hits) };
  }
  if (name === "kb_get" && clIsQa(data)) return { body: clHits([data]) };
  if (name === "kb_add" && data.status === "created" && clIsQa(data.item)) return { go: data.item.id, body: [clQa(data.item)] };
  if (name === "kb_add" && data.status === "similar_found" && Array.isArray(data.similar)) {
    return { res: `· 비슷한 질문 ${data.similar.length}건`, body: clHits(data.similar) };
  }
  if (name === "kb_update" && clIsQa(data)) return { go: data.id, body: [clQa(data, true)] };
  if (name === "kb_restore" && clIsQa(data)) return { go: data.id, body: clHits([data]) };
  if (name === "kb_archive" && clIsQa(data)) return { body: clHits([data], false) };
  if (name === "kb_categories" && Array.isArray(data.categories)) return { res: `· 대분류 ${data.categories.length}개` };
  return {};
}

function clArg(t, input) {
  try {
    const v = input && typeof input === "object" && t.arg ? t.arg(input) : "";
    return typeof v === "string" ? v : "";
  } catch (e) {
    return "";
  }
}

function clToolRow(name, input, result) {
  const t = CL_TOOLS[name] || { label: name, busy: `${name} 실행 중`, icon: "doc" };
  const pending = result === undefined;
  const sum = pending || result.is_error ? {} : clSummary(name, clParse(result.text));
  const arg = clArg(t, input);
  const head = el("div", { class: "cl-act" }, clIcon(t.icon, 13), el("b", {}, pending ? t.busy : t.label),
    arg ? el("span", { class: "arg", title: arg }, arg) : null,
    sum.res ? el("span", { class: "res" }, sum.res) : null,
    pending ? clDots() : null,
    sum.go ? el("a", { class: "go", href: `#qa/${sum.go}` }, "목록에서 열기") : null);
  if (pending) return el("div", { class: "cl-tool" }, head);
  return el("div", { class: "cl-tool" }, head, el("div", { class: "cl-act-b" },
    result.is_error ? el("p", { class: "cl-tool-err" }, result.text) : null,
    sum.body || null,
    el("details", { class: "cl-raw" }, el("summary", {}, "원본 보기"),
      el("pre", {}, `${name} ${JSON.stringify(input ?? {})}\n→ ${result.text}`))));
}

// ---------- 대화 그리기 ----------

function clThinking(text, secs) {
  return text ? el("details", { class: "cl-think" }, el("summary", {}, secs ? `생각 · ${secs}초` : "생각"), el("p", {}, text)) : null;
}

function clResultText(content) {
  if (typeof content === "string") return content;
  return (content || []).filter((c) => c.type === "text").map((c) => c.text).join("\n");
}

function clMessages() {
  const results = new Map();
  for (const m of cl.history) {
    if (m.role === "user" && Array.isArray(m.content)) {
      for (const b of m.content) {
        if (b.type === "tool_result") results.set(b.tool_use_id, { text: clResultText(b.content), is_error: !!b.is_error });
      }
    }
  }
  const out = [];
  cl.history.forEach((m, i) => {
    if (m.role === "user") {
      const text = typeof m.content === "string" ? m.content
        : m.content.filter((b) => b.type === "text").map((b) => b.text).join("\n");
      if (text) out.push(el("div", { class: "cl-user" }, text.replace(CL_CTX, "")));
      return;
    }
    const turn = el("div", { class: "cl-turn" });
    m.content.forEach((b, j) => {
      if (b.type === "thinking") turn.append(...[clThinking(b.thinking, cl.think[`${i}:${j}`])].filter(Boolean));
      else if (b.type === "text") turn.append(el("div", { class: "cl-md" }, mdRender(b.text)));
      else if (b.type === "tool_use") turn.append(clToolRow(b.name, b.input, results.get(b.id)));
      else if (b.type === "fallback") turn.append(el("p", { class: "cl-note" }, `${CL_MODELS[b.to?.model] || b.to?.model || "다른 모델"} 가 이어서 답했습니다.`));
    });
    out.push(turn);
  });
  return out;
}

function clEmpty() {
  return el("div", { class: "cl-empty" }, el("span", { class: "mark" }, clStar(20)),
    el("h3", {}, "무엇을 도와드릴까요?"),
    el("p", {}, "저장소를 찾아보고 정리합니다. 등록하거나 고친 내용은 목록에 바로 보입니다."),
    el("div", { class: "cl-sugs" }, CL_SUGS.map((t) => el("button", { class: "cl-sug", type: "button", onclick: () => clPick(t) },
      t, el("span", { class: "k" }, clIcon("next", 13))))));
}

// 제안을 누르면 입력칸에 넣기만 한다(바로 보내지 않는다)
function clPick(text) {
  const input = $("#cl-input");
  input.value = text;
  clGrow();
  input.focus();
}

const clAtEnd = (log) => log.scrollHeight - log.scrollTop - log.clientHeight <= 40;

function clToEnd() {
  const log = $("#cl-log");
  if (log) log.scrollTop = log.scrollHeight;
  if ($("#cl-latest")) $("#cl-latest").hidden = true;
}

// 새 글이 올 때: 대화 끝 가까이(40px)에 있었으면 끝으로 따라가고, 위로 올려 읽는 중이면 그대로 두고 '최신 메시지로' 를 보인다
function clFollow(change, force) {
  const log = $("#cl-log");
  const stick = force || !log || clAtEnd(log);
  change();
  if (!log) return;
  if (stick) clToEnd();
  else if (log.scrollHeight > log.clientHeight) $("#cl-latest").hidden = false;
}

function clRenderLog(live, force) {
  const log = $("#cl-log");
  if (!log) return;
  const items = clMessages();
  if (!items.length && !live) items.push(clEmpty());
  clFollow(() => fill(log, items, live), force);
}

// ---------- 키 등록 ----------

function clKeyForm(stale, replacing) {
  const input = el("input", { class: "fld", type: "password", "aria-label": "Anthropic API 키", placeholder: "sk-ant-...",
    autocomplete: "off", spellcheck: "false" });
  const err = el("p", { class: "cl-err", role: "alert", hidden: true });
  const btn = el("button", { class: "btn primary", type: "submit" }, "등록");
  const form = el("form", { class: "cl-key", onsubmit: async (ev) => {
    ev.preventDefault();
    btn.disabled = true;
    try {
      await api("PUT", "/api/claude/key", { key: input.value.trim(), model: cl.key?.model });
      input.value = "";
      await clOpen();
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    } finally {
      btn.disabled = false;
    }
  } });
  form.append(
    el("span", { class: "mark" }, clStar(20)),
    el("h3", {}, "Claude 를 이 저장소에 연결"),
    el("p", { class: "desc" }, "Claude Console 에서 만든 API 키를 등록하면 이 패널에서 Claude 가 저장소를 찾아보고 정리합니다."),
    stale ? el("p", { class: "cl-err", role: "alert" }, "저장된 키를 더 쓸 수 없습니다. 다시 등록해 주세요.") : null,
    el("div", { class: "cl-key-row" }, input, btn, replacing ? el("button", { class: "btn", type: "button", onclick: clOpen }, "취소") : null),
    err,
    el("div", { class: "notes" }, el("span", {}, "키는 서버에 암호화해 보관합니다."),
      el("span", {}, "대화와 저장소 검색 결과가 이 키로 Anthropic 에 전달되고, 요금은 키의 계정에 청구됩니다.")),
    el("a", { class: "cl-keylink", href: "https://console.anthropic.com/settings/keys", target: "_blank",
      rel: "noopener noreferrer" }, "API 키 만들기 ↗"));
  return form;
}

function clKeyView(stale, replacing) {
  fill($("#claude"), clHead(), el("div", { class: "cl-scroll" }, el("div", { class: "cl-log" }, clKeyForm(stale, replacing))));
  $("#claude input").focus();
}

// ---------- 보내기 ----------

function clLive() {
  const turn = el("div", { class: "cl-turn live" });
  const tools = new Map();
  const secs = []; // 생각 블록마다 걸린 초. 끝나면 기록의 생각 블록에 순서대로 붙인다
  let think = null;
  let text = null;
  let textBuf = "";
  let wait = clDots();
  turn.append(wait);
  const endThink = () => {
    if (!think) return;
    const s = Math.max(1, Math.round((Date.now() - think.t0) / 1000));
    secs.push(s);
    think.summary.textContent = `생각 · ${s}초`;
    think.box.open = false;
    think = null;
  };
  return {
    node: turn,
    secs,
    end: endThink,
    on(ev) {
      clFollow(() => {
        wait?.remove();
        wait = null;
        if (ev.type === "thinking") {
          if (!think) {
            const summary = el("summary", {}, "생각 중");
            const p = el("p");
            const box = el("details", { class: "cl-think", open: true }, summary, p);
            turn.append(box);
            think = { summary, p, box, t0: Date.now() };
          }
          think.p.textContent += ev.text;
          return;
        }
        endThink();
        if (ev.type === "text") {
          if (!text) {
            text = el("div", { class: "cl-md" });
            textBuf = "";
            turn.append(text);
          }
          textBuf += ev.text;
          fill(text, mdRender(textBuf));
        } else if (ev.type === "tool_start") {
          text = null;
          const row = clToolRow(ev.name, null, undefined);
          tools.set(ev.id, row);
          turn.append(row);
        } else if (ev.type === "tool_result") {
          const row = clToolRow(ev.name, ev.input, ev);
          tools.get(ev.id)?.replaceWith(row);
          tools.set(ev.id, row);
          text = null;
          wait = clDots(); // 다음 응답을 기다리는 동안
          turn.append(wait);
        } else if (ev.type === "notice") {
          turn.append(el("p", { class: "cl-note" }, ev.text));
        }
      });
    },
  };
}

// 이번 응답의 생각 블록(글이 있는 것)에 브라우저가 잰 초를 순서대로 붙인다. 개수가 다르면 붙이지 않는다('생각' 만 보인다)
function clKeepThink(start, appended, secs) {
  const keys = [];
  appended.forEach((m, a) => {
    if (m.role === "assistant") m.content.forEach((b, j) => b.type === "thinking" && b.thinking && keys.push(`${start + a}:${j}`));
  });
  if (keys.length === secs.length) keys.forEach((k, n) => { cl.think[k] = secs[n]; });
}

async function clSend(text) {
  if (cl.running || !text.trim()) return;
  const input = $("#cl-input");
  const ctx = clCtxId();
  const prompt = (ctx ? `[열어 둔 항목 #${ctx}]\n` : "") + text;
  const before = cl.history.length;
  cl.history.push({ role: "user", content: prompt });
  input.value = "";
  clGrow();
  const live = clLive();
  clRenderLog(live.node, true);
  clBusy(true);
  cl.abort = new AbortController();
  clStatus("응답을 기다리는 중입니다.");
  let finished = false;
  let wrote = false;
  // 실패한 요청은 기록에 남기지 않고 보낸 말을 입력칸에 되돌린다. 응답을 기다리는 동안 새로 쓴 글이 있으면 그 앞에 둔다
  const restore = () => {
    cl.history.length = before;
    const box = $("#cl-input");
    if (box) {
      box.value = box.value.trim() ? `${text}\n${box.value}` : text;
      clGrow();
    } else {
      cl.draft = text;
    }
  };
  const fail = (msg) => {
    restore();
    clStatus("");
    clRenderLog(el("p", { class: "cl-err", role: "alert" }, msg), true);
  };
  try {
    const r = await fetch("/api/claude/chat", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: cl.history }), signal: cl.abort.signal });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      if (data.code === "no_key") {
        restore();
        cl.draft = text;
        return clOpen();
      }
      return fail(r.status === 413 ? "대화가 길어 더 보낼 수 없습니다. 새 대화를 시작해 주세요."
        : (data.error || `요청을 처리하지 못했습니다(HTTP ${r.status}).`));
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i);
        buf = buf.slice(i + 2);
        if (!chunk.startsWith("data: ")) continue;
        const ev = JSON.parse(chunk.slice(6));
        if (ev.type === "done") {
          live.end();
          clKeepThink(cl.history.length, ev.append, live.secs);
          cl.history.push(...ev.append);
          wrote = ev.append.some((m) => m.role === "assistant" && m.content.some((b) => b.type === "tool_use" && CL_WRITES.has(b.name)));
          finished = true;
        } else if (ev.type === "error") {
          finished = true;
          fail(ev.text);
        } else {
          // 멈춤으로 done 을 못 받아도 이미 실행한 쓰기는 목록에 반영한다
          if (ev.type === "tool_result" && !ev.is_error && CL_WRITES.has(ev.name)) wrote = true;
          try {
            live.on(ev);
          } catch (err) {
            // 그리기 실패(예상 밖 모양의 도구 결과)로 대화를 끊지 않는다. 끝나면 기록으로 다시 그린다
          }
        }
      }
    }
    if (!finished) fail("응답이 도중에 끊겼습니다. 다시 보내 주세요.");
    else if (cl.history.length > before + 1) {
      clSave();
      clStatus("답이 왔습니다.");
      clRenderLog();
    }
  } catch (e) {
    if (e.name !== "AbortError") cl.abort?.abort(); // 읽다가 실패하면 서버의 대화 반복도 멈춘다
    if (!finished) {
      fail(e.name === "AbortError" ? "멈췄습니다. 이미 실행한 변경은 그대로 남습니다." : "서버에 연결하지 못했습니다.");
    } else if (cl.history.length > before + 1) {
      // done 을 받은 뒤 끊겼으면 끝난 대화로 둔다
      clSave();
      clRenderLog();
    }
  } finally {
    cl.abort = null;
    clBusy(false);
    if (wrote) {
      refreshCats();
      runSearch();
    }
  }
}

// 스크린리더용 상태 한 줄. 대화 띠 전체를 live region 으로 두면 조각마다 다시 읽힌다
function clStatus(text) {
  const s = $("#cl-status");
  if (s) s.textContent = text;
}

function clBusy(on) {
  cl.running = on;
  const send = $("#cl-send");
  if (!send) return;
  send.classList.toggle("stop", on);
  send.setAttribute("aria-label", on ? "멈춤" : "보내기");
  send.title = on ? "멈춤" : "보내기 (Enter)";
  fill(send, on ? clStop() : clIcon("up", 15, 2.4));
  for (const b of $("#claude").querySelectorAll("[data-busy-off]")) b.disabled = on;
  // 응답 중에도 입력칸은 쓸 수 있다(다음 말을 미리 쓴다). 끝나면 포커스가 패널 안이나 빈 곳이었을 때만 입력칸으로 돌린다
  const a = document.activeElement;
  if (!on && (!a || a === document.body || $("#claude").contains(a))) $("#cl-input").focus();
}

// ---------- 입력 띠 ----------

// textarea 는 2~6줄로 자란다(줄 22px + 위아래 2px)
function clGrow() {
  const ta = $("#cl-input");
  if (!ta) return;
  ta.style.height = "auto";
  ta.style.height = `${Math.min(Math.max(ta.scrollHeight, 48), 136)}px`;
}

// 목록에서 펼쳐 둔 항목. 칩의 닫기 버튼으로 뺀 항목은 다른 항목을 펼칠 때까지 붙이지 않는다
function clCtxId() {
  const id = typeof state !== "undefined" ? state.sel : null;
  return id && id !== cl.ctxOff ? id : null;
}

function clCtx() {
  const slot = $("#cl-ctx");
  if (!slot) return;
  const id = clCtxId();
  fill(slot, id
    ? el("span", { class: "cl-ctx", title: "목록에서 열어 둔 항목을 함께 보냅니다" },
      el("span", {}, el("span", { class: "mono" }, `#${id}`), " 함께 보냄"),
      el("button", { type: "button", "aria-label": "함께 보내지 않기", onclick: () => {
        cl.ctxOff = id;
        clCtx();
        $("#cl-input").focus();
      } }, clIcon("cross", 12)))
    : el("span", { class: "cl-hint" }, "Enter 보내기 · Shift+Enter 줄바꿈"));
}

function clComposer() {
  const input = el("textarea", { id: "cl-input", rows: 2, "aria-label": "Claude 에게 보낼 말", placeholder: "무엇을 할까요?",
    value: cl.draft });
  cl.draft = ""; // 키를 다시 등록하느라 입력 띠가 사라졌던 동안 보관한 말
  input.addEventListener("input", clGrow);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      clSend(input.value);
    }
  });
  const send = el("button", { id: "cl-send", class: "cl-send", type: "button", onclick: () => {
    if (cl.running) cl.abort?.abort();
    else clSend(input.value);
  } });
  return el("div", { class: "cl-compose" }, input, el("div", { class: "cl-row" }, el("span", { id: "cl-ctx", class: "cl-ctx-slot" }), send));
}

async function clDeleteKey() {
  if (!window.confirm("등록한 Anthropic API 키를 지웁니다. 이 패널을 다시 쓰려면 키를 새로 등록해야 합니다.\n지우시겠습니까?")) return;
  try {
    await api("DELETE", "/api/claude/key");
  } catch (e) {
    toast(e.message, { err: true });
    return;
  }
  await clOpen();
}

// ---------- 머리 띠와 메뉴(모델, 키 관리) ----------

function clMenu(kind) {
  cl.menu = kind && cl.menu !== kind ? kind : null;
  $("#cl-menu")?.remove();
  for (const b of $("#claude").querySelectorAll("[data-menu]")) {
    const on = b.dataset.menu === cl.menu;
    b.setAttribute("aria-expanded", String(on));
    b.classList.toggle("on", on);
  }
  if (!cl.menu) return;
  const menu = cl.menu === "key" ? el("div", { id: "cl-menu", class: "cl-menu", role: "menu", "aria-label": "키 관리" },
    el("div", { class: "row" }, el("span", {}, "등록한 키"), el("span", { class: "mono" }, `sk-ant-…${cl.key.last4}`)),
    el("button", { type: "button", role: "menuitem", onclick: () => {
      clMenu(null);
      clKeyView(false, true);
    } }, "키 바꾸기"),
    el("button", { type: "button", role: "menuitem", class: "danger", onclick: () => {
      clMenu(null);
      clDeleteKey();
    } }, "키 삭제"))
    : el("div", { id: "cl-menu", class: "cl-menu model", role: "menu", "aria-label": "모델" },
      (cl.key.models || Object.keys(CL_MODELS)).map((m) => el("button", { type: "button", role: "menuitemradio",
        "aria-checked": String(m === cl.key.model), onclick: () => clSetModel(m) },
      CL_MODELS[m] || m, m === cl.key.model ? clIcon("check", 14) : null)));
  // 머리 띠 안에 넣어 Tab 순서가 여는 버튼 바로 뒤로 이어지게 한다. 위치는 패널 기준(absolute)이라 그대로다
  menu.addEventListener("keydown", (e) => {
    const items = [...menu.querySelectorAll("button")];
    const i = items.indexOf(document.activeElement);
    const go = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: items.length - 1 }[e.key];
    if (go !== undefined) {
      e.preventDefault();
      items[(go + items.length) % items.length].focus();
    } else if (e.key === "Tab") {
      clMenu(null);
    }
  });
  menu.addEventListener("focusout", (e) => {
    if (cl.menu && !menu.contains(e.relatedTarget) && !e.relatedTarget?.closest?.("[data-menu]")) clMenu(null);
  });
  $("#claude .cl-head").append(menu);
  menu.querySelector("button").focus();
}

async function clSetModel(model) {
  clMenu(null);
  if (model === cl.key.model) return;
  try {
    cl.key = { ...cl.key, ...(await api("PUT", "/api/claude/key", { model })) };
  } catch (err) {
    toast(err.message, { err: true });
    return;
  }
  $("#claude .cl-head").replaceWith(clHead());
}

function clHead() {
  const set = cl.key?.set;
  const name = set ? CL_MODELS[cl.key.model] || cl.key.model : "";
  return el("div", { class: "cl-head" },
    el("span", { class: "cl-title" }, clStar(15), "Claude"),
    set ? el("button", { class: "cl-model", type: "button", "data-menu": "model", "aria-haspopup": "menu", "aria-expanded": "false",
      "aria-label": `모델: ${name}`, title: "모델 바꾸기", onclick: () => clMenu("model") }, name, clIcon("chev", 12)) : null,
    el("div", { class: "grow" }),
    set ? el("button", { class: "cl-ib", type: "button", "aria-label": "새 대화", title: "새 대화", "data-busy-off": "",
      disabled: cl.running, onclick: () => {
        if (cl.running) return;
        cl.history = [];
        cl.think = {};
        clSave();
        clRenderLog(null, true);
        $("#cl-input")?.focus();
      } }, clIcon("compose", 16)) : null,
    set ? el("button", { class: "cl-ib", type: "button", "data-menu": "key", "aria-haspopup": "menu", "aria-expanded": "false",
      "aria-label": "키 관리", title: "키 관리", "data-busy-off": "", disabled: cl.running, onclick: () => clMenu("key") },
    clIcon("more", 16, 3)) : null,
    el("button", { class: "cl-ib", type: "button", "aria-label": "Claude 패널 닫기", title: "Claude 패널 닫기",
      onclick: () => clToggle(false) }, clIcon("cross", 16)));
}

// ---------- 패널 ----------

async function clOpen() {
  const panel = $("#claude");
  cl.menu = null;
  try {
    cl.key = await api("GET", "/api/claude/key");
  } catch (e) {
    toast(e.message, { err: true });
    return;
  }
  if (!cl.key.set) return clKeyView(cl.key.stale, false);
  const log = el("div", { id: "cl-log", class: "cl-log" });
  log.addEventListener("scroll", () => {
    if (clAtEnd(log)) $("#cl-latest").hidden = true;
  });
  const latest = el("button", { id: "cl-latest", class: "cl-latest", type: "button", hidden: true, onclick: clToEnd },
    clIcon("down", 13), "최신 메시지로");
  fill(panel, clHead(), el("div", { class: "cl-scroll" }, log, latest), clComposer(),
    el("span", { id: "cl-status", class: "sr-only", role: "status" }));
  clCtx();
  clBusy(cl.running);
  clRenderLog(null, true);
  $("#cl-input").focus();
}

function clToggle(open) {
  const panel = $("#claude");
  const app = $("#app");
  if (cl.running && !open) cl.abort?.abort();
  // 패널을 열고 닫으면 스크롤하는 곳이 페이지와 목록 칸(#app) 사이에서 바뀐다. 보던 위치를 옮겨 준다
  const was = document.body.classList.contains("claude-open");
  const y = was ? app.scrollTop : Math.max(0, window.scrollY - app.offsetTop);
  const focusIn = panel.contains(document.activeElement) || document.activeElement === document.body;
  panel.hidden = !open;
  document.body.classList.toggle("claude-open", open);
  if (open && !was) app.scrollTop = y;
  else if (!open && was) document.scrollingElement.scrollTop = app.offsetTop + y; // window.scrollTo 는 app.js 의 scrollTo(id) 가 가린다
  $("#claude-btn").classList.toggle("on", open);
  $("#claude-btn").setAttribute("aria-expanded", String(open));
  store("fq-claude-open", open ? "1" : "0");
  if (open) clOpen();
  else if (focusIn) $("#claude-btn").focus({ preventScroll: true }); // 패널 안에 있던 포커스를 여는 버튼으로 돌린다(보던 위치는 그대로)
}

clLoad();
$("#claude-btn").addEventListener("click", () => clToggle($("#claude").hidden));
// 좁은 화면에서는 패널이 목록을 덮으므로 항목 링크(#qa/<id>)를 누르면 패널을 닫아 그 항목을 보인다
$("#claude").addEventListener("click", (e) => {
  if (e.target.closest('a[href^="#qa/"]') && window.matchMedia("(max-width: 900px)").matches) clToggle(false);
});
// 패널 안의 키는 목록 단축키(Esc 지우기, 화살표 이동 등)로 넘기지 않는다. Esc 는 열린 메뉴를 닫는다
$("#claude").addEventListener("keydown", (e) => {
  e.stopPropagation();
  if (e.key === "Escape" && cl.menu) {
    const opener = $(`#claude [data-menu="${cl.menu}"]`);
    clMenu(null);
    opener?.focus();
  }
});
document.addEventListener("click", (e) => {
  if (cl.menu && !e.target.closest("#cl-menu, [data-menu]")) clMenu(null);
});
document.addEventListener("fq-select", () => {
  if (state.sel !== cl.ctxOff) cl.ctxOff = null;
  clCtx();
});
// 계정 표시가 켜질 때(원격 모드 로그인) 아이콘을 보인다. 지난번에 열어 둔 채였으면 다시 연다
new MutationObserver(() => {
  const on = !$("#account").hidden;
  $("#claude-btn").hidden = !on;
  if (on && store("fq-claude-open") === "1" && $("#claude").hidden) clToggle(true);
}).observe($("#account"), { attributes: true, attributeFilter: ["hidden"] });
