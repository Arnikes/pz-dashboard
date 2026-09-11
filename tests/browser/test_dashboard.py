import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_navigation_and_layout(page, dashboard, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    for route in ["players", "mods", "maintenance", "backups", "events", "console", "overview"]:
        link = page.locator(f'.nav [data-route="{route}"]')
        link.click()
        expect(link).to_have_attribute("aria-current", "page")
        expect(page.locator(f"#view-{route}")).to_be_visible()
        expect(page.locator(".view:visible")).to_have_count(1)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.go_back()
    expect(page.locator("#view-console")).to_be_visible()


def test_stop_requires_confirmation_and_shows_api_error(page, dashboard):
    page.goto(dashboard["url"])
    page.locator("#btnStop").click()
    expect(page.get_by_role("alertdialog")).to_be_visible()
    assert dashboard["actions"] == []
    page.locator("#modalCancel").click()
    expect(page.get_by_role("alertdialog")).to_be_hidden()
    assert dashboard["actions"] == []
    page.locator("#btnStop").click()
    page.locator("#warnSel").select_option("60")
    page.locator("#modalOk").click()
    expect(page.locator("#toasts")).to_contain_text("Тест: сервер занят")
    assert dashboard["actions"] == [{"op": "stop", "warnSeconds": 60}]


@pytest.mark.parametrize("action", ["kick", "ban"])
@pytest.mark.parametrize("name", ["Alice", "-Дмитрий-V"])
def test_player_buttons_send_exact_name(page, dashboard, action, name):
    commands = []
    dashboard["players"].update(names=[name], count=1, raw=f"-{name}")
    page.route(
        "**/api/players",
        lambda route: route.fulfill(
            json={"ok": True, "names": [name], "count": 1, "raw": f"-{name}"}
        ),
    )

    def handle_rcon(route):
        commands.append(route.request.post_data_json)
        route.fulfill(json={"ok": True, "output": "OK"})

    page.route("**/api/rcon", handle_rcon)
    page.goto(dashboard["url"])
    page.locator('.nav [data-route="players"]').click()
    expect(page.locator(".p-name")).to_have_text(name)
    page.locator(f'[data-p="{action}"]').click()
    expect(page.get_by_role("alertdialog")).to_contain_text(name)
    assert commands == []
    page.locator("#modalOk").click()
    expect(page.locator("#toasts")).to_contain_text(name)
    assert commands == [{"command": f'{action}user "{name}"'}]


def test_unavailable_api_enters_demo(page, dashboard):
    page.route("**/api/health", lambda route: route.fulfill(status=503, json={"ok": False}))
    page.goto(dashboard["url"])
    expect(page.locator("#demoBadge")).to_be_visible()
    expect(page.locator("#demoBanner")).to_be_visible()
    page.locator('.nav [data-route="console"]').click()
    expect(page.locator("#consoleOut")).to_contain_text("Демо-режим")
    assert dashboard["actions"] == []
