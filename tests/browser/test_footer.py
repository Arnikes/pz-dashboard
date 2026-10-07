"""Keep the fixed footer reachable without covering navigation or page content."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844), (320, 640), (844, 390)])
def test_footer_stays_visible_on_short_and_scrolling_pages(page, dashboard, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(dashboard["url"] + "/#/players")
    footer = page.get_by_role("contentinfo")
    expect(footer).to_contain_text("© 2026 Arnike")
    link = footer.get_by_role("link", name="GitHub")
    expect(link).to_have_attribute("href", "https://github.com/Arnikes/pz-dashboard")
    expect(link).to_have_attribute("rel", "noopener noreferrer")
    expect(link).to_have_attribute("target", "_blank")
    for route in ["players", "maintenance"]:
        page.evaluate("route => location.hash = '#/' + route", route)
        expect(page.locator(f"#view-{route}")).to_be_visible()
        for scroll in [0, 100000]:
            page.evaluate("y => scrollTo(0, y)", scroll)
            box = footer.bounding_box()
            nav = page.locator(".nav").bounding_box()
            expected_bottom = nav["y"] if width <= 740 else height
            assert abs(box["y"] + box["height"] - expected_bottom) <= 1
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        last = page.locator(f"#view-{route}").bounding_box()
        assert last["y"] + last["height"] <= box["y"]
    link.focus()
    expect(link).to_be_focused()
    evidence = Path(__file__).resolve().parents[2] / ".tmp-footer-render"
    evidence.mkdir(exist_ok=True)
    page.screenshot(path=str(evidence / f"footer-{width}-{height}.png"))
