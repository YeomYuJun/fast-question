"use strict";
// fast-question 화면. DOM 은 el() 로만 만든다(사용자 문자열을 HTML 로 해석하지 않는다).

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];

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

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(url, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(data.error || `HTTP ${r.status}`);
    err.status = r.status;
    err.data = data;
    throw err;
  }
  return data;
}

let toastTimer = null;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast on" + (err ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), err ? 6000 : 2500);
}

const KST = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul", year: "2-digit", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
});
const when = (s) => (s ? KST.format(new Date(s)) : "-");

const TIER = { exact: "정확히 일치", synonym: "동의어", morph: "형태소", similar: "비슷함", recent: "최근" };
const EVID = { none: "근거 없음", external: "외부 근거", trusted: "근거 같음", stale: "재확인 필요", unknown: "판정 불가" };
const OP = { insert: "생성", update: "수정", archive: "보관", restore: "복원" };
const ACTOR = { user: "사용자", claude: "Claude", system: "시스템" };
const KIND = { file: "파일", dir: "폴더", git: "git 경로", githead: "git HEAD", url: "URL", note: "확인 메모" };
const VERDICT = { same: "같음", changed: "바뀜", unknown: "판정 불가", external: "기계 판정 안 함" };
const TABLE = { qa: "Q&A", category: "분류", evidence: "근거", qa_link: "관련 질문", synonym: "동의어", user_word: "사용자 단어", stopword: "불용어" };

function marked(text, spans) {
  const f = document.createDocumentFragment();
  let pos = 0;
  for (const [s, e] of spans || []) {
    if (s > pos) f.append(text.slice(pos, s));
    f.append(el("mark", {}, text.slice(s, e)));
    pos = e;
  }
  f.append(text.slice(pos));
  return f;
}

const state = { opts: { case: false, regex: false }, hits: [], sel: -1, current: null, cats: [], flatCats: [] };

// ---------- 탭 ----------

function showTab(name) {
  $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("on", t.id === "tab-" + name));
  if (name === "cats") loadCats();
  if (name === "dict") loadDict();
  if (name === "watch") loadWatch();
  if (name === "new") $("#new-q").focus();
  if (name === "search") $("#q").focus();
}

// ---------- 분류 ----------

async function refreshCats() {
  const { categories } = await api("GET", "/api/categories");
  state.cats = categories;
  state.flatCats = categories.flatMap((t) => [t, ...t.children]);
  fillSelect($("#f-cat"), state.flatCats, "전체 분류");
  fillSelect($("#new-cat"), state.flatCats, null);
  fillSelect($("#cat-parent"), categories, "(대분류로 만들기)");
}

function fillSelect(sel, cats, emptyLabel) {
  const keep = sel.value;
  sel.replaceChildren();
  if (emptyLabel) sel.append(el("option", { value: "" }, emptyLabel));
  for (const c of cats) sel.append(el("option", { value: c.id }, `${c.path} (${c.count})`));
  if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
}

// ---------- 검색 ----------

let searchTimer = null;
let searchSeq = 0;
function scheduleSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 150);
}

async function runSearch() {
  const my = ++searchSeq;
  const p = new URLSearchParams({ q: $("#q").value });
  if ($("#f-cat").value) p.set("category", $("#f-cat").value);
  if ($("#f-only").value) p.set("only", $("#f-only").value);
  if ($("#f-archived").checked) p.set("archived", "1");
  if (state.opts.case) p.set("case", "1");
  if (state.opts.regex) p.set("regex", "1");
  try {
    const res = await api("GET", "/api/search?" + p);
    if (my === searchSeq) renderResults(res); // 늦게 도착한 이전 응답은 버린다
  } catch (e) {
    if (my !== searchSeq) return;
    $("#summary").textContent = e.message;
    $("#results").replaceChildren();
  }
}

