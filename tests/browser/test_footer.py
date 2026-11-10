"""Keep the fixed footer reachable without covering navigation or page content."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from browser_evidence import capture_evidence

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844), (320, 640), (844, 390)])
def test_footer_stays_visible_on_short_and_scrolling_pages(page, dashboard, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(dashboard["url"] + "/#/players")
    expect(page.locator("#startupLoader")).to_be_hidden()
    page.evaluate("document.fonts.ready")
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
            geometry = page.evaluate(
                """({route, y}) => {
                    scrollTo(0, y);
                    return {
                        footer: document.querySelector('.site-footer').getBoundingClientRect().toJSON(),
                        nav: document.querySelector('.nav').getBoundingClientRect().toJSON(),
                        view: document.getElementById('view-' + route).getBoundingClientRect().toJSON(),
                        fits: document.documentElement.scrollWidth <= innerWidth,
                    };
                }""",
                {"route": route, "y": scroll},
            )
            box = geometry["footer"]
            nav = geometry["nav"]
            expected_bottom = nav["y"] if width <= 740 else height
            assert abs(box["y"] + box["height"] - expected_bottom) <= 1
            assert geometry["fits"]
        # SSE/status updates can grow the page between browser round trips.
        # Compare the view and footer from the same scroll/layout observation.
        last = geometry["view"]
        assert last["y"] + last["height"] <= box["y"]
    link.focus()
    expect(link).to_be_focused()
    evidence = Path(__file__).resolve().parents[2] / ".tmp-footer-render"
    evidence.mkdir(exist_ok=True)
    capture_evidence(page, path=str(evidence / f"footer-{width}-{height}.png"))
