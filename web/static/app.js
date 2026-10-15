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

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(url, opt);
  const data = await r.json().catch(() => ({}));
  // 원격 모드에서 세션이 끝났으면 로그인 화면으로 바꾼다
  if (r.status === 401 && data.code === "unauthorized") showLogin(data.providers || []);
  if (!r.ok) {
    const err = new Error(data.error || `HTTP ${r.status}`);
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

const KIND = { file: "FILE", dir: "DIR", git: "GIT", githead: "HEAD", url: "URL", note: "메모" };
const OP = { insert: "생성", update: "수정", archive: "삭제", restore: "복원" };
const FIELD = { question: "질문", answer: "답변", category_id: "분류", evidence_state: "근거 상태" };

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
    facet("전체", total, state.cat === null, () => setCat(null), "top"),
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
        "aria-label": `${c.path} Markdown 내보내기`, title: "Markdown 내보내기" }, icon("download")),
      ibtn("rn", "pen", `${c.path} 이름 변경`, "이름 변경", () => { state.catRenaming = c.id; renderTree(); }),
      ibtn("del", "trash", `${c.path} 삭제`, "삭제", () => deleteCategory(c)));
  };
  fill($("#cat-tree"), state.cats.flatMap((t) => [line(t, "top"), ...t.children.map((c) => line(c, "child"))]));
}

// 이름과 설명을 함께 고친다. 바뀐 값만 보낸다
function renameForm(c, cls) {
  const input = el("input", { class: "fld", value: c.name, "aria-label": `${c.path} 새 이름`, autocomplete: "off" });
  const desc = el("input", {
    class: "fld desc", value: c.description || "", "aria-label": `${c.path} 설명`, placeholder: "설명 (선택)",
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
        toast(body.name ? `분류 이름을 "${done.path}" 로 바꿨다.` : `분류 "${done.path}" 의 설명을 바꿨다.`);
      } catch (err) {
        toast(err.message, { err: true });
      }
    },
    onkeydown: (e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cancel(); } },
  }, input,
  ibtn("ok", "check", "저장", "저장 (Enter)", null, "submit"),
  ibtn("no", "cross", "취소", "취소 (Esc)", cancel), desc);
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
  if (impact.children) parts.push(`하위 분류 ${impact.children}개`);
  if (impact.qa) parts.push(`지식 ${impact.qa}건`);
  const warn = parts.length ? `\n${parts.join(", ")}도 모두 함께 삭제됩니다.` : "";
  if (!window.confirm(`분류 "${c.path}" 를 삭제한다.${warn}\n삭제를 하시겠습니까?`)) return;
  try {
    const res = await api("POST", `/api/categories/${c.id}/archive`, { version: impact.category.version });
    const gone = new Set([c.id, ...(c.children || []).map((x) => x.id)]);
    if (gone.has(state.cat)) state.cat = null;
    await refreshCats();
    runSearch();
    toast(`분류 "${c.path}" 를 삭제했다.` + (res.archived_qa ? ` 지식 ${res.archived_qa}건도 함께 삭제했다.` : ""));
  } catch (err) {
    toast(err.message, { err: true });
    refreshCats();
  }
}

function setCatEdit(on) {
  state.catEdit = on;
  state.catRenaming = null;
  const b = $("#cat-edit");
  b.textContent = on ? "편집 종료" : "분류 편집";
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
}

