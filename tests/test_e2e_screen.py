"""화면을 실제 브라우저(Chromium, headless)로 여는 테스트. 데모 DB(scripts/demo_db.py)를 임시 폴더에 만들어 로컬 모드 서버를 띄운다.

playwright 는 서버·클라이언트 의존성이 아니라 requirements-e2e.txt 에 따로 있다. 패키지나 브라우저가 없으면 이 파일은 건너뛴다.
  .venv\\Scripts\\python.exe -m pip install -r requirements-e2e.txt
  .venv\\Scripts\\python.exe -m playwright install chromium
각 테스트는 새 페이지를 열고, 페이지가 낸 스크립트 오류가 없어야 통과한다. DB 는 파일 전체가 하나를 쓰므로 테스트마다
다른 항목을 고친다.
"""
import re
import socket
import threading
import time

import pytest

pytest.importorskip("playwright.sync_api")
import uvicorn  # noqa: E402
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from kb.config import load_config  # noqa: E402
from kb.morph import Analyzer  # noqa: E402
from scripts import demo_db  # noqa: E402
from web.app import create_app  # noqa: E402


def _no_kiwi():
    raise RuntimeError("화면 테스트는 형태소 계층을 쓰지 않는다")


@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = load_config(demo_db.build(tmp_path_factory.mktemp("e2e"), port))
    app = create_app(cfg, run_jobs=False, analyzer=Analyzer(loader=_no_kiwi))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "서버가 15초 안에 뜨지 않았다"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except PlaywrightError as e:
            pytest.skip(f"Chromium 을 띄우지 못했다(playwright install chromium): {e}")
        yield b
        b.close()


def open_page(browser, base_url, locale: str):
    """화면 언어는 navigator.language 를 따르므로 브라우저 언어를 정해 연다."""
    ctx = browser.new_context(base_url=base_url, viewport={"width": 1280, "height": 900}, locale=locale)
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.fq_errors = errors  # 서버가 거부하는 요청을 일부러 보내는 테스트가 그 줄을 지운다
    return ctx, pg, errors


@pytest.fixture
def page(browser, base_url):
    ctx, pg, errors = open_page(browser, base_url, "ko-KR")
    yield pg
    ctx.close()
    assert errors == [], errors


@pytest.fixture
def en_page(browser, base_url):
    ctx, pg, errors = open_page(browser, base_url, "en-US")
    yield pg
    ctx.close()
    assert errors == [], errors


# 화면 문구(글자, aria-label, title, placeholder)에서 한글을 찾는다. 질문·답변·분류 이름처럼 데이터를 보이는 곳은 뺀다
# (데모 DB 의 데이터가 한국어다). 언어 전환 버튼은 각 언어 이름을 그 언어로 쓴다. 서버가 만드는 오류 문장도 대상이 아니다
HANGUL_IN_UI = """(sel) => {
  const skip = ".qtext, .atext, .grp-head, .facet .name, .facet-edit .name, .links, .ev, select, .cl-hit, .cl-qa, #cat-chip, .langs, #account-name";
  const out = [];
  for (const root of document.querySelectorAll(sel)) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const n = walker.currentNode;
      if (!n.parentElement.closest(skip) && /[\\uAC00-\\uD7A3]/.test(n.textContent)) out.push(n.textContent.trim());
    }
    for (const e of [root, ...root.querySelectorAll("*")]) {
      if (e.closest(skip)) continue;
      for (const a of ["aria-label", "title", "placeholder"]) {
        const v = e.getAttribute(a);
        if (v && /[\\uAC00-\\uD7A3]/.test(v)) out.push(`${a}=${v}`);
      }
    }
  }
  return out;
}"""


def rows(page):
    return page.locator("#results .row:not(.out)")


def search(page, text: str, count: int):
    page.fill("#q", text)
    expect(rows(page)).to_have_count(count)


def open_edit(page, text: str):
    """text 로 찾은 한 건을 펼쳐 편집 폼을 연다."""
    search(page, text, 1)
    rows(page).first.locator(".rowbtn").click()
    page.locator("#results .detail").get_by_role("button", name="편집").click()
    form = page.locator("#results form.edit")
    expect(form).to_be_visible()
    return form


def test_search_highlights_and_expands_with_address(page):
    page.goto("/")
    expect(rows(page)).to_have_count(16)  # 데모 DB 전체
    search(page, "CORS", 4)
    expect(page.locator("#results mark.hit").first).to_have_text("CORS")
    first = rows(page).first
    first.locator(".rowbtn").click()
    expect(first.locator(".detail .label").first).to_have_text("근거")
    expect(first.locator(".rowbtn")).to_have_attribute("aria-expanded", "true")
    assert "q=CORS" in page.url and "#qa/" in page.url
    page.keyboard.press("Escape")  # 펼침을 접는다
    expect(first.locator(".rowbtn")).to_have_attribute("aria-expanded", "false")


