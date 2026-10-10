"""A caching proxy must not pair new markup with scripts for removed controls."""

import pytest
from playwright.sync_api import expect

import auth
from test_editors import editing as editing, env as env
from test_pwa_browser import pwa_server as pwa_server

pytestmark = pytest.mark.browser_context_args(service_workers="allow")


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("language", ["en", "ru"])
def test_release_reloads_editor_and_styles_behind_sticky_proxy(
    page,
    context,
    pwa_server,
    editing,
    language,
    width,
):
    page.set_viewport_size({"width": width, "height": 844})
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    token = pwa_server["auth"].sign_in("pwa-admin", "pwa-test-password-long", "fixture")
    context.add_cookies([{"name": auth.COOKIE_NAME, "value": token, "url": pwa_server["url"]}])
    root = pwa_server["root"]
    html = root / "index.html"
    editor = root / "editor.js"
    style = root / "style.css"
    original_html = html.read_text(encoding="utf-8")
    original_editor = editor.read_text(encoding="utf-8")
    html.write_text(
        original_html.replace(
            '<script src="/static/catalogs.js">',
            '<button id="legacyDraftAction" hidden></button><script src="/static/catalogs.js">',
            1,
        ),
        encoding="utf-8",
    )
    # The old release binds a control that the next release removes.
    # Put it before initialization to reproduce the permanent Loading state.
    editor.write_text(
        "document.getElementById('legacyDraftAction').addEventListener('click', () => {});\n"
        + original_editor,
        encoding="utf-8",
    )
    pwa_server["server"].asset_cache = {}
    page.goto(pwa_server["url"] + "/#/settings")
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    old_editor_url = page.locator('script[src*="/editor.js"]').get_attribute("src")
    old_style_url = page.locator('link[href*="/style.css"]').get_attribute("href")

    html.write_text(original_html, encoding="utf-8")
    editor.write_text(original_editor, encoding="utf-8")
    style.write_text(
        style.read_text(encoding="utf-8") + "\nbody { --asset-release: two; }", encoding="utf-8"
    )
    page.reload()
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    expect(page.locator('[data-key="PublicName"]')).to_be_enabled()
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Сервер")
    new_editor_url = page.locator('script[src*="/editor.js"]').get_attribute("src")
    new_style_url = page.locator('link[href*="/style.css"]').get_attribute("href")
    assert new_editor_url != old_editor_url
    assert new_style_url != old_style_url
    assert {old_editor_url, new_editor_url, old_style_url, new_style_url}.issubset(
        pwa_server["server"].asset_cache
    )
    assert (
        page.locator("body").evaluate(
            "body => getComputedStyle(body).getPropertyValue('--asset-release').trim()"
        )
        == "two"
    )
    page.screenshot(path=f".tmp-pytest/review-evidence/profile-fixed-{language}-{width}.png")
    page.locator('.nav [data-route="mods"]').click()
    expect(page.locator("#view-mods")).to_be_visible()
    expect(page.locator("#modsLoading")).to_be_hidden()
    expect(page.locator("#modPackages details")).to_have_count(1)
    expect(page.locator("#modPackages")).to_contain_text("Package name differs")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=f".tmp-pytest/review-evidence/mods-fixed-{language}-{width}.png")