// 요약 줄의 "최근 24시간 N건" 칩. 켜져 있지 않고 0건이면 숨긴다
function renderSince(n) {
  const chip = $("#since-chip");
  chip.hidden = !state.since && !n;
  chip.textContent = `최근 24시간 ${n}건`;
  chip.classList.toggle("on", state.since);
  chip.setAttribute("aria-pressed", String(state.since));
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
  if (parts.length > 2) return toast("분류는 대분류 > 중분류 2단계까지다.", { err: true });
  const body = parts.length === 2 ? { name: parts[1], parent: parts[0] } : { name: parts[0] };
  try {
    const c = await api("POST", "/api/categories", body);
    $("#cat-name").value = "";
    $("#cat-form").hidden = true;
    $("#cat-add").hidden = false;
    await refreshCats();
    toast(`분류 "${c.path}" 를 만들었다.`);
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
    res = await api("GET", "/api/search?" + searchParams({ relax: 1 }));
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
    const g = groupEl(key, similar ? "비슷한 결과" : hits[0].category_path, counts.get(key) || hits.length);
    g.classList.remove("out");
    box.append(g);
    const container = g.querySelector(".grp-rows");
    for (const h of hits) container.append(rowEl(h));
    markLast(g);
  }

  summarize(res, similar, order.length);
  const empty = !res.hits.length;
  state.empty = empty;
  $("#empty").hidden = !empty || Boolean($("#new-slot").firstChild);
  if (empty) {
    $("#empty-msg").textContent = res.query
      ? (similar ? "일치하는 질문이 없습니다. 비슷한 결과를 아래에 보입니다." : "일치하는 질문이 없습니다.")
      : state.since ? `최근 ${SINCE_HOURS}시간 안에 등록한 질문이 없습니다.` : "아직 등록된 질문이 없습니다.";
    $("#empty-new").textContent = res.query ? `“${res.query}” 질문으로 새로 등록` : "+ 새 질문";
  }
  // 단어를 하나 빼면 결과가 있는 질의. 누르면 그 질의로 검색한다
  fill($("#relaxed"), empty ? (res.relaxed || []).map((r) => el("button", {
    class: "chip", type: "button", title: `"${r.query}" 로 검색`, onclick: () => searchFor(r.query),
  }, el("code", {}, r.dropped), ` 빼고 ${r.total}건`)) : null);
  if (state.sel && rows.has(state.sel) && !state.details.has(state.sel)) loadDetail(state.sel);
}

function summarize(res, similar, nGroups) {
  const count = $("#sum-count");
  if (similar) count.replaceChildren(el("b", {}, "0"), `건 · 비슷한 결과 ${res.similar.length}건`);
  else if (res.query) count.replaceChildren(el("b", {}, res.total), `건 · ${res.groups.length}개 분류`);
  else count.replaceChildren("최근 수정 ", el("b", {}, res.hits.length), `건 · ${nGroups}개 분류`);
  const notes = [];
  if (res.query && res.morph === "off") notes.push("형태소 검색 꺼짐");
  if (res.total > res.hits.length && res.query) notes.push(`위 ${res.hits.length}건만 표시`);
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
  g.firstChild.title = shut ? "펴기" : "접기";
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
  const t = kst(h.updated_at);
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
    el("span", { class: "date", title: t.full }, t.md));
}

// ---------- 펼침 ----------

function toggle(id, force) {
  const open = force ?? state.sel !== id;
  const prev = state.sel;
  state.sel = open ? id : null;
  if (prev && prev !== id) repaint(prev);
  repaint(id);
  setHash(open ? id : null);
  if (open) {
    if (state.q) remember(state.q);
    loadDetail(id);
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
  return el("a", { class: "evlink", href: vscodeUrl(path), title: `VS Code 로 열기: ${path}`, onclick: (e) => e.stopPropagation() }, ...kids);
}

// 파일 경로는 폴더와 파일 이름으로 나눈다. 폴더는 앞쪽을 줄이고 파일 이름은 남긴다
function splitPath(p) {
  const i = Math.max(p.lastIndexOf("\\"), p.lastIndexOf("/"));
  return i < 0 ? ["", p] : [p.slice(0, i + 1), p.slice(i + 1)];
}

function evidenceText(d) {
  if (!d.evidence.length) return el("span", { class: "none" }, "없음");
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
    const t = kst(r.at);
    let op = OP[r.op] || r.op;
    if (r.op === "update" && r.before && r.after) {
      const changed = Object.keys(FIELD).filter((k) => r.before[k] !== r.after[k]).map((k) => FIELD[k]);
      if (changed.length) op += `(${changed.join("·")})`;
    }
    return `${t.md} ${t.hm} ${r.actor} ${op}`;
  });
}

// 이력은 최근 한 건과 +N 버튼. 누르면 전부(최근 순)와 접기
function histLine(id, d) {
  const items = histItems(d);
  const open = state.histOpen.has(id);
  const more = items.length > 1
    ? el("button", {
      class: "more", type: "button", "aria-expanded": String(open),
      title: open ? "최근 한 건만 보기" : `이력 ${items.length}건 모두 보기`,
      onclick: () => {
        if (open) state.histOpen.delete(id);
        else state.histOpen.add(id);
        repaint(id);
      },
    }, open ? "접기" : `+${items.length - 1}`)
    : null;
  return el("span", { class: "val hist" + (open ? " wrap" : "") },
    el("span", { class: open ? "" : "ell" }, open ? items.join("  ·  ") : items[0] || ""), more);
}

