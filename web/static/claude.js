"use strict";
// 웹 Claude 패널(원격 모드). 서버(web/claude_chat.py)가 사용자 API 키로 대화 반복을 돌리고 진행을 SSE 로 보낸다.
// app.js 의 $, el, fill, icon, api, toast, store, refreshCats, runSearch 를 쓴다(app.js 뒤에 읽힌다).
// 대화 기록(API 형식 메시지)은 sessionStorage 에만 두고 매번 서버에 보낸다. 생각·대체 모델 블록은 받은 그대로 돌려보낸다.
// Claude 의 글은 mdRender() 로만 그린다(HTML 로 해석하지 않는다).

const CL_HISTORY = "fq-claude-history";
const CL_MODELS = { "claude-opus-5-5": "Opus 5.5", "claude-sonnet-5-5": "Sonnet 5.5", "claude-haiku-5-5": "Haiku 5.5" };
const CL_WRITES = new Set(["kb_add", "kb_update", "kb_archive", "kb_restore", "kb_link", "kb_add_category",
  "kb_rename_category"]);
const cl = { key: null, history: [], running: false, abort: null };

function clLoad() {
  try {
    cl.history = JSON.parse(sessionStorage.getItem(CL_HISTORY) || "[]");
  } catch (e) {
    cl.history = [];
  }
}

function clSave() {
  try {
    sessionStorage.setItem(CL_HISTORY, JSON.stringify(cl.history));
  } catch (e) {
    // 저장 공간이 모자라도 이 탭에서는 대화가 이어진다
  }
}

// ---------- 마크다운(작은 부분집합): 문단, 제목, 목록, 표, 코드, 굵게, 인라인 코드, 링크 ----------