function badges(h) {
  return [
    h.tier && h.tier !== "recent" ? el("span", { class: "badge " + h.tier }, TIER[h.tier] + (h.score ? ` ${Math.round(h.score * 100)}%` : "")) : null,
    el("span", { class: "badge " + h.evidence_state }, EVID[h.evidence_state]),
    h.reviewed ? null : el("span", { class: "badge unreviewed" }, "미검토"),
    h.archived ? el("span", { class: "badge archived" }, "보관") : null,
    el("span", {}, `#${h.id} · ${when(h.updated_at)} ${ACTOR[h.updated_by] || h.updated_by}`),
  ];
}

function hitRow(h) {
  return el("div", { class: "hit", "data-id": h.id, onclick: () => select(state.hits.findIndex((x) => x.id === h.id)) },
    el("div", { class: "q" }, marked(h.question, h.spans && h.spans.question)),
    el("div", { class: "a" }, marked(h.answer, h.spans && h.spans.answer)),
    el("div", { class: "meta" }, badges(h)));
}

function group(path, count, hits) {
  const caret = el("span", { class: "caret" }, "▾");
  const g = el("div", { class: "group" });
  const head = el("div", {
    class: "ghead",
    onclick: () => { g.classList.toggle("closed"); caret.textContent = g.classList.contains("closed") ? "▸" : "▾"; },
  }, caret, path, el("span", { class: "count" }, count));
  g.append(head, el("div", { class: "hits" }, hits.map(hitRow)));
  return g;
}

function renderResults(res) {
  const box = $("#results");
  box.replaceChildren();
  const parts = [res.query ? `${res.total}건 / ${res.groups.length}개 분류` : `최근 수정 ${res.hits.length}건`];
  parts.push(`${res.elapsed_ms} ms`);
  if (res.query) parts.push(res.morph === "on" ? "형태소 검색 켜짐" : "형태소 검색 꺼짐");
  if (res.total > res.hits.length) parts.push(`위 ${res.hits.length}건만 표시`);
  $("#summary").textContent = parts.join(" · ");

  state.hits = [];
  const byCat = new Map();
  for (const h of res.hits) {
    if (!byCat.has(h.category_id)) byCat.set(h.category_id, []);
    byCat.get(h.category_id).push(h);
  }
  const order = res.groups.length
    ? res.groups
    : [...byCat.keys()].map((id) => ({ category_id: id, category_path: byCat.get(id)[0].category_path, count: byCat.get(id).length }));
  for (const g of order) {
    const hits = byCat.get(g.category_id) || [];
    if (!hits.length) continue;
    state.hits.push(...hits);
    box.append(group(g.category_path, g.count, hits));
  }
  if (res.query && !res.hits.length) {
    box.append(el("p", { class: "muted", style: "padding: 8px 10px" },
      "일치하는 항목이 없다." + (res.similar.length ? " 비슷한 결과:" : " 표현을 바꿔 다시 검색해 본다.")));
    if (res.similar.length) {
      state.hits.push(...res.similar);
      box.append(group("비슷한 결과", res.similar.length, res.similar));
    }
  }
  state.sel = -1;
  if (state.current) {
    const i = state.hits.findIndex((h) => h.id === state.current.id);
    if (i >= 0) markSel(i);
  }
}

function markSel(i) {
  state.sel = i;
  const id = (state.hits[i] || {}).id;
  $$(".hit").forEach((n) => n.classList.toggle("sel", Number(n.dataset.id) === id));
  const n = $(`.hit[data-id="${id}"]`);
  if (n) n.scrollIntoView({ block: "nearest" });
}

function select(i) {
  if (i < 0 || i >= state.hits.length) return;
  markSel(i);
  openDetail(state.hits[i].id);
}

// ---------- 상세 ----------

async function openDetail(id) {
  try {
    const item = await api("GET", `/api/qa/${id}`);
    state.current = item;
    renderDetail(item);
    history.replaceState(null, "", `#qa/${id}`);
  } catch (e) {
    toast(e.message, true);
  }
}