function detailEl(id) {
  const d = state.details.get(id);
  const label = (s) => el("span", { class: "label" }, s);
  if (!d) return el("div", { class: "detail" }, el("div", { class: "dline" }, label(""), el("span", { class: "val hist" }, "읽는 중...")));
  const ev = dline("근거", evidenceText(d), "ev");
  if (d.evidence.length) ev.title = evidenceTitle(d);
  return el("div", { class: "detail" },
    el("div", { class: "dline" }, label("근거"), ev),
    d.links.length
      ? el("div", { class: "dline" }, label("연결"),
        dline("연결", d.links.map((l) => el("a", { href: `#qa/${l.id}`, class: "ell", title: l.question }, "→ " + l.question)), "links"))
      : null,
    el("div", { class: "dline" }, label("이력"), histLine(id, d),
      ibtn("cp", "copy", "복사", "복사", () => copy('Q:\t'+d.question+'\r\nA:\t'+ d.answer, "Q&A를 복사했다.")),
      ibtn("cp", "link", "주소 복사", "주소 복사", () => copy(`${location.origin}/#qa/${id}`, "주소를 복사했다.")),
      el("button", { class: "btn small", type: "button", title: "편집 (E)", onclick: () => startEdit(id) }, "편집")));
}

async function copy(text, done) {
  try {
    await navigator.clipboard.writeText(text);
    toast(done);
  } catch (e) {
    toast(`복사하지 못했다: ${e.message}`, { err: true });
  }
}

// ---------- 편집 ----------

