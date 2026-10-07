"""The shipped UI must load its assets with external network requests blocked."""

from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from test_auth import auth_server  # noqa: F401
from test_login import fill_login, login_url  # noqa: F401

pytestmark = pytest.mark.browser
FONTS = Path(__file__).resolve().parents[2] / "dashboard" / "static" / "fonts"


def assert_fonts_loaded(page):
    # Loading explicit Latin and Cyrillic samples rules out silent system fallbacks.
    faces = page.evaluate(
        """async () => {
            const families = [['Golos Text', [400, 500, 600]],
                ['JetBrains Mono', [400, 500, 700]], ['Russo One', [400]]];
            for (const [family, weights] of families) {
                for (const weight of weights) {
                    const faces = await document.fonts.load(`${weight} 16px "${family}"`,
                        'PZ Пульт Ёжик №123');
                    if (faces.length !== 2 || faces.some(face => face.status !== 'loaded')) {
                        throw new Error(`Bundled font missing: ${family} ${weight}`);
                    }
                }
            }
            await document.fonts.ready;
            return [...document.fonts].map(face => face.status);
        }"""
    )
    assert len(faces) == 6
    assert set(faces) == {"loaded"}


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_all_pages_and_fonts_load_without_external_requests(
    page,
    login_url,  # noqa: F811
    width,
    height,
    tmp_path,
):
    page.set_viewport_size({"width": width, "height": height})
    origin = urlsplit(login_url).netloc
    external = []
    failures = []
    errors = []
    requested = set()

    def local_only(route):
        url = urlsplit(route.request.url)
        if url.scheme in ("http", "https") and url.netloc != origin:
            external.append(route.request.url)
            route.abort()
        else:
            requested.add(url.path)
            route.fallback()

    # Fall back to the existing API mocks for same-origin requests.
    page.route("**/*", local_only)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "response",
        lambda response: failures.append(response.url) if response.status >= 400 else None,
    )

    page.goto(login_url + "/login")
    expect(page.get_by_role("heading", name="PZ·ПУЛЬТ")).to_be_visible()
    assert_fonts_loaded(page)
    page.screenshot(path=str(tmp_path / f"login-{width}.png"))
    fill_login(page)
    expect(page.locator("#view-overview")).to_be_visible()
    for route in (
        "overview",
        "settings",
        "mods",
        "players",
        "maintenance",
        "backups",
        "events",
        "console",
    ):
        page.goto(login_url + "/#/" + route)
        expect(page.locator(f"#view-{route}")).to_be_visible()
        assert_fonts_loaded(page)

    page.goto(login_url + "/static/config-help.html")
    expect(page.get_by_role("heading", name="Настройки, моды и восстановление")).to_be_visible()
    assert_fonts_loaded(page)
    assert "/static/fonts/fonts.css" in requested
    assert {f"/static/fonts/{font.name}" for font in FONTS.glob("*400-*.woff2")} <= requested
    assert not any("-500-" in url or "-600-" in url or "-700-" in url for url in requested)
    # Old URLs remain valid for previously cached CSS and external integrations.
    for font in FONTS.glob("*.woff2"):
        assert page.request.get(login_url + f"/static/fonts/{font.name}").status == 200
    assert not external, f"External UI requests: {external}"
    assert not failures, f"Failed asset/API responses: {failures}"
    assert not errors, f"Browser errors: {errors}"