function counter(input, max, lines) {
  const c = el("span", { class: "counter" });
  const update = () => {
    const v = input.value;
    const n = [...v.trim()].length;
    const ln = v.split("\n").filter((s) => s.trim()).length;
    c.textContent = lines ? `${n}/${max}자 · ${ln}/${lines}줄` : `${n}/${max}자`;
    c.classList.toggle("over", Boolean(n > max || (lines && ln > lines))); // undefined 를 넘기면 매번 뒤집힌다
  };
  input.addEventListener("input", update);
  update();
  return c;
}

function catSelect(selected) {
  const s = el("select");
  for (const c of state.flatCats) s.append(el("option", { value: c.id }, c.path));
  if (![...s.options].some((o) => Number(o.value) === selected)) s.append(el("option", { value: selected }, `(보관된 분류 ${selected})`));
  s.value = selected;
  return s;
}

function afterChange(item) {
  if (item && item.id) {
    state.current = item;
    renderDetail(item);
  }
  runSearch();
  refreshStatus();
}

async function act(fn, okMsg) {
  try {
    const r = await fn();
    if (okMsg) toast(okMsg);
    return r;
  } catch (e) {
    if (e.status === 409) {
      toast("다른 곳에서 먼저 고쳤다. 현재 값을 다시 읽었다.", true);
      if (state.current) openDetail(state.current.id);
    } else toast(e.message, true);
    return null;
  }
}

function renderDetail(it) {
  const d = $("#detail");
  d.replaceChildren();
  const qIn = el("input", { value: it.question });
  const aIn = el("textarea", { rows: 3 });
  aIn.value = it.answer;
  const cat = catSelect(it.category_id);
  const note = el("input", { placeholder: "수정 사유 (선택, 이력에 남는다)" });

  const save = async () => {
    const body = { version: it.version, note: note.value || null };
    if (qIn.value !== it.question) body.question = qIn.value;
    if (aIn.value !== it.answer) body.answer = aIn.value;
    if (Number(cat.value) !== it.category_id) body.category = Number(cat.value);
    if (Object.keys(body).length === 2) return toast("바뀐 값이 없다.");
    afterChange(await act(() => api("PATCH", `/api/qa/${it.id}`, body), "저장했다."));
  };
  const post = (path, body, msg) => async () => afterChange(await act(() => api("POST", `/api/qa/${it.id}${path}`, body), msg));

  d.append(
    el("h3", {}, `${it.category_path} · #${it.id}`),
    el("div", { class: "meta row" }, badges(it).slice(1, 4)),
    el("div", { class: "muted", style: "font-size:12px" },
      `생성 ${when(it.created_at)} ${ACTOR[it.created_by]} · 수정 ${when(it.updated_at)} ${ACTOR[it.updated_by]} · 버전 ${it.version}`
      + (it.reviewed ? ` · 검토 ${when(it.reviewed_at)} ${ACTOR[it.reviewed_by] || ""}` : "")),
    el("div", { class: "field" }, el("span", {}, "질문 ", counter(qIn, 150)), qIn),
    el("div", { class: "field" }, el("span", {}, "답변 ", counter(aIn, 400, 2)), aIn),
    el("div", { class: "field" }, el("span", {}, "분류"), cat),
    el("div", { class: "field" }, note),
    el("div", { class: "row" },
      el("button", { class: "primary", onclick: save }, "저장"),
      it.reviewed ? null : el("button", { onclick: post("/review", undefined, "검토 완료로 표시했다.") }, "검토 완료"),
      it.archived
        ? el("button", { onclick: post("/restore", { version: it.version }, "복원했다.") }, "복원")
        : el("button", { onclick: post("/archive", { version: it.version, note: note.value || null }, "보관했다.") }, "보관"),
    ),
    evidenceSection(it),
    linkSection(it),
    revisionSection(it),
  );
}

