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


def new_page(browser, site, logged_in: bool, errors: list[str]):
    url, cookie, token = site
    ctx = browser.new_context(base_url=url, viewport={"width": 1280, "height": 900})
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
    ctx.close()
    assert errors == []
