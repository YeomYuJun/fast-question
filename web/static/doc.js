"use strict";
// /terms, /privacy 의 테마 버튼. 본 화면(app.js)과 같은 저장값(fq-theme)을 쓴다
(function () {
  var btn = document.getElementById("theme-btn");
  function label(t) {
    btn.setAttribute("aria-label", t === "dark" ? "라이트 테마로 바꾸기" : "다크 테마로 바꾸기");
    btn.title = btn.getAttribute("aria-label");
  }
  label(document.documentElement.getAttribute("data-theme"));
  btn.addEventListener("click", function () {
    var t = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("fq-theme", t); } catch (e) { /* 저장이 막혀도 이 페이지에서는 바뀐다 */ }
    label(t);
  });
})();
