"""Login UI against the real authentication handler; server operations are mocked."""

from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, expect

import app
import auth
from test_auth import LOGIN, PASSWORD, auth_server  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.fixture
def login_url(page, auth_server, monkeypatch):  # noqa: F811
    monkeypatch.setattr(app.payloads, "STREAM_PLAN", (("overview", 100),))
    overview = {
        "ok": True,
        "serverName": "Auth test server",
        "docker": True,
        "compose": True,
        "rconConfigured": True,
        "rcon": {"state": "ok"},
        "containerInfo": {"running": True, "status": "running", "uptimeSec": 600},
        "settings": {},
        "backupsCount": 0,
    }
    monkeypatch.setattr(app.payloads, "stream_payload", lambda name: overview)

    def route_api(route):
        path = route.request.url.split("/api/", 1)[1].split("?", 1)[0]
        if path.startswith("auth/") or path == "stream":
            route.continue_()
        elif path == "overview":
            route.fulfill(json=overview)
        else:
            route.fulfill(json={"ok": True, "items": [], "points": [], "names": []})

    page.route("**/api/**", route_api)
    return f"http://127.0.0.1:{auth_server.server_port}"


def fill_login(page):
    page.get_by_label("Логин", exact=True).fill(LOGIN)
    page.get_by_label("Пароль", exact=True).fill(PASSWORD)
    page.get_by_role("button", name="Войти", exact=True).click()


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844), (320, 640)])
def test_login_navigation_reload_and_logout(page, login_url, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(login_url + "/#/console")
    expect(page.get_by_role("heading", name="PZ·ПУЛЬТ")).to_be_visible()
    expect(page.locator(".login-brand .brand-icon")).to_be_visible()
    expect(page.get_by_label("Запомнить меня")).not_to_be_checked()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    output = Path(__file__).resolve().parents[2] / ".tmp-ui-auth"
    output.mkdir(exist_ok=True)
    page.screenshot(path=str(output / f"login-{width}.png"))
    fill_login(page)
    expect(page.locator("#view-console")).to_be_visible()
    page.reload()
    expect(page.locator("#view-console")).to_be_visible()
    page.goto(login_url + "/#/overview")
    page.goto(login_url + "/#/console")
    page.get_by_role("button", name="Выйти", exact=True).click()
    expect(page.get_by_role("heading", name="PZ·ПУЛЬТ")).to_be_visible()
    page.go_back()
    expect(page.get_by_role("heading", name="PZ·ПУЛЬТ")).to_be_visible()


@pytest.mark.parametrize("remember", [False, True])
def test_remember_me_browser_cookie_and_restart(page, login_url, auth_server, remember):  # noqa: F811
    page.goto(login_url + "/login")
    if remember:
        # The label is also a touch target; Space toggles the native checkbox.
        page.get_by_text("Запомнить меня", exact=True).click()
        checkbox = page.get_by_label("Запомнить меня")
        expect(checkbox).to_be_checked()
        checkbox.focus()
        page.keyboard.press("Space")
        expect(checkbox).not_to_be_checked()
        page.keyboard.press("Space")
    fill_login(page)
    expect(page.locator("#view-overview")).to_be_visible()
    cookie = next(item for item in page.context.cookies() if item["name"] == "pz_session")
    assert cookie["httpOnly"] and cookie["sameSite"] == "Strict"
    assert cookie["expires"] > 0 if remember else cookie["expires"] == -1
    auth_server.auth = auth.Auth(
        LOGIN, PASSWORD, auth_server.test_key, sessions_file=auth_server.auth._sessions_file
    )
    # A fresh browser context represents reopening the browser with its retained cookies.
    reopened = page.context.browser.new_context(locale="ru-RU", service_workers="block")
    try:
        if remember:
            reopened.add_cookies([cookie])
        fresh_page = reopened.new_page()
        response = fresh_page.goto(login_url + "/api/auth/session")
        assert response.status == (200 if remember else 401)
        if not remember:
            fresh_page.goto(login_url + "/login")
            expect(fresh_page.get_by_role("heading", name="PZ·ПУЛЬТ")).to_be_visible()
    finally:
        reopened.close()


def test_wrong_password_retry_and_safe_redirect(page, login_url):
    page.goto(login_url + "/login?next=https://evil.test")
    page.get_by_label("Логин", exact=True).fill(LOGIN)
    password = page.get_by_label("Пароль", exact=True)
    password.fill("wrong")
    page.get_by_role("button", name="Войти", exact=True).click()
    expect(page.get_by_role("alert")).to_have_text("Неверный логин или пароль")
    expect(password).to_have_value("")
    expect(password).to_be_focused()
    fill_login(page)
    expect(page).to_have_url(login_url + "/#/overview")
    expect(page.locator("#view-overview")).to_be_visible()


def test_session_revocation_redirects_open_dashboard(page, login_url, auth_server):  # noqa: F811
    page.goto(login_url + "/")
    fill_login(page)
    expect(page.locator("#view-overview")).to_be_visible()
    page.wait_for_function("S.overview?.serverName === 'Auth test server'")
    # The real SSE connection must stop and redirect after revocation.
    cookies = page.context.cookies()
    cookie_header = "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies)
    auth_server.auth.sign_out(auth_server.auth.session(cookie_header))
    expect(page.get_by_role("heading", name="PZ·ПУЛЬТ")).to_be_visible(timeout=15000)


def test_api_401_redirects_without_demo(page, login_url):
    page.goto(login_url + "/")
    fill_login(page)
    expect(page.locator("#btnLogout")).to_be_visible()
    page.wait_for_function("typeof S !== 'undefined' && !!S.overview")
    page.context.clear_cookies()
    page.route("**/api/overview", lambda route: route.fulfill(status=401, json={"ok": False}))
    # Do not await the API promise in the departing document: the successful
    # redirect destroys that context before Playwright can receive its result.
    try:
        page.evaluate("() => { api('/api/overview').catch(() => {}); }")
    except PlaywrightError as error:
        # The redirect can finish before evaluate's result crosses the browser connection.
        if "Execution context was destroyed" not in str(error):
            raise
    expect(page.get_by_role("heading", name="PZ·ПУЛЬТ")).to_be_visible()
