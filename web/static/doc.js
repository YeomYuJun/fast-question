"use strict";
// /terms, /privacy, /s/<토큰> 의 테마·언어 버튼. 본 화면(app.js)과 같은 저장값(fq-theme, fq.lang)을 쓴다
(function () {
  var en = window.fqLang === "en";
  var btn = document.getElementById("theme-btn");
  function label(t) {
    var toLight = en ? "Switch to light theme" : "라이트 테마로 바꾸기";
    var toDark = en ? "Switch to dark theme" : "다크 테마로 바꾸기";
    btn.setAttribute("aria-label", t === "dark" ? toLight : toDark);
    btn.title = btn.getAttribute("aria-label");
  }
  label(document.documentElement.getAttribute("data-theme"));
  btn.addEventListener("click", function () {
    var t = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("fq-theme", t); } catch (e) { /* 저장이 막혀도 이 페이지에서는 바뀐다 */ }
    label(t);
  });
  window.fqLangButtons();
})();
