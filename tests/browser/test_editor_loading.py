"""Editor requests show progress without moving the existing settings layout."""

import pytest
from playwright.sync_api import expect

from test_editors import editing, env  # noqa: F401


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("width", [390, 1440])
def test_settings_loading_preserves_layout(page, dashboard, editing, language, width):  # noqa: F811
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    profiles = []
    page.route("**/api/server-configs", lambda route: profiles.append(route))
    page.goto(dashboard["url"] + "/#/settings")
    progress = page.locator("#settingsLoading")
    expect(page.locator("#startupLoader")).to_be_visible()
    expect(progress).to_have_attribute("role", "progressbar")
    expect(progress).to_have_attribute(
        "aria-label", "Loading…" if language == "en" else "Загрузка…"
    )
    expect(page.locator("#view-settings")).to_have_attribute("aria-busy", "true")
    assert len(profiles) == 1
    profiles.pop().fallback()
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect(progress).to_be_hidden()
    field = page.locator('[data-key="PublicName"]')
    expect(field).to_be_enabled()
    expect(field).to_have_value("Сервер")

    def bounds():
        return page.evaluate("""() => {
            const selectors = ['.editor-card', '#configTabs', '#configSearch', '#configFields'];
            return selectors.map(selector => {
                const {x, y, width, height} = document.querySelector('#view-settings ' + selector).getBoundingClientRect();
                return {x, y, width, height};
            });
        }""")

    before = bounds()
    requests = []
    page.route("**/api/config-draft?*", lambda route: requests.append(route))
    page.route("**/api/mods?*", lambda route: requests.append(route))
    page.evaluate("ConfigEditor.route('overview'); ConfigEditor.route('settings')")
    expect(progress).to_be_visible()
    expect(field).to_be_disabled()
    assert bounds() == before
    assert page.evaluate("getComputedStyle(settingsLoading, '::after').height") == "3px"
    assert progress.evaluate("""el => {
        const layer = getComputedStyle(el), card = getComputedStyle(el.parentElement);
        return layer.borderRadius === card.borderRadius && layer.overflow === 'hidden';
    }""")
    assert page.evaluate("getComputedStyle(settingsLoading).position") == "absolute"
    assert page.evaluate("getComputedStyle(settingsLoading, '::after').animationName") == "none"
    for message in (
        "Profile loading. Wait for the draft.",
        "Профиль загружается. Дождитесь получения черновика.",
    ):
        expect(page.get_by_text(message, exact=True)).to_have_count(0)
    page.screenshot(path=str(editing[0].parent / f"settings-loading-{language}-{width}.png"))
    assert len(requests) == 1
    # The bar remains while the same profile's Workshop metadata is in flight.
    with page.expect_request("**/api/mods?*"):
        requests.pop().fallback()
    expect(progress).to_be_visible()
    assert bounds() == before
    assert len(requests) == 1
    requests.pop().fallback()
    expect(progress).to_be_hidden()
    expect(page.locator("#view-settings")).to_have_attribute("aria-busy", "false")
    expect(field).to_be_enabled()
    assert bounds() == before
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    page.emulate_media(reduced_motion="no-preference")
    with page.expect_request("**/api/config-draft?*"):
        page.evaluate("ConfigEditor.route('overview'); ConfigEditor.route('settings')")
    expect(progress).to_be_visible()
    assert (
        page.evaluate("getComputedStyle(settingsLoading, '::after').animationName")
        == "editor-loading"
    )
    assert len(requests) == 1
    requests.pop().fulfill(status=503, json={"ok": False, "error": "Profile unavailable"})
    expect(progress).to_be_hidden()
    expect(page.locator("#view-settings")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#configError")).to_have_text("Profile unavailable")
    expect(page.locator("#configError")).to_be_visible()
