"""원격 모드 화면을 실제 브라우저로 여는 테스트(tests/test_e2e_screen.py 와 같은 조건: playwright 가 없으면 건너뛴다).

로그인은 공급자 화면을 거치지 않고 제어 DB 에 세션을 만들어 쿠키로 넣는다. public_url 은 http://127.0.0.1:<포트> 다
(http 라 세션 쿠키 이름에 __Host- 접두어가 붙지 않는다). 공개 지식 층은 tests/test_public_kb.py 의 원본을 적재한다.
"""
import socket
import threading
import time

import pytest

pytest.importorskip("playwright.sync_api")
import uvicorn  # noqa: E402
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402

from kb import control, tenant  # noqa: E402
from kb.morph import Analyzer  # noqa: E402
from scripts import public_db  # noqa: E402
from tests.test_auth import make_cfg  # noqa: E402
from tests.test_public_kb import build_source  # noqa: E402
from web import auth  # noqa: E402
from web.app import create_app  # noqa: E402


def _no_kiwi():
    raise RuntimeError("화면 테스트는 형태소 계층을 쓰지 않는다")


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """(주소, 세션 쿠키 이름, 세션 토큰). 계정 하나와 공개 DB(항목 2건)가 있는 원격 모드 서버."""
    tmp = tmp_path_factory.mktemp("e2e-remote")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    cfg = make_cfg(tmp, public_url=url, port=port, github_client_id="", google_client_id="")
    public_db.set_public(cfg.data_dir, build_source(tmp))
    conn = control.connect_control(tenant.control_path(cfg.data_dir))
    token = control.create_session(conn, control.create_account(conn, "화면 테스트"))
    conn.close()
    app = create_app(cfg, run_jobs=False, analyzer=Analyzer(loader=_no_kiwi))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "서버가 15초 안에 뜨지 않았다"
        time.sleep(0.05)
    yield url, auth.cookie_names(cfg)[0], token
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


def new_page(browser, site, logged_in: bool, errors: list[str], locale: str = "ko-KR"):
    """화면 언어는 navigator.language 를 따르므로 브라우저 언어를 정해 연다."""
    url, cookie, token = site
    ctx = browser.new_context(base_url=url, viewport={"width": 1280, "height": 900}, locale=locale)
    if logged_in:
        ctx.add_cookies([{"name": cookie, "value": token, "url": url}])
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    return ctx, page


@pytest.fixture
def page(browser, site):
    errors: list[str] = []
    ctx, pg = new_page(browser, site, True, errors)
    yield pg
    ctx.close()
    assert errors == [], errors


def test_without_session_shows_login_screen(browser, site):
    errors: list[str] = []
    ctx, page = new_page(browser, site, False, errors)
    page.goto("/")
    expect(page.locator("#login")).to_be_visible()
    expect(page.locator("#app")).to_be_hidden()
    ctx.close()
    assert errors == []


def test_session_opens_app_with_seeded_guide_and_connect_panel(page):
    page.goto("/")
    expect(page.locator("#account")).to_be_visible()
    expect(page.locator("#login")).to_be_hidden()
    expect(page.locator("#results .row").first).to_be_visible()  # 새 계정 DB 에 기본 가이드가 들어간다
    page.click("#connect-btn")
    panel = page.locator("#connect")
    expect(panel).to_be_visible()
    expect(panel).to_contain_text("/mcp")  # 한 줄 연결 명령의 주소


def test_public_bundle_copies_into_own_store(page):
    page.goto("/")
    page.fill("#q", "롤링")
    box = page.locator("#public")
    expect(box).to_be_visible()
    row = box.locator(".pub-row")
    expect(row).to_have_count(1)
    row.locator(".rowbtn").click()
    expect(row.locator(".detail")).to_contain_text("운영자가 적재한 공개 항목입니다.")
    row.get_by_role("button", name="내 저장소로").click()
    expect(page.locator("#toast")).to_be_visible()
    mine = page.locator("#results .row:not(.out) .qtext")
    expect(mine.filter(has_text="롤링")).to_have_count(1)  # 내 결과에 복사본이 생긴다
    copied = page.request.get("/api/search", params={"q": "롤링"}).json()["hits"]
    assert len(copied) == 1 and copied[0]["created_by"] == "user"


def test_share_link_opens_without_login_and_stops_after_revoke(browser, site, page):
    page.goto("/")
    hit = page.request.get("/api/search", params={"q": ""}).json()["hits"][0]
    made = page.request.post("/api/account/shares", data={"kind": "qa", "id": hit["id"]}).json()
    assert made["url"].startswith(site[0] + "/s/")
    errors: list[str] = []
    ctx, guest = new_page(browser, site, False, errors)
    guest.goto(made["url"])
    expect(guest.locator("#share-items")).to_contain_text(hit["question"])
    listed = page.request.get("/api/account/shares").json()["shares"]
    assert page.request.delete(f"/api/account/shares/{listed[0]['id']}").ok
    guest.reload()
    expect(guest.locator("#share-items")).not_to_contain_text(hit["question"])
    expect(guest.locator("#share-title")).to_have_text("공개 링크를 찾지 못했습니다")
    ctx.close()
    assert errors == []