function counter(input, max, lines) {
  const c = el("span", { class: "count", "aria-live": "polite" });
  const update = () => {
    const v = input.value;
    const n = Array.from(v.trim()).length;
    const ln = v.split("\n").filter((s) => s.trim()).length;
    c.textContent = `${n}/${max}`;
    c.title = lines ? `${ln}/${lines}줄` : "";
    c.classList.toggle("over", n > max || Boolean(lines && ln > lines));
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
  const s = el("select", { class: "fld", "aria-label": "분류" });
  for (const c of state.flat) s.append(el("option", { value: c.id }, c.path));
  if (selected != null && !state.flat.some((c) => c.id === selected)) s.append(el("option", { value: selected }, `(삭제된 분류 ${selected})`));
  if (selected != null) s.value = String(selected);
  return s;
}

function editForm(base, isNew, onSaved, onCancel) {
  const fq = el("input", { class: "fld fq", "aria-label": "질문", value: base.question, autocomplete: "off" });
  const fa = el("textarea", { class: "fld fa", "aria-label": "답변", rows: 2 });
  fa.value = base.answer;
  fa.addEventListener("input", () => grow(fa));
  setTimeout(() => grow(fa), 0);
  const fc = catSelect(base.category_id);
  const fe = el("input", {
    class: "fld fe", "aria-label": "근거 (선택)", value: base.evidence_line || "", autocomplete: "off", spellcheck: "false",
    placeholder: "근거 (선택): 경로 #앵커 · URL · 메모: 누가 확인",
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
        return onSaved(r.item, "등록했다.");
      }
      const body = { version: base.version };
      if (fq.value !== base.question) body.question = fq.value;
      if (fa.value !== base.answer) body.answer = fa.value;
      if (Number(fc.value) !== base.category_id) body.category = Number(fc.value);
      if (fe.value.trim() !== (base.evidence_line || "")) body.evidence_line = fe.value;
      if (Object.keys(body).length === 1) return onCancel();
      onSaved(await api("PATCH", `/api/qa/${base.id}`, body), "저장했다.");
    } catch (e) {
      if (e.status === 409) {
        await loadDetail(base.id);
        const fresh = state.details.get(base.id);
        if (fresh) {
          form.replaceWith(editForm(fresh, false, onSaved, onCancel));
          return toast("다른 곳에서 먼저 고쳤다. 지금 값으로 다시 열었다.", { err: true });
        }
      }
      msg.textContent = e.message;
    } finally {
      busy(false);
    }
  }

  function showSimilar(list) {
    fill(sim,
      el("div", { class: "head" }, "비슷한 질문이 이미 있다. 같은 내용이면 그 항목을 고친다."),
      list.map((h) => el("div", {},
        el("a", { href: `#qa/${h.id}` }, h.question),
        el("span", { class: "sub" }, `${h.category_path} · ${Math.round(h.score * 100)}%`))),
      el("div", {}, el("button", { class: "btn", type: "button", onclick: () => submit(true) }, "다른 질문이다. 그래도 등록")));
  }

  // 삭제는 행을 지우지 않고 보관 표시만 한다(되돌리기·MCP kb_restore 로 복원된다)
  async function remove() {
    if (!window.confirm("삭제를 하시겠습니까?")) return;
    try {
      const it = await api("POST", `/api/qa/${base.id}/archive`, { version: base.version });
      onSaved(it, null);
      toast("삭제했다.", {
        action: {
          label: "되돌리기",
          fn: async () => {
            try {
              await api("POST", `/api/qa/${it.id}/restore`, { version: it.version });
              toast("되돌렸다.");
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
  el("div", { class: "eline" }, el("span", { class: "tag-q" }, "Q"), fq, counter(fq, 150)),
  el("div", { class: "eline multi" }, el("span", { class: "tag-a" }, "A"), fa, counter(fa, 400, 6)),
  el("div", { class: "erow" }, fc, fe,
    el("button", { class: "btn primary", type: "submit", title: `${isNew ? "등록" : "저장"} (Ctrl+Enter)` }, isNew ? "등록" : "저장"),
    el("button", { class: "btn", type: "button", title: "취소 (Esc)", onclick: () => onCancel() }, "취소"),
    isNew ? null : el("span", { class: "spacer" }),
    isNew ? null : ibtn("del", "trash", "이 질문 삭제", "삭제", remove)),
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
    toast("분류가 아직 없다. 왼쪽 패널에서 + 분류 추가로 먼저 만든다.", { err: true });
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
    state.cat = null;
    state.pin = it.id;
    state.sel = it.id;
    state.details.set(it.id, it);
    setHash(it.id);
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
  state.cat = state.flat.some((c) => c.id === cat) ? cat : null;
  state.since = p.has("since");
  renderTree();
  renderChip();
}

// 화면 밖이면 그 행이 보이게 스크롤한다. 위는 붙는 묶음 머리 높이(44px), 아래는 16px 를 비운다
function scrollTo(id) {
  const r = rows.get(id);
  if (!r) return;
  const b = r.getBoundingClientRect();
  const top = 44;
  const bottom = window.innerHeight - 16;
  if (b.top < top) window.scrollBy({ top: b.top - top, behavior: "smooth" });
  else if (b.bottom > bottom) window.scrollBy({ top: Math.min(b.bottom - bottom, b.top - top), behavior: "smooth" });
}

async function openFromHash() {
  const m = location.hash.match(/^#qa\/(\d+)$/);
  if (!m) return;
  const id = Number(m[1]);
  state.pin = id;
  if (!rows.has(id) || rows.get(id).classList.contains("out")) {
    state.q = "";
    $("#q").value = "";
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
    toast(`#${id} 를 찾지 못했다(삭제했거나 없는 번호다).`, { err: true });
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
  toggle(next, true);
  // 펼친 내용이 그려진 뒤의 높이로 스크롤 위치를 정한다
  requestAnimationFrame(() => scrollTo(next));
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
  state.q = query.trim();
  state.pin = null;
  q.focus();
  runSearch();
}

function clearSearch() {
  const q = $("#q");
  q.value = "";
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
function setTheme(t, save) {
  document.documentElement.setAttribute("data-theme", t);
  if (save) store("fq-theme", t);
  $("#theme-btn").setAttribute("aria-label", t === "dark" ? "라이트 테마로 바꾸기" : "다크 테마로 바꾸기");
}

// ---------- 시작 ----------

// ---------- 원격 모드 로그인 ----------
const PROVIDERS = { github: "GitHub", google: "Google" };
const LOGIN_ERRORS = {
  denied: "로그인을 취소했다.",
  state: "로그인 시간이 지났거나 다른 창에서 시작했다. 다시 로그인한다.",
  provider: "로그인 공급자의 응답을 확인하지 못했다. 다시 시도한다.",
  closed: "이 계정은 로그인할 수 없다.",
  limit: "이 주소에서 한 시간 안에 만든 계정이 너무 많다. 한 시간 뒤 다시 시도한다.",
};

function showLogin(providers) {
  if (!$("#login").hidden) return;
  const p = new URLSearchParams(location.search);
  const code = p.get("login_error");
  p.delete("login_error");
  const rest = p.toString();
  if (code) history.replaceState(null, "", location.pathname + (rest ? `?${rest}` : "") + location.hash);
  // 로그인 뒤 지금 주소(검색어·펼친 항목)로 돌아온다
  const next = encodeURIComponent(location.pathname + location.search + location.hash);
  fill($("#login-btns"), providers.map((name) =>
    el("a", { class: "btn login-btn", href: `/auth/login/${name}?next=${next}` }, `${PROVIDERS[name] || name} 로 로그인`)));
  if (!providers.length) fill($("#login-btns"), el("p", { class: "login-note" }, "켜진 로그인 공급자가 없다."));
  $("#login-err").hidden = !code;
  $("#login-err").textContent = LOGIN_ERRORS[code] || "";
  for (const id of ["#app", "#toggle-side", "#new-btn", "#account"]) $(id).hidden = true;
  $(".keys").hidden = true;
  $("#login").hidden = false;
}

function showAccount(account) {
  const fallback = account.providers.map((n) => `${PROVIDERS[n] || n} 계정`).join(", ");
  $("#account-name").textContent = account.display_name || fallback;
  $("#account-name").title = `${fallback}으로 로그인`;
  $("#account").hidden = false;
}

async function logout() {
  try {
    await api("POST", "/auth/logout");
  } catch (e) {
    toast(e.message, { err: true });
    return;
  }
  location.reload();
}

// ---------- 원격 모드 연결: 개인 토큰과 설치 명령 ----------
// 상자와 복사 내용은 명령 한 줄뿐이다(붙여 넣어 바로 실행되게). 안내는 상자 밖 문장으로 둔다.
// Git Bash 는 \ 를 이스케이프로 읽어 / 로 쓴다. install.py 는 Windows 가상환경(.venv\Scripts)을 전제로 한다
const INSTALL_SHELLS = [
  { name: "PowerShell", cmd: (url) => `.venv\\Scripts\\python.exe scripts\\install.py --remote ${url}` },
  { name: "cmd", cmd: (url) => `.venv\\Scripts\\python.exe scripts\\install.py --remote ${url}` },
  { name: "Git Bash", cmd: (url) => `.venv/Scripts/python.exe scripts/install.py --remote ${url}` },
];

function installShell() {
  return INSTALL_SHELLS.find((s) => s.name === store("fq-shell")) || INSTALL_SHELLS[0];
}

function installBlock() {
  const shell = installShell();
  const command = shell.cmd(location.origin);
  const tabs = el("div", { class: "shell-tabs", role: "group", "aria-label": "셸" },
    INSTALL_SHELLS.map((s) => el("button", { class: "chip" + (s === shell ? " on" : ""), type: "button",
      "aria-pressed": String(s === shell), onclick: () => {
        store("fq-shell", s.name);
        $(".connect-cmd").replaceWith(installBlock());
      } }, s.name)));
  return el("div", { class: "connect-cmd" }, el("span", { class: "label" }, "설치 명령"), tabs,
    el("p", { class: "connect-note" }, "fast-question 저장소 폴더에서 실행한다. 토큰을 물으면 위에서 만든 값을 붙여 넣는다."),
    el("pre", { class: "mono" }, command),
    ibtn("cp", "copy", "설치 명령 복사", "설치 명령 복사", () => copy(command, `${shell.name} 설치 명령을 복사했다.`)));
}

function openConnect(open) {
  $("#connect").hidden = !open;
  $("#connect-btn").classList.toggle("on", open);
  $("#connect-btn").setAttribute("aria-expanded", String(open));
  if (open) renderConnect();
}

async function revokeToken(t) {
  if (!window.confirm(`토큰 "${t.name}" 을 폐기한다. 이 토큰을 쓰는 PC 는 더 연결되지 않는다.\n폐기를 하시겠습니까?`)) return;
  try {
    await api("DELETE", `/api/account/tokens/${t.id}`);
  } catch (e) {
    toast(e.message, { err: true });
    return;
  }
  toast("토큰을 폐기했다.");
  renderConnect();
}

// made: 방금 만든 토큰. 토큰 값은 이때 한 번만 보인다
async function renderConnect(made) {
  let tokens = [];
  try {
    tokens = (await api("GET", "/api/account/tokens")).tokens;
  } catch (e) {
    toast(e.message, { err: true });
  }
  const name = el("input", { class: "fld", "aria-label": "토큰 이름", placeholder: "PC 이름 (예: 회사 노트북)",
    maxlength: 60, autocomplete: "off" });
  const form = el("form", { class: "connect-form", onsubmit: async (ev) => {
    ev.preventDefault();
    try {
      renderConnect(await api("POST", "/api/account/tokens", { name: name.value }));
    } catch (e) {
      toast(e.message, { err: true });
    }
  } }, name, el("button", { class: "btn primary", type: "submit" }, "토큰 만들기"));
  const day = (iso) => (iso || "").slice(0, 10);
  fill($("#connect"),
    el("div", { class: "connect-head" }, el("span", { class: "label" }, "연결"),
      el("button", { class: "btn small", type: "button", onclick: () => openConnect(false) }, "닫기")),
    el("p", { class: "connect-note" },
      "이 PC 의 Claude Code 와 즉석 질문 훅을 이 서버에 붙인다. 토큰은 PC 마다 하나씩 만든다."),
    made ? el("div", { class: "token-made", role: "status" },
      el("p", {}, `토큰 "${made.name}" 을 만들었다. 이 값은 지금만 보인다.`),
      el("div", { class: "token-row" }, el("code", { class: "mono token-value" }, made.token),
        ibtn("cp", "copy", "토큰 복사", "토큰 복사", () => copy(made.token, "토큰을 복사했다.")))) : null,
    form,
    tokens.length
      ? el("ul", { class: "token-list" }, tokens.map((t) => el("li", { class: "token-item" + (t.expired ? " expired" : "") },
        el("span", { class: "name" }, t.name),
        el("span", { class: "meta mono", title: `만든 날 ${t.created_at}` },
          t.expired ? "만료" : `만료 ${day(t.expires_at)}`, t.last_used_at ? ` · 마지막 사용 ${day(t.last_used_at)}` : " · 아직 안 씀"),
        ibtn("del", "trash", `${t.name} 토큰 폐기`, "폐기", () => revokeToken(t)))))
      : el("p", { class: "connect-note" }, "아직 만든 토큰이 없다."),
    installBlock(),
    el("div", { class: "connect-export" }, el("span", { class: "label" }, "내보내기"),
      el("p", { class: "connect-note" }, "내 DB 전체를 SQLite 파일 하나로 받는다. 로컬 모드 config.json 의 db_path 로 그대로 열린다."),
      el("a", { class: "btn", href: "/api/export/db", download: "" }, "DB 파일 받기")),
    leaveForm());
  if (made) name.value = "";
}

// 탈퇴: 확인 문구를 입력해야 버튼이 동작한다
function leaveForm() {
  const word = el("input", { class: "fld", "aria-label": "탈퇴 확인 문구", placeholder: "탈퇴", autocomplete: "off" });
  return el("form", { class: "connect-leave", onsubmit: async (ev) => {
    ev.preventDefault();
    try {
      await api("POST", "/api/account/delete", { confirm: word.value.trim() });
    } catch (e) {
      toast(e.message, { err: true });
      return;
    }
    location.href = "/";
  } },
  el("span", { class: "label" }, "탈퇴"),
  el("p", { class: "connect-note" }, "계정을 지운다. 이 계정의 로그인·토큰이 모두 끊기고 DB 를 더 열 수 없다. 먼저 DB 파일을 받아 둔다. 계속하려면 탈퇴 를 입력한다."),
  el("div", { class: "connect-form" }, word, el("button", { class: "btn danger", type: "submit" }, "탈퇴")));
}

// 로컬 모드는 그대로 시작한다. 원격 모드는 로그인했을 때만 true
async function whoami() {
  try {
    const me = await api("GET", "/api/me");
    if (me.account) showAccount(me.account);
    return true;
  } catch (e) {
    return e.status !== 401;  // 그 밖의 실패는 화면을 띄우고 각 요청의 오류로 보인다
  }
}

function init() {
  const q = $("#q");
  // 검색어를 입력하면 펼친 행과 편집을 닫는다
  q.addEventListener("input", () => {
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
      if (ids.length && !ids.includes(state.sel)) {
        toggle(ids[0], true);
        requestAnimationFrame(() => scrollTo(ids[0]));
      }
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
