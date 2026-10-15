"use strict";
// 테마는 그리기 전에 정한다. 저장값이 없으면 OS 설정을 따른다.
// index.html 의 <head> 에서 스타일시트보다 먼저 defer 없이 읽는다(CSP 가 인라인 스크립트를 막아 파일로 뒀다)
(function () {
  var t = null;
  try { t = localStorage.getItem("fq-theme"); } catch (e) { t = null; }
  if (t !== "light" && t !== "dark") t = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", t);
})();