// 같은 서버의 항목 주소(<origin>/#qa/<id>)는 #qa/<id> 로 바꿔 화면을 다시 읽지 않고 그 항목을 연다
function clHref(url) {
  const local = url.startsWith(location.origin + "/#qa/") ? url.slice(location.origin.length + 1) : url;
  if (/^#qa\/\d+$/.test(local)) return { href: local };
  if (/^https?:\/\//.test(url)) return { href: url, target: "_blank", rel: "noopener noreferrer" };
  return null;
}

function mdInline(text) {
  const out = [];
  const re = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)\s]+)\)/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1] != null) out.push(el("code", {}, m[1]));
    else if (m[2] != null) out.push(el("strong", {}, m[2]));
    else {
      const link = clHref(m[4]);
      out.push(link ? el("a", link, m[3]) : m[0]);
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
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

// ---------- 대화 그리기 ----------

function clToolRow(name, input, result) {
  const pending = result === undefined;
  const body = el("div", { class: "cl-tool-body" },
    el("span", { class: "cl-tool-label" }, "요청"),
    el("pre", { class: "cl-code" }, JSON.stringify(input ?? {}, null, 2)),
    pending ? null : el("span", { class: "cl-tool-label" }, result.is_error ? "오류" : "응답"),
    pending ? null : el("pre", { class: "cl-code" + (result.is_error ? " err" : "") }, result.text));
  return el("details", { class: "cl-tool" + (pending ? " pending" : "") + (result?.is_error ? " err" : "") },
    el("summary", {}, `fast-question: ${name} ${pending ? "사용 중" : "사용함"}`), body);
}

function clThinking(text) {
  return text ? el("details", { class: "cl-think" }, el("summary", {}, "생각"), el("p", {}, text)) : null;
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
  for (const m of cl.history) {
    if (m.role === "user") {
      const text = typeof m.content === "string" ? m.content
        : m.content.filter((b) => b.type === "text").map((b) => b.text).join("\n");
      if (text) out.push(el("div", { class: "cl-user" }, text));
      continue;
    }
    const turn = el("div", { class: "cl-turn" });
    for (const b of m.content) {
      if (b.type === "thinking") turn.append(...[clThinking(b.thinking)].filter(Boolean));
      else if (b.type === "text") turn.append(el("div", { class: "cl-md" }, mdRender(b.text)));
      else if (b.type === "tool_use") turn.append(clToolRow(b.name, b.input, results.get(b.id)));
      else if (b.type === "fallback") turn.append(el("p", { class: "cl-note" }, `${b.to?.model || "다른 모델"} 가 이어서 답했습니다.`));
    }
    out.push(turn);
  }
  return out;
}

function clScroll() {
  const log = $("#cl-log");
  if (log) log.scrollTop = log.scrollHeight;
}

function clRenderLog(live) {
  const log = $("#cl-log");
  if (!log) return;
  const items = clMessages();
  if (!items.length && !live) {
    items.push(el("div", { class: "cl-empty" },
      el("p", {}, "이 DB 에 대해 물어보거나 일을 맡기세요."),
      el("p", { class: "cl-note" }, "예) 지난주에 등록한 배포 관련 질문 찾아 줘 / 이 답변을 더 짧게 고쳐 줘")));
  }
  fill(log, items, live);
  clScroll();
}

// ---------- 키 등록 ----------

function clKeyForm(stale) {
  const input = el("input", { class: "fld", type: "password", "aria-label": "Anthropic API 키", placeholder: "sk-ant-...",
    autocomplete: "off", spellcheck: "false" });
  const err = el("p", { class: "cl-err", role: "alert", hidden: true });
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
  const btn = el("button", { class: "btn primary", type: "submit" }, "등록");
  form.append(
    el("p", {}, "Claude Console 에서 만든 API 키를 등록하면 이 패널에서 Claude 가 이 DB 를 찾고 고칩니다."),
    stale ? el("p", { class: "cl-err" }, "저장된 키를 더 쓸 수 없습니다. 다시 등록해 주세요.") : null,
    el("div", { class: "cn-row" }, input, btn), err,
    el("p", { class: "cl-note" }, "키는 서버에 암호화해 보관합니다. 대화 내용과 이 DB 의 검색 결과가 그 키로 Anthropic 에 전달되고, 요금은 그 키의 계정에 청구됩니다."),
    el("p", { class: "cl-note" }, el("a", { href: "https://console.anthropic.com/settings/keys", target: "_blank",
      rel: "noopener noreferrer" }, "API 키 만들기")));
  return form;
}

// ---------- 보내기 ----------

function clLive() {
  const turn = el("div", { class: "cl-turn live" });
  const tools = new Map();
  let think = null;
  let text = null;
  let textBuf = "";
  return {
    node: turn,
    on(ev) {
      if (ev.type === "thinking") {
        if (!think) {
          think = el("p");
          turn.append(el("details", { class: "cl-think", open: true }, el("summary", {}, "생각 중"), think));
        }
        think.textContent += ev.text;
      } else if (ev.type === "text") {
        if (!text) {
          text = el("div", { class: "cl-md" });
          textBuf = "";
          turn.append(text);
        }
        textBuf += ev.text;
        fill(text, mdRender(textBuf));
      } else if (ev.type === "tool_start") {
        think = null;
        text = null;
        const row = clToolRow(ev.name, null, undefined);
        tools.set(ev.id, row);
        turn.append(row);
      } else if (ev.type === "tool_result") {
        const row = clToolRow(ev.name, ev.input, ev);
        tools.get(ev.id)?.replaceWith(row);
        tools.set(ev.id, row);
        think = null;
        text = null;
      } else if (ev.type === "notice") {
        turn.append(el("p", { class: "cl-note" }, ev.text));
      }
      clScroll();
    },
  };
}

async function clSend(prompt) {
  if (cl.running || !prompt.trim()) return;
  const input = $("#cl-input");
  const before = cl.history.length;
  cl.history.push({ role: "user", content: prompt });
  input.value = "";
  const live = clLive();
  clRenderLog(live.node);
  clBusy(true);
  cl.abort = new AbortController();
  let finished = false;
  let wrote = false;
  const fail = (msg) => {
    cl.history.length = before; // 실패한 요청은 기록에 남기지 않는다. 입력은 되돌린다
    input.value = prompt;
    clRenderLog(el("p", { class: "cl-err", role: "alert" }, msg));
  };
  try {
    const r = await fetch("/api/claude/chat", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: cl.history }), signal: cl.abort.signal });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      if (data.code === "no_key") return clOpen();
      return fail(r.status === 413 ? "대화가 길어 더 보낼 수 없습니다. 새 대화를 시작해 주세요." : (data.error || `HTTP ${r.status}`));
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
          cl.history.push(...ev.append);
          wrote = ev.append.some((m) => m.role === "assistant" && m.content.some((b) => b.type === "tool_use" && CL_WRITES.has(b.name)));
          finished = true;
        } else if (ev.type === "error") {
          finished = true;
          fail(ev.text);
        } else {
          live.on(ev);
        }
      }
    }
    if (!finished) fail("응답이 도중에 끊겼습니다. 다시 보내 주세요.");
    else if (cl.history.length > before + 1) {
      clSave();
      clRenderLog();
    }
  } catch (e) {
    fail(e.name === "AbortError" ? "멈췄습니다. 이미 실행한 변경은 그대로 남습니다." : "서버에 연결하지 못했습니다.");
  } finally {
    cl.abort = null;
    clBusy(false);
    if (wrote) {
      refreshCats();
      runSearch();
    }
  }
}