function evidenceSection(it) {
  const list = el("div");
  for (const e of it.evidence) {
    const target = e.kind === "note" ? e.text : e.kind === "githead" ? e.repo : e.path || (e.paths || []).join(", ");
    list.append(el("div", { class: "item" },
      el("div", { class: "row" },
        el("span", { class: "badge" }, KIND[e.kind] || e.kind),
        el("span", { class: "badge " + (e.verdict === "changed" ? "stale" : e.verdict === "same" ? "trusted" : e.verdict) }, VERDICT[e.verdict] || e.verdict),
        el("button", { class: "link", title: "근거를 다시 읽고 지금 상태로 기록한다", onclick: async () => afterChange(await act(() => api("POST", `/api/evidence/${e.id}/recapture`), "근거를 다시 기록했다.")) }, "재기록"),
        el("button", { class: "link", onclick: async () => afterChange(await act(() => api("POST", `/api/evidence/${e.id}/archive`), "근거를 보관했다.")) }, "보관"),
      ),
      el("div", { class: "path" }, target || ""),
      e.anchor ? el("div", { class: "muted" }, `기준점: ${e.anchor}`) : null,
      e.verdict_detail ? el("div", { class: "muted" }, e.verdict_detail) : null,
      e.commit ? el("div", { class: "muted" }, `커밋 ${e.commit.slice(0, 10)}`) : null,
    ));
  }
  const kind = el("select", {}, el("option", { value: "auto" }, "파일·폴더 경로"), el("option", { value: "url" }, "URL"), el("option", { value: "note" }, "사람 확인 메모"));
  const value = el("input", { placeholder: "경로 / URL / 누가 어떻게 확인" });
  const anchor = el("input", { placeholder: "기준점 (선택)" });
  const add = async () => {
    if (!value.value.trim()) return toast("값을 넣는다.", true);
    afterChange(await act(() => api("POST", `/api/qa/${it.id}/evidence`, evidenceSpec(kind.value, value.value, anchor.value)), "근거를 추가했다."));
  };
  return el("section", {},
    el("div", { class: "row" }, el("h2", { style: "margin:0" }, `근거 ${it.evidence.length}건`),
      el("button", { class: "link", onclick: async () => {
        const r = await act(() => api("POST", "/api/recheck", { qa_id: it.id }));
        if (r) { toast(`판정 ${r.results.length}건, 바뀐 판정 ${r.results.filter((x) => x.changed).length}건`); openDetail(it.id); runSearch(); }
      } }, "재검증")),
    list,
    el("div", { class: "row" }, kind, value, anchor, el("button", { onclick: add }, "추가")));
}

function evidenceSpec(kind, value, anchor) {
  value = value.trim();
  if (kind === "url") return { kind, url: value };
  if (kind === "note") return { kind, text: value };
  return { kind: "auto", path: value, anchor: anchor.trim() || null };
}

function linkSection(it) {
  const other = el("input", { placeholder: "연결할 Q&A 번호", style: "width:140px" });
  return el("section", {},
    el("h2", {}, `관련 질문 ${it.links.length}건`),
    it.links.map((l) => el("div", { class: "item row" },
      el("button", { class: "link", onclick: () => openDetail(l.id) }, `#${l.id} ${l.question}`),
      el("span", { class: "muted" }, l.category_path),
      el("button", { class: "link", onclick: async () => afterChange(await act(() => api("POST", `/api/qa/${it.id}/links`, { other: l.id, remove: true }), "연결을 끊었다.")) }, "끊기"))),
    el("div", { class: "row" }, other,
      el("button", { onclick: async () => {
        const n = Number(other.value);
        if (!n) return toast("번호를 넣는다.", true);
        afterChange(await act(() => api("POST", `/api/qa/${it.id}/links`, { other: n }), "연결했다."));
      } }, "연결")));
}

