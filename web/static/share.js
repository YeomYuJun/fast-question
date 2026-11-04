"use strict";
// 공개 링크 페이지(/s/<토큰>). 주소의 토큰으로 /api/share/<토큰> 을 읽어 질문·답변 목록을 그린다. 로그인이 없다
(function () {
  var token = location.pathname.split("/").pop();
  var title = document.getElementById("share-title");
  var meta = document.getElementById("share-meta");
  var lead = document.getElementById("share-lead");
  var box = document.getElementById("share-items");

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
    return r.json().then(function (data) { return { ok: r.ok, data: data }; });
  }).then(function (res) {
    if (!res.ok) {
      title.textContent = "공개 링크를 찾지 못했습니다";
      lead.textContent = res.data.error || "폐기됐거나 없는 주소입니다.";
      return;
    }
    var d = res.data;
    document.title = d.title + " - fast question";
    title.textContent = d.kind === "qa" ? "공개한 질문" : d.path;
    meta.textContent = d.kind === "qa" ? d.path : d.items.length + "건";
    lead.textContent = d.kind === "qa" ? "" : d.description;
    d.items.forEach(function (it) { box.append(row(it)); });
  }).catch(function () {
    title.textContent = "서버에 연결하지 못했습니다";
  });
})();