def test_address_restores_search_and_open_item(page):
    page.goto("/?q=PATCH#qa/2")
    expect(page.locator("#q")).to_have_value("PATCH")
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first.locator(".rowbtn")).to_have_attribute("aria-expanded", "true")


def test_zero_hits_offers_dropping_a_word(page):
    page.goto("/")
    page.fill("#q", "PATCH 없는단어")
    expect(page.locator("#empty")).to_be_visible()  # 0건 안내. 아래 목록에는 비슷한 결과가 따로 나온다
    chip = page.locator("#relaxed .chip")
    expect(chip).to_have_count(1)
    chip.click()
    expect(page.locator("#q")).to_have_value("PATCH")
    expect(rows(page)).to_have_count(1)


def test_edit_saves_answer(page):
    page.goto("/")
    form = open_edit(page, "PATCH")
    form.locator("textarea.fa").fill("PUT 은 전체를 바꾸고 PATCH 는 보낸 필드만 바꾼다(화면 테스트).")
    form.get_by_role("button", name="저장").click()
    expect(page.locator("#toast")).to_contain_text("저장했습니다.")
    expect(rows(page).first.locator(".atext")).to_contain_text("(화면 테스트)")
    saved = page.request.get("/api/qa/2").json()
    assert saved["answer"].endswith("(화면 테스트).") and saved["updated_by"] == "user"


def test_edit_rejects_overlong_answer_with_message(page):
    page.goto("/")
    form = open_edit(page, "bcrypt")
    form.locator("textarea.fa").fill("가" * 401)
    form.get_by_role("button", name="저장").click()
    expect(form.locator(".emsg")).not_to_be_empty()
    expect(form).to_be_visible()  # 저장되지 않고 폼이 남는다
    assert len(page.fq_errors) == 1 and "400" in page.fq_errors[0]  # 브라우저가 남기는 요청 실패 줄 하나뿐이다
    page.fq_errors.clear()


def test_delete_asks_and_undo_restores(page):
    page.goto("/")
    form = open_edit(page, "SameSite")
    page.once("dialog", lambda d: d.dismiss())
    form.get_by_role("button", name="이 질문 삭제").click()
    expect(form).to_be_visible()  # 확인 창을 취소하면 그대로다
    page.once("dialog", lambda d: d.accept())
    form.get_by_role("button", name="이 질문 삭제").click()
    toast = page.locator("#toast")
    expect(toast).to_contain_text("삭제했습니다.")
    expect(rows(page)).to_have_count(0)
    toast.get_by_role("button", name="되돌리기").click()
    expect(toast).to_contain_text("되돌렸습니다.")
    expect(rows(page)).to_have_count(1)


def test_new_question_is_registered_and_opened(page):
    page.goto("/")
    page.click("#new-btn")
    form = page.locator("#new-slot form.edit")
    form.locator("select.fld").select_option(label="웹 > 배포")
    form.locator("input.fq").fill("화면 테스트로 등록한 질문은 어디에 보이나?")
    form.locator("textarea.fa").fill("등록하면 목록에서 그 항목이 펼쳐져 보인다.")
    form.get_by_role("button", name="등록").click()
    expect(page.locator("#toast")).to_contain_text("등록했습니다.")
    expect(page.locator("#new-slot form.edit")).to_have_count(0)
    opened = page.locator('#results .row .rowbtn[aria-expanded="true"]')
    expect(opened).to_have_count(1)
    expect(opened.locator(".qtext")).to_have_text("화면 테스트로 등록한 질문은 어디에 보이나?")


def test_category_panel_filters_results(page):
    page.goto("/")
    page.click("#toggle-side")
    expect(page.locator("#side")).to_be_visible()
    page.locator("#cat-tree").get_by_text("인증", exact=True).click()
    expect(page.locator("#cat-chip")).to_be_visible()
    expect(rows(page)).to_have_count(5)  # 데모 DB 의 웹 > 인증 항목
    assert "cat=" in page.url
    page.keyboard.press("Escape")  # 분류 필터를 푼다
    expect(page.locator("#cat-chip")).to_be_hidden()


def test_phone_width_has_no_horizontal_scroll(browser, base_url):
    ctx = browser.new_context(base_url=base_url, viewport={"width": 375, "height": 800}, locale="ko-KR")
    page = ctx.new_page()
    page.goto("/")
    expect(rows(page).first).to_be_visible()
    rows(page).first.locator(".rowbtn").click()
    expect(page.locator("#results .detail").first).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    ctx.close()