function revisionSection(it) {
  const rows = it.revisions.map((r) => {
    const diffs = [];
    if (r.op === "update" && r.before && r.after) {
      for (const [k, label] of [["question", "질문"], ["answer", "답변"], ["category_id", "분류"], ["evidence_state", "근거 상태"]]) {
        if (r.before[k] !== r.after[k]) {
          diffs.push(el("div", { class: "diff" }, `${label}: `, el("span", { class: "del" }, String(r.before[k] ?? "")), " → ", String(r.after[k] ?? "")));
        }
      }
      if (!r.before.reviewed_at && r.after.reviewed_at) diffs.push(el("div", { class: "diff" }, "검토 완료"));
      if (r.before.reviewed_at && !r.after.reviewed_at) diffs.push(el("div", { class: "diff" }, "미검토로 바뀜"));
    }
    return el("div", { class: "item rev" },
      `${when(r.at)} · ${ACTOR[r.actor] || r.actor} · ${OP[r.op] || r.op}`,
      r.after && r.after.change_note ? el("span", { class: "muted" }, ` · ${r.after.change_note}`) : null,
      diffs);
  });
  return el("section", {}, el("h2", {}, "이력 (최근 20건)"), rows);
}

// ---------- 새 Q&A ----------

async function submitNew(force) {
  const kind = $("#new-ev-kind").value;
  const body = { category: Number($("#new-cat").value), question: $("#new-q").value, answer: $("#new-a").value, force };
  if (kind && $("#new-ev-value").value.trim()) body.evidence = [evidenceSpec(kind, $("#new-ev-value").value, $("#new-ev-anchor").value)];
  const r = await act(() => api("POST", "/api/qa", body));
  const box = $("#new-similar");
  box.replaceChildren();
  if (!r) return;
  if (r.status === "similar_found") {
    $("#new-msg").textContent = "비슷한 질문이 있다. 같은 내용이면 그 항목을 고친다.";
    box.append(el("div", { class: "results", style: "max-height:none" },
      r.similar.map((h) => el("div", { class: "hit", onclick: () => { showTab("search"); openDetail(h.id); } },
        el("div", { class: "q" }, h.question), el("div", { class: "a" }, h.answer),
        el("div", { class: "meta" }, `#${h.id} · ${h.category_path} · 유사도 ${Math.round(h.score * 100)}%`)))),
      el("div", { class: "row" }, el("button", { type: "button", onclick: () => submitNew(true) }, "다른 질문이 맞다. 그래도 등록")));
    return;
  }
  $("#new-msg").textContent = "";
  toast(`#${r.item.id} 를 등록했다.`);
  $("#new-q").value = "";
  $("#new-a").value = "";
  $("#new-ev-value").value = "";
  $("#new-ev-anchor").value = "";
  ["#new-q", "#new-a"].forEach((s) => $(s).dispatchEvent(new Event("input"))); // 글자 수 표시 갱신
  refreshCats();
  refreshStatus();
  showTab("search");
  openDetail(r.item.id);
  runSearch();
}

// ---------- 분류 탭 ----------

async function loadCats() {
  await refreshCats();
  const tree = $("#cat-tree");
  tree.replaceChildren();
  tree.className = "tree";
  const node = (c, child) => el("div", { class: "node" + (child ? " child" : "") },
    el("div", { class: "row" },
      el("strong", {}, c.name), el("span", { class: "count" }, c.count),
      c.description ? el("span", { class: "muted" }, c.description) : null,
      el("button", { class: "link", onclick: async () => {
        const name = prompt("새 이름", c.name);
        if (name == null) return;
        const description = prompt("설명", c.description);
        if (await act(() => api("PATCH", `/api/categories/${c.id}`, { version: c.version, name, description: description ?? c.description }), "분류를 고쳤다.")) loadCats();
      } }, "고치기"),
      el("button", { class: "link", onclick: async () => {
        if (await act(() => api("POST", `/api/categories/${c.id}/archive`, { version: c.version }), "분류를 보관했다.")) loadCats();
      } }, "보관")));
  for (const t of state.cats) {
    tree.append(node(t, false));
    for (const c of t.children) tree.append(node(c, true));
  }
  if (!state.cats.length) tree.append(el("p", { class: "muted" }, "분류가 없다. 위에서 대분류부터 만든다."));
}