# ---- 화면 언어: 로그인한 원격 모드에만 있는 화면(연결 패널, Claude 패널, 로그인 화면, 공개 링크) ----

from tests.test_e2e_screen import HANGUL_IN_UI  # noqa: E402


@pytest.fixture
def en_page(browser, site):
    errors: list[str] = []
    ctx, pg = new_page(browser, site, True, errors, "en-US")
    yield pg
    ctx.close()
    assert errors == [], errors


def test_english_connect_and_claude_panels_have_no_korean_ui(en_page):
    page = en_page
    page.goto("/")
    expect(page.locator("#account")).to_be_visible()
    expect(page.locator("#connect-btn")).to_have_text("Connect")
    page.click("#connect-btn")
    panel = page.locator("#connect")
    expect(panel.locator("h2")).to_have_text("Connect")
    expect(panel).to_contain_text("Delete account")
    assert page.evaluate(HANGUL_IN_UI, "#connect") == []
    page.click("#claude-btn")
    expect(page.locator("#claude h3")).to_have_text("Connect Claude to this repository")
    assert page.evaluate(HANGUL_IN_UI, "#claude, header.top") == []


HEADER_OVERLAP = """() => [...document.querySelectorAll('.top > *')].some((a, i, all) => all.slice(i + 1).some((b) => {
  const p = a.getBoundingClientRect(), q = b.getBoundingClientRect();
  return p.width && q.width && p.left < q.right && q.left < p.right && p.top < q.bottom && q.top < p.bottom;
}))"""


@pytest.mark.parametrize("locale", ["ko-KR", "en-US"])
@pytest.mark.parametrize("width", [902, 960, 1100, 1201])
def test_header_buttons_do_not_overlap_with_claude_panel_open(browser, site, locale, width):
    """패널이 열리면 상단은 남은 폭에 든다. 영어 버튼 글자는 길어 좁은 폭에서 언어 버튼을 숨기거나 두 줄로 꺾는다."""
    errors: list[str] = []
    ctx, page = new_page(browser, site, True, errors, locale)
    page.set_viewport_size({"width": width, "height": 700})
    page.goto("/")
    expect(page.locator("#account")).to_be_visible()
    page.click("#claude-btn")
    expect(page.locator("#claude h3")).to_be_visible()
    assert page.evaluate(HEADER_OVERLAP) is False
    ctx.close()
    assert errors == []


def test_korean_connect_and_claude_panels(page):
    page.goto("/")
    page.click("#connect-btn")
    expect(page.locator("#connect h2")).to_have_text("연결")
    expect(page.locator("#connect")).to_contain_text("탈퇴")
    page.click("#claude-btn")
    expect(page.locator("#claude h3")).to_have_text("Claude 를 이 저장소에 연결")
    expect(page.locator("#claude")).not_to_contain_text("null")  # 키가 유효할 때 빈 경고 자리가 글자로 남지 않는다


def test_english_login_screen_without_providers(browser, site):
    errors: list[str] = []
    ctx, page = new_page(browser, site, False, errors, "en-US")
    page.goto("/")
    expect(page.locator("#login")).to_be_visible()
    expect(page.locator("#login-title")).to_have_text("Get started with fast question")
    expect(page.locator("#login-btns")).to_have_text("No sign-in provider is enabled.")
    assert page.evaluate(HANGUL_IN_UI, "#login, header.top") == []
    ctx.close()
    assert errors == []


def test_english_share_page(browser, site, page):
    page.goto("/")
    hit = page.request.get("/api/search", params={"q": ""}).json()["hits"][0]
    made = page.request.post("/api/account/shares", data={"kind": "qa", "id": hit["id"]}).json()
    errors: list[str] = []
    ctx, guest = new_page(browser, site, False, errors, "en-US")
    guest.goto(made["url"])
    expect(guest.locator("#share-title")).to_have_text("Shared question")
    assert guest.evaluate("document.documentElement.lang") == "en"
    expect(guest.locator(".doc-foot")).to_contain_text("Privacy Policy")
    guest.goto(made["url"] + "x")
    expect(guest.locator("#share-title")).to_have_text("Public link not found")
    expect(guest.locator("#share-lead")).to_have_text("It was revoked or the address does not exist.")
    ctx.close()
    assert errors == []