def test_dark_theme_toggle_persists(page):
    page.goto("/")
    before = page.evaluate("document.documentElement.dataset.theme || ''")
    page.click("#theme-btn")
    after = page.evaluate("document.documentElement.dataset.theme || ''")
    assert after and after != before
    page.reload()
    assert page.evaluate("document.documentElement.dataset.theme || ''") == after


# ---- 화면 언어(ko/en) ----

def test_language_follows_browser_and_toggle_persists(page):
    page.goto("/")
    assert page.evaluate("document.documentElement.lang") == "ko"
    expect(page.locator("#new-btn")).to_have_text("+ 새 질문")
    expect(page.locator('button[data-lang="ko"]')).to_have_attribute("aria-pressed", "true")
    page.locator('button[data-lang="en"]').click()
    expect(page.locator("#new-btn")).to_have_text("+ New question")  # 고른 언어로 다시 읽는다
    assert page.evaluate("document.documentElement.lang") == "en"
    assert page.evaluate("localStorage.getItem('fq.lang')") == "en"
    expect(page.locator('button[data-lang="en"]')).to_have_attribute("aria-pressed", "true")
    page.locator('button[data-lang="ko"]').click()
    expect(page.locator("#new-btn")).to_have_text("+ 새 질문")
    assert page.evaluate("localStorage.getItem('fq.lang')") == "ko"


def test_english_screen_list_search_and_help_have_no_korean_ui(en_page):
    page = en_page
    page.goto("/")
    expect(rows(page).first).to_be_visible()
    assert page.evaluate("document.documentElement.lang") == "en"
    assert page.title() == "fast question - A Q&A store you share with Claude"
    expect(page.locator("#q")).to_have_attribute("placeholder", "What do you want to know?  e.g. CORS error")
    expect(page.locator("#sum-count")).to_contain_text("Recently updated:")
    page.click("#toggle-side")
    page.click("#help-btn")
    expect(page.locator("#help")).to_be_visible()
    assert page.evaluate(HANGUL_IN_UI, "header.top, .summary, #side, #help, #empty") == []
    page.keyboard.press("Escape")
    page.fill("#q", "PATCH nosuchword")  # 검색어는 0건 안내와 칩에 그대로 보이므로 한글이 없는 말로 찾는다
    expect(page.locator("#empty")).to_be_visible()
    expect(page.locator("#empty-msg")).to_contain_text("No matching questions.")
    expect(page.locator("#relaxed .chip")).to_contain_text("without")
    assert page.evaluate(HANGUL_IN_UI, ".summary, #empty") == []


def test_english_screen_detail_and_edit_have_no_korean_ui(en_page):
    page = en_page
    page.goto("/")
    search(page, "429", 1)
    rows(page).first.locator(".rowbtn").click()
    expect(rows(page).first.locator(".detail .label").first).to_have_text("Evidence")
    assert page.evaluate(HANGUL_IN_UI, "#results .detail") == []
    page.locator("#results .detail").get_by_role("button", name="Edit").click()
    form = page.locator("#results form.edit")
    expect(form).to_be_visible()
    assert page.evaluate(HANGUL_IN_UI, "#results form.edit") == []
    form.locator("textarea.fa").fill("Wait for Retry-After and send again (screen test).")
    form.get_by_role("button", name="Save").click()
    expect(page.locator("#toast")).to_contain_text("Saved.")


def test_terms_and_privacy_follow_screen_language(en_page):
    """화면 언어가 en 이면 /privacy 는 영어판으로 옮기고(#operator 유지), ko 버튼을 누르면 한국어판으로 돌아온다."""
    page = en_page
    page.goto("/privacy#operator")
    expect(page).to_have_url(re.compile(r"/privacy/en#operator$"))
    expect(page.locator("h1")).to_have_text("Privacy Policy")
    expect(page.locator('button[data-lang="en"]')).to_have_attribute("aria-pressed", "true")
    page.locator('button[data-lang="ko"]').click()
    expect(page).to_have_url(re.compile(r"/privacy#operator$"))
    expect(page.locator("h1")).to_have_text("개인정보 처리방침")
    page.goto("/terms/en")
    expect(page).to_have_url(re.compile(r"/terms$"))
    expect(page.locator("h1")).to_have_text("이용약관")


