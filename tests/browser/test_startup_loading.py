"""The first paint hides incomplete data until the initial snapshot is ready."""

from pathlib import Path

import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("width", [390, 1440])
def test_startup_waits_for_profile_and_visible_data(page, dashboard, language, width):
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    profiles, backups = [], []
    page.route("**/api/server-configs", lambda route: profiles.append(route))
    page.route("**/api/backups", lambda route: backups.append(route))
    page.goto(dashboard["url"])
    loader = page.locator("#startupLoader")
    expect(loader).to_be_visible()
    expect(loader).to_contain_text(
        "Loading server data…" if language == "en" else "Загружаем данные сервера…"
    )
    expect(page.locator("#dashboardShell")).to_have_attribute("inert", "")
    expect(page.locator("#view-overview")).to_be_hidden()
    expect(page.locator("#btnStop")).to_be_disabled()
    page.keyboard.press("Control+k")
    expect(page.locator("#commandDialog")).not_to_be_visible()
    page.keyboard.press("Tab")
    assert not page.evaluate("dashboardShell.contains(document.activeElement)")
    assert (
        page.evaluate(
            "getComputedStyle(document.querySelector('.startup-track span')).animationName"
        )
        == "none"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    out = Path(".tmp-startup-loading")
    out.mkdir(exist_ok=True)
    page.screenshot(path=str(out / f"startup-{language}-{width}.png"), full_page=True)
    assert len(profiles) == len(backups) == 1
    profiles.pop().fallback()
    expect(page.locator("#configProfile option")).to_have_count(1)
    expect(loader).to_be_visible()
    backups.pop().fallback()
    expect(loader).to_be_hidden()
    expect(page.locator("#view-overview")).to_be_visible()
    expect(page.locator("#view-overview")).to_be_focused()
    expect(page.locator("#btnStop")).to_be_enabled()
    assert not page.locator("#dashboardShell").evaluate("el => el.inert")
    page.locator('.nav [data-route="settings"]').click()
    expect(page.locator("#view-settings")).to_be_visible()
    expect(loader).to_be_hidden()


def test_startup_slow_request_offers_recovery_and_failure_releases_shell(page, dashboard):
    page.clock.install()
    profiles = []
    page.route("**/api/server-configs", lambda route: profiles.append(route))
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#startupLoader")).to_be_visible()
    page.clock.run_for(15001)
    expect(page.locator("#startupSlow")).to_be_visible()
    expect(page.locator("#startupRetry")).to_be_enabled()
    page.emulate_media(reduced_motion="no-preference")
    assert (
        page.evaluate(
            "getComputedStyle(document.querySelector('.startup-track span')).animationName"
        )
        == "startup-loading"
    )
    profiles.pop().fulfill(status=503, json={"ok": False, "error": "Profile unavailable"})
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect(page.locator("#configFields")).to_have_text("Profile unavailable")
    expect(page.locator("#view-settings")).to_be_visible()


def test_demo_startup_uses_no_live_api_and_reveals_loaded_preview(page, dashboard):
    requests = []

    def reject_live_api(route):
        requests.append(route.request.url)
        route.fulfill(status=401, json={"ok": False})

    page.route("**/api/**", reject_live_api)
    page.goto(dashboard["url"] + "/?demo=1")
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect(page.locator("#demoBadge")).to_be_visible()
    expect(page.locator("#view-overview")).to_be_visible()
    expect(page.locator("#kpiOnline")).to_have_text("3")
    expect(page.locator("#btnStop")).to_be_disabled()
    assert requests == []