// ---------- 사전 탭 ----------

async function dictAction(body, msg) {
  const r = await act(() => api("POST", "/api/dict", body), msg);
  if (r && r.reindexed != null) toast(`${msg} 형태소 색인 ${r.reindexed}건을 다시 만들었다.`);
  if (r) { renderDict(r.snapshot); runSearch(); refreshStatus(); }
}

async function loadDict() {
  renderDict(await api("GET", "/api/dict"));
}

function chip(label, onRemove) {
  return el("span", { class: "chip" }, label, el("button", { class: "link", title: "빼기", onclick: onRemove }, "×"));
}

function renderDict(snap) {
  $("#syn-list").replaceChildren(...snap.synonyms.map((g) => {
    const input = el("input", { placeholder: "이 그룹에 단어 추가", style: "width:180px" });
    return el("div", { class: "list" }, el("div", { class: "item" },
      el("div", {}, g.terms.map((t) => chip(t.term, () => dictAction({ action: "synonym_remove", id: t.id }, "동의어를 뺐다.")))),
      input,
      el("button", { onclick: () => input.value.trim() && dictAction({ action: "synonym_add", terms: [input.value], group_id: g.group_id }, "동의어를 추가했다.") }, "추가")));
  }));
  if (!snap.synonyms.length) $("#syn-list").append(el("p", { class: "muted" }, "동의어 그룹이 없다."));
  $("#sw-list").replaceChildren(...snap.stopwords.map((s) => chip(s.word, () => dictAction({ action: "stopword_remove", id: s.id }, "불용어를 뺐다."))));
  $("#uw-list").replaceChildren(...snap.user_words.map((u) => chip(`${u.word} (${u.tag})`, () => dictAction({ action: "user_word_remove", id: u.id }, "사용자 단어를 뺐다."))));
  refreshStatus();
}

// ---------- 감독 탭 ----------

async function loadWatch() {
  const o = await api("GET", "/api/oversight");
  const card = (h, extra) => el("div", { class: "hit" },
    el("div", { class: "q" }, h.question), el("div", { class: "a" }, h.answer),
    el("div", { class: "meta" }, badges(h), el("button", { class: "link", onclick: () => { showTab("search"); openDetail(h.id); } }, "열기"), extra));
  $("#w-unreviewed").replaceChildren(...o.unreviewed.map((h) => card(h,
    el("button", { class: "link", onclick: async () => { if (await act(() => api("POST", `/api/qa/${h.id}/review`), "검토 완료로 표시했다.")) { loadWatch(); runSearch(); } } }, "검토 완료"))));
  if (!o.unreviewed.length) $("#w-unreviewed").append(el("p", { class: "muted" }, "없다."));
  $("#w-stale").replaceChildren(...o.stale.map((h) => card(h)));
  if (!o.stale.length) $("#w-stale").append(el("p", { class: "muted" }, "없다."));
  $("#w-recent").replaceChildren(el("div", { class: "list" }, o.recent.map((r) => el("div", { class: "item" },
    el("span", { class: "muted" }, when(r.at)), `${ACTOR[r.actor] || r.actor} · ${TABLE[r.table] || r.table} ${OP[r.op] || r.op}`,
    el("span", { class: "muted" }, r.label)))));
  $("#watch-count").textContent = (o.unreviewed.length + o.stale.length) || "";
}

// ---------- 상태 줄 ----------