def test_english_edit_form_shows_note_prefix_in_english(en_page):
    """서버는 메모 근거를 "메모:" 로 돌려준다. en 편집 폼은 note: 로 보이고, 고치지 않고 저장하면 근거를 다시 보내지 않는다."""
    page = en_page
    page.goto("/")
    search(page, "PKCE", 1)
    rows(page).first.locator(".rowbtn").click()
    expect(page).to_have_url(re.compile(r"#qa/\d+$"))
    qa_id = int(page.evaluate("location.hash").split("/")[-1])
    cur = page.request.get(f"/api/qa/{qa_id}").json()
    r = page.request.patch(f"/api/qa/{qa_id}", data={"version": cur["version"], "evidence_line": "메모: checked by ops"})
    assert r.ok, r.text()
    version = r.json()["version"]
    assert r.json()["evidence_line"] == "메모: checked by ops"
    page.reload()  # 펼칠 때 읽어 둔 상세에는 고치기 전 근거가 있다
    page.locator("#results .detail").get_by_role("button", name="Edit").click()
    form = page.locator("#results form.edit")
    expect(form.locator("input.fe")).to_have_value("note: checked by ops")
    form.get_by_role("button", name="Save").click()
    expect(form).to_be_hidden()
    after = page.request.get(f"/api/qa/{qa_id}").json()
    assert after["version"] == version and after["evidence_line"] == "메모: checked by ops"


def test_counter_uses_weighted_length(page):
    """한글·한자는 2, 그 밖은 1로 센다. 질문 300, 답변 800, 답변 한 줄 240 을 넘으면 over 표시."""
    page.goto("/")
    page.click("#new-btn")
    form = page.locator("#new-slot form.edit")
    counts = form.locator(".count")
    form.locator("input.fq").fill("가나 ab")
    expect(counts.nth(0)).to_have_text("7/300")
    form.locator("input.fq").fill("가" * 150)
    expect(counts.nth(0)).not_to_have_class("count over")
    form.locator("input.fq").fill("가" * 150 + "a")
    expect(counts.nth(0)).to_have_class("count over")
    form.locator("textarea.fa").fill("a" * 240)
    expect(counts.nth(1)).to_have_text("240/800")
    expect(counts.nth(1)).not_to_have_class("count over")
    form.locator("textarea.fa").fill("가" * 121)  # 한 줄이 가중 242
    expect(counts.nth(1)).to_have_class("count over")
    form.locator("textarea.fa").fill("\n".join(["가" * 100] * 4) + "\n" + "a" * 1)
    expect(counts.nth(1)).to_have_text("805/800")
    expect(counts.nth(1)).to_have_class("count over")
    form.get_by_role("button", name="취소").click()


def test_string_tables_have_the_same_keys(page):
    page.goto("/")
    expect(rows(page).first).to_be_visible()
    diff = page.evaluate("""() => {
      const only = (a, b) => Object.keys(STR[a]).filter((k) => !(k in STR[b])).map((k) => `${a}:${k}`);
      return [...only("ko", "en"), ...only("en", "ko")];
    }""")
    assert diff == []


def test_weight_ranges_match_contract():
    """화면의 가중 길이 범위 표가 계약의 범위와 같다(kb/text.py 의 weight 와 같은 표를 쓴다)."""
    import re
    from web.app import STATIC
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    block = re.search(r"const WIDE = \[(.*?)\];", js, re.S).group(1)
    got = [(int(a, 16), int(b, 16)) for a, b in re.findall(r"\[0x([0-9A-Fa-f]+), 0x([0-9A-Fa-f]+)\]", block)]
    assert got == [(0x1100, 0x115F), (0x2E80, 0xA4CF), (0xAC00, 0xD7A3), (0xF900, 0xFAFF), (0xFE30, 0xFE4F),
                   (0xFF00, 0xFF60), (0xFFE0, 0xFFE6)]
    assert re.search(r"const QUESTION_MAX = 300;", js) and re.search(r"const ANSWER_MAX = 800;", js)
    assert re.search(r"const LINE_MAX = 240;", js)


def test_login_screen_in_both_languages(page, en_page):
    """로컬 모드에는 로그인 화면이 없어 showLogin 을 직접 불러 그린다."""
    for pg, lang, button in ((page, "ko", "GitHub 로 계속하기"), (en_page, "en", "Continue with GitHub")):
        pg.goto("/")
        expect(rows(pg).first).to_be_visible()
        pg.evaluate("history.replaceState(null, '', '/?login_error=denied'); showLogin(['github', 'google'])")
        expect(pg.locator("#login")).to_be_visible()
        expect(pg.locator("#login-btns a").first).to_have_text(button)
        expect(pg.locator(".langs")).to_be_visible()
        if lang == "en":
            expect(pg.locator("#login-err")).to_have_text("Sign-in was cancelled.")
            expect(pg.locator(".ex-u").first).to_have_text("How did I fix the CORS error last time?")
            assert pg.evaluate(HANGUL_IN_UI, "#login, header.top") == []
        else:
            expect(pg.locator("#login-err")).to_have_text("로그인을 취소했습니다.")
