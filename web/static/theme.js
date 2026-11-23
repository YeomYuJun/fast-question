"use strict";
// 테마와 화면 언어는 그리기 전에 정한다. 저장값이 없으면 테마는 OS 설정, 언어는 navigator.language 를 따른다.
// index.html 의 <head> 에서 스타일시트보다 먼저 defer 없이 읽는다(CSP 가 인라인 스크립트를 막아 파일로 뒀다)
// 언어는 window.fqLang 에만 둔다. <html lang> 은 각 화면 스크립트가 쓴다. OAuth 화면도 이 파일을 읽지만 언어를 서버가
// Accept-Language 로 정해 <html lang> 에 이미 적어 보낸다
(function () {
  var t = null;
  try { t = localStorage.getItem("fq-theme"); } catch (e) { t = null; }
  if (t !== "light" && t !== "dark") t = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", t);

  var l = null;
  try { l = localStorage.getItem("fq.lang"); } catch (e) { l = null; }
  if (l !== "ko" && l !== "en") l = String(navigator.language || "").toLowerCase().indexOf("ko") === 0 ? "ko" : "en";
  window.fqLang = l;
  // 약관·처리방침은 언어마다 파일이 따로라 <head> 의 <link rel="alternate" hreflang> 가 다른 언어판을 가리킨다.
  // 그리기 전에 옮겨야 다른 언어 본문이 잠깐 보이지 않는다
  var alt = document.querySelector('link[rel="alternate"][hreflang="' + l + '"]');
  if (alt && document.documentElement.lang !== l) location.replace(alt.getAttribute("href") + location.hash);

  // 언어 전환 버튼(button[data-lang]). 고른 값을 저장하고 다시 읽는다. 검색어·펼친 항목은 주소에, Claude 대화는 sessionStorage 에 남는다
  window.fqLangButtons = function (root) {
    var btns = (root || document).querySelectorAll("button[data-lang]");
    for (var i = 0; i < btns.length; i++) {
      var b = btns[i];
      var on = b.getAttribute("data-lang") === l;
      b.setAttribute("aria-pressed", String(on));
      b.classList.toggle("on", on);
      b.addEventListener("click", function (ev) {
        var pick = ev.currentTarget.getAttribute("data-lang");
        if (pick === l) return;
        try { localStorage.setItem("fq.lang", pick); } catch (e) { return; }
        location.reload();
      });
    }
  };
})();
