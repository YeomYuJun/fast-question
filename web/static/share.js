"use strict";
// 공개 링크 페이지(/s/<토큰>). 주소의 토큰으로 /api/share/<토큰> 을 읽어 질문·답변 목록을 그린다. 로그인이 없다
(function () {
  var L = {
    ko: {
      page: "공개 링크", missing: "공개 링크를 찾지 못했습니다", gone: "폐기됐거나 없는 주소입니다.", qa: "공개한 질문",
      count: function (n) { return n + "건"; }, offline: "서버에 연결하지 못했습니다", terms: "이용약관", privacy: "개인정보 처리방침",
    },
    en: {
      page: "Public link", missing: "Public link not found", gone: "It was revoked or the address does not exist.",
      qa: "Shared question", count: function (n) { return n === 1 ? "1 item" : n + " items"; },
      offline: "Could not reach the server", terms: "Terms", privacy: "Privacy Policy",
    },
  }[window.fqLang === "en" ? "en" : "ko"];
  document.documentElement.lang = window.fqLang === "en" ? "en" : "ko";
  var token = location.pathname.split("/").pop();
  var title = document.getElementById("share-title");
  var meta = document.getElementById("share-meta");
  var lead = document.getElementById("share-lead");
  var box = document.getElementById("share-items");
  document.title = L.page + " - fast question";
  title.textContent = L.page;
  document.getElementById("share-terms").textContent = L.terms;
  document.getElementById("share-privacy").textContent = L.privacy;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function row(it) {
    var r = el("div", "share-item");
    var q = el("div", "line");
    q.append(el("span", "tag-q", "Q"), el("span", "qtext", it.question));
    var a = el("div", "line");
    a.append(el("span", "tag-a", "A"), el("span", "atext", it.answer));
    r.append(q, a, el("div", "share-path", it.path));
    return r;
  }

  fetch("/api/share/" + encodeURIComponent(token)).then(function (r) {
    return r.json().then(function (data) { return { ok: r.ok, status: r.status, data: data }; });
  }).then(function (res) {
    if (!res.ok) {
      title.textContent = L.missing;
      // 없는 주소(404)는 화면 문구로 알린다. 그 밖의 실패는 서버 문장을 보인다
      lead.textContent = res.status === 404 ? L.gone : res.data.error || L.gone;
      return;
    }
    var d = res.data;
    document.title = d.title + " - fast question";
    title.textContent = d.kind === "qa" ? L.qa : d.path;
    meta.textContent = d.kind === "qa" ? d.path : L.count(d.items.length);
    lead.textContent = d.kind === "qa" ? "" : d.description;
    d.items.forEach(function (it) { box.append(row(it)); });
  }).catch(function () {
    title.textContent = L.offline;
  });
})();
