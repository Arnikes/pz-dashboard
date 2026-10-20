"""Player history hover details stay inside the chart without widening the page."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from browser_evidence import capture_evidence

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("width", [320, 390, 1024, 1440])
def test_history_tooltip_fits_at_both_edges(page, dashboard, language, width):
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 900})
    page.route(
        "**/api/players/history",
        lambda route: route.fulfill(
            json={
                "ok": True,
                "points": [
                    {"ts": "2026-10-12T12:00:00Z", "count": 0},
                    {"ts": "2026-10-12T12:04:00Z", "count": 128},
                ],
            }
        ),
    )
    page.goto(dashboard["url"] + "/#/players")
    expect(page.locator("#startupLoader")).to_be_hidden()
    chart = page.locator("#phSpark")
    expect(chart.locator("path").first).to_be_visible()
    chart.scroll_into_view_if_needed()
    page.evaluate("document.fonts.ready")
    tip = page.locator("#phTip")
    bounds = page.locator(".ph-wrap").bounding_box()
    box = chart.bounding_box()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    for fraction in [1, 0, 0.4, 0.6]:
        chart.hover(position={"x": max(1, fraction * (box["width"] - 1)), "y": box["height"] / 2})
        expect(tip).to_be_visible()
        expect(tip).to_contain_text("· 128 " if fraction > 0.5 else "· 0 ")
        if fraction == 1:
            output = Path(".tmp-pytest/review-evidence")
            output.mkdir(parents=True, exist_ok=True)
            capture_evidence(page, path=str(output / f"players-tooltip-{language}-{width}.png"))
        tooltip = tip.bounding_box()
        assert tooltip["x"] >= bounds["x"] - 0.5
        assert tooltip["x"] + tooltip["width"] <= bounds["x"] + bounds["width"] + 0.5
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

    page.mouse.move(box["x"], box["y"] + box["height"] + 10)
    expect(tip).to_be_hidden()