function clBusy(on) {
  cl.running = on;
  const send = $("#cl-send");
  if (!send) return;
  send.textContent = on ? "멈춤" : "보내기";
  send.classList.toggle("primary", !on);
  $("#cl-input").disabled = on;
}

// ---------- 패널 ----------

function clComposer() {
  const input = el("textarea", { id: "cl-input", class: "fld", rows: 3, "aria-label": "Claude 에게 보낼 말",
    placeholder: "무엇을 할까요?  Enter 보내기 · Shift+Enter 줄바꿈" });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      clSend(input.value);
    }
    if (e.key === "Escape") e.stopPropagation(); // 검색창의 Esc 동작으로 넘기지 않는다
  });
  const send = el("button", { id: "cl-send", class: "btn primary", type: "button", onclick: () => {
    if (cl.running) cl.abort?.abort();
    else clSend(input.value);
  } }, "보내기");
  return el("div", { class: "cl-compose" }, input, el("div", { class: "cl-compose-row" },
    el("span", { class: "cl-note" }, `키 …${cl.key.last4}`),
    el("button", { class: "btn quiet small", type: "button", onclick: clDeleteKey }, "키 삭제"),
    el("div", { class: "grow" }), send));
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

function clHead() {
  const model = cl.key?.set ? el("select", { class: "fld cl-model", "aria-label": "모델", onchange: async (e) => {
    try {
      cl.key = { ...cl.key, ...(await api("PUT", "/api/claude/key", { model: e.target.value })) };
    } catch (err) {
      toast(err.message, { err: true });
    }
  } }, (cl.key.models || Object.keys(CL_MODELS)).map((m) =>
    el("option", { value: m, selected: m === cl.key.model }, CL_MODELS[m] || m))) : null;
  return el("div", { class: "cl-head" }, el("span", { class: "cl-title" }, "Claude"), model, el("div", { class: "grow" }),
    cl.key?.set ? el("button", { class: "navbtn small", type: "button", disabled: cl.running, onclick: () => {
      if (cl.running) return;
      cl.history = [];
      clSave();
      clRenderLog();
    } }, "새 대화") : null,
    el("button", { class: "navbtn small", type: "button", onclick: () => clToggle(false) }, "닫기"));
}

async function clOpen() {
  const panel = $("#claude");
  try {
    cl.key = await api("GET", "/api/claude/key");
  } catch (e) {
    toast(e.message, { err: true });
    return;
  }
  if (!cl.key.set) {
    fill(panel, clHead(), el("div", { class: "cl-body" }, clKeyForm(cl.key.stale)));
    return;
  }
  fill(panel, clHead(), el("div", { id: "cl-log", class: "cl-log", "aria-live": "polite" }), clComposer());
  clRenderLog();
  $("#cl-input").focus();
}

function clToggle(open) {
  const panel = $("#claude");
  if (cl.running && !open) cl.abort?.abort();
  panel.hidden = !open;
  document.body.classList.toggle("claude-open", open);
  $("#claude-btn").classList.toggle("on", open);
  $("#claude-btn").setAttribute("aria-expanded", String(open));
  store("fq-claude-open", open ? "1" : "0");
  if (open) clOpen();
}

clLoad();
$("#claude-btn").addEventListener("click", () => clToggle($("#claude").hidden));
// 좁은 화면에서는 패널이 목록을 덮으므로 항목 링크(#qa/<id>)를 누르면 패널을 닫아 그 항목을 보인다
$("#claude").addEventListener("click", (e) => {
  if (e.target.closest('a[href^="#qa/"]') && window.matchMedia("(max-width: 900px)").matches) clToggle(false);
});
// 계정 표시가 켜질 때(원격 모드 로그인) 아이콘을 보인다. 지난번에 열어 둔 채였으면 다시 연다
new MutationObserver(() => {
  const on = !$("#account").hidden;
  $("#claude-btn").hidden = !on;
  if (on && store("fq-claude-open") === "1" && $("#claude").hidden) clToggle(true);
}).observe($("#account"), { attributes: true, attributeFilter: ["hidden"] });
