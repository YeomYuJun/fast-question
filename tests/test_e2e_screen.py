"""화면을 실제 브라우저(Chromium, headless)로 여는 테스트. 데모 DB(scripts/demo_db.py)를 임시 폴더에 만들어 로컬 모드 서버를 띄운다.

playwright 는 서버·클라이언트 의존성이 아니라 requirements-e2e.txt 에 따로 있다. 패키지나 브라우저가 없으면 이 파일은 건너뛴다.
  .venv\\Scripts\\python.exe -m pip install -r requirements-e2e.txt
  .venv\\Scripts\\python.exe -m playwright install chromium
각 테스트는 새 페이지를 열고, 페이지가 낸 스크립트 오류가 없어야 통과한다. DB 는 파일 전체가 하나를 쓰므로 테스트마다
다른 항목을 고친다.
"""
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


@pytest.fixture
def page(browser, base_url):
    ctx = browser.new_context(base_url=base_url, viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.fq_errors = errors  # 서버가 거부하는 요청을 일부러 보내는 테스트가 그 줄을 지운다
    yield pg
    ctx.close()
    assert errors == [], errors


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
    ctx = browser.new_context(base_url=base_url, viewport={"width": 375, "height": 800})
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
