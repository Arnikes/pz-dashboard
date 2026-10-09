"""Responsive sidebar behavior, persistence, and keyboard access."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [741, 834, 1024, 1180, 1181, 1440])
def test_sidebar_defaults_toggle_and_layout(page, dashboard, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"])
    toggle = page.locator("#navToggle")
    collapsed = width <= 1180
    expect(toggle).to_have_attribute("aria-expanded", str(not collapsed).lower())
    sidebar = page.locator(".nav")
    # Sidebar attributes initialize before the startup shell reveals its layout.
    expect(sidebar).to_be_visible()
    assert sidebar.bounding_box()["width"] == (72 if collapsed else 204)
    # Hidden visual labels retain accessible names and native hover tooltips.
    settings = sidebar.get_by_role("link", name="Настройки", exact=True)
    expect(settings).to_have_attribute("title", "Настройки")
    toggle.focus()
    toggle.press("Enter")
    expect(toggle).to_be_focused()
    expect(toggle).to_have_attribute("aria-expanded", str(collapsed).lower())
    assert sidebar.bounding_box()["width"] == (204 if collapsed else 72)
    toggle.press("Space")
    expect(toggle).to_have_attribute("aria-expanded", str(not collapsed).lower())
    settings.click()
    expect(page.locator("#view-settings")).to_be_visible()
    expect(settings).to_have_attribute("aria-current", "page")
    # All fixed workspace elements follow the sidebar's new width.
    # Capture one layout frame: live editor updates can hide the draft bar
    # between separate browser calls, even after it was explicitly shown.
    boxes = page.evaluate("""() => {
        document.getElementById('draftBar').hidden = false;
        return Object.fromEntries(['.main', '#draftBar', '.site-footer', '.nav'].map(selector => {
            const {x, width} = document.querySelector(selector).getBoundingClientRect();
            return [selector, {x, width}];
        }));
    }""")
    assert boxes[".main"]["x"] == (72 if collapsed else 204)
    for selector in ["#draftBar", ".site-footer"]:
        box = boxes[selector]
        assert box["x"] >= boxes[".nav"]["width"]
        assert box["x"] + box["width"] <= width
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=f".tmp-sidebar/navigation-{width}.png")


def test_sidebar_preferences_survive_reload_and_resize_without_changing_mobile(page, dashboard):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(dashboard["url"])
    toggle = page.locator("#navToggle")
    toggle.click()
    page.reload()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    page.set_viewport_size({"width": 834, "height": 900})
    expect(toggle).to_have_attribute("aria-expanded", "false")
    toggle.click()
    page.reload()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    page.screenshot(path=".tmp-sidebar/tablet-expanded.png")
    page.set_viewport_size({"width": 1440, "height": 900})
    expect(toggle).to_have_attribute("aria-expanded", "false")
    page.screenshot(path=".tmp-sidebar/desktop-collapsed.png")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(toggle).to_be_hidden()
    nav = page.locator(".nav")
    assert nav.bounding_box()["width"] == 390
    assert nav.locator("a:visible").count() == 3
    page.locator("#navMore").click()
    expect(page.locator("#moreMenu")).to_be_visible()
    page.get_by_role("link", name="Консоль", exact=True).click()
    expect(page.locator("#view-console")).to_be_visible()
    expect(page.locator("#moreMenu")).to_be_hidden()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=".tmp-sidebar/mobile.png")
    page.set_viewport_size({"width": 834, "height": 900})
    expect(toggle).to_have_attribute("aria-expanded", "true")


def test_sidebar_works_without_browser_storage_in_english(page, dashboard):
    page.add_init_script("""Object.defineProperty(window, 'localStorage', {
        get() { throw new Error('Storage unavailable'); }
    });""")
    page.context.add_cookies([{"name": "pz_language", "value": "en", "url": dashboard["url"]}])
    page.set_viewport_size({"width": 1024, "height": 900})
    page.goto(dashboard["url"])
    toggle = page.get_by_role("button", name="Expand navigation", exact=True)
    toggle.click()
    expect(page.locator("#navToggle")).to_have_attribute("aria-label", "Collapse navigation")
    expect(page.locator('.nav a[data-route="settings"]')).to_have_attribute("title", "Settings")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.set_viewport_size({"width": 1024, "height": 900})
    expect(page.locator("#navToggle")).to_have_attribute("aria-expanded", "true")