async function refreshStatus() {
  try {
    const s = await api("GET", "/api/status");
    const morph = { on: "형태소 켜짐", idle: "형태소 대기(첫 검색 때 켜짐)", off: "형태소 꺼짐" }[s.morph];
    const box = $("#status");
    box.textContent = `Q&A ${s.counts.qa}건 · ${morph} · 재색인 대기 ${s.pending_reindex} · 재검증 ${when(s.last_recheck)} · 백업 ${when(s.last_backup)}`;
    box.title = s.analyzer_error || s.db_path;
    const rb = $("#reindex-box");
    rb.replaceChildren(el("span", { class: "muted" }, `형태소 재색인 대기 ${s.pending_reindex}건`),
      s.pending_reindex ? el("button", { onclick: async () => {
        const r = await act(() => api("POST", "/api/reindex"));
        if (r) { toast(`${r.reindexed}건을 다시 색인했다.`); refreshStatus(); }
      } }, "지금 재색인") : null);
    const o = await api("GET", "/api/oversight");
    $("#watch-count").textContent = (o.unreviewed.length + o.stale.length) || "";
  } catch (e) {
    $("#status").textContent = "서버에 연결하지 못했다: " + e.message;
  }
}

// ---------- 이벤트 ----------

function openFromHash() {
  const m = location.hash.match(/^#qa\/(\d+)/);
  if (m) {
    showTab("search");
    openDetail(Number(m[1]));
  }
}

function init() {
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  const q = $("#q");
  q.addEventListener("input", scheduleSearch);
  q.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); markSel(Math.min(state.sel + 1, state.hits.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); markSel(Math.max(state.sel - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); select(state.sel < 0 ? 0 : state.sel); }
    else if (e.key === "Escape") { q.value = ""; runSearch(); }
  });
  document.addEventListener("keydown", (e) => {
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
    if (e.key === "/" && !typing) { e.preventDefault(); showTab("search"); q.focus(); q.select(); }
  });
  for (const id of ["opt-case", "opt-regex"]) {
    $("#" + id).addEventListener("click", (e) => {
      const key = id === "opt-case" ? "case" : "regex";
      state.opts[key] = !state.opts[key];
      e.currentTarget.classList.toggle("on", state.opts[key]);
      runSearch();
    });
  }
  ["#f-cat", "#f-only", "#f-archived"].forEach((s) => $(s).addEventListener("change", runSearch));

  const nq = $("#new-q"), na = $("#new-a");
  nq.parentElement.querySelector(".counter").replaceWith(counter(nq, 150));
  na.parentElement.querySelector(".counter").replaceWith(counter(na, 400, 2));
  $("#new-form").addEventListener("submit", (e) => { e.preventDefault(); submitNew(false); });

  $("#cat-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { name: $("#cat-name").value, parent: $("#cat-parent").value ? Number($("#cat-parent").value) : null, description: $("#cat-desc").value };
    if (await act(() => api("POST", "/api/categories", body), "분류를 만들었다.")) {
      $("#cat-name").value = "";
      $("#cat-desc").value = "";
      loadCats();
    }
  });
  $("#syn-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const terms = $("#syn-terms").value.split(",").map((s) => s.trim()).filter(Boolean);
    dictAction({ action: "synonym_add", terms }, "동의어 그룹을 만들었다.").then(() => ($("#syn-terms").value = ""));
  });
  $("#sw-form").addEventListener("submit", (e) => {
    e.preventDefault();
    dictAction({ action: "stopword_add", word: $("#sw-word").value }, "불용어를 추가했다.").then(() => ($("#sw-word").value = ""));
  });
  $("#uw-form").addEventListener("submit", (e) => {
    e.preventDefault();
    dictAction({ action: "user_word_add", word: $("#uw-word").value, tag: $("#uw-tag").value }, "사용자 단어를 추가했다.").then(() => ($("#uw-word").value = ""));
  });
  $("#w-recheck").addEventListener("click", async () => {
    $("#w-recheck-msg").textContent = "재검증 중...";
    const r = await act(() => api("POST", "/api/recheck", {}));
    $("#w-recheck-msg").textContent = r ? `판정 ${r.results.length}건, 바뀐 판정 ${r.results.filter((x) => x.changed).length}건` : "";
    loadWatch();
    runSearch();
  });

  window.addEventListener("hashchange", openFromHash);
  refreshCats().then(() => { runSearch(); openFromHash(); });
  refreshStatus();
  setInterval(refreshStatus, 60000);
}

init();
