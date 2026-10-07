"""Keep wide workspaces balanced and draft actions aligned when the sidebar changes."""

from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_editors import editing, env  # noqa: F401

pytestmark = pytest.mark.browser
EVIDENCE = Path(__file__).resolve().parents[2] / ".tmp-ultrawide"
ROUTES = ("overview", "settings", "mods", "players", "maintenance", "backups", "events", "console")


def assert_workspace_alignment(page, *, draft=False):
    # Capture one layout: live status updates can hide a forced banner between
    # separate locator calls, making nth() wait for a node that is no longer visible.
    layout = page.evaluate(
        """draft => {
        document.querySelectorAll('.banner').forEach(node => node.hidden = false);
        const rect = node => {
            const {x, width, y, height} = node.getBoundingClientRect();
            return {x, width, y, height};
        };
        const main = document.querySelector('.main');
        const viewport = document.documentElement.clientWidth;
        return {
            viewport,
            sidebar: viewport > 740 ? rect(document.querySelector('.nav')).width : 0,
            main: rect(main),
            gutter: parseFloat(getComputedStyle(main).paddingLeft),
            footer: rect(document.querySelector('.site-footer-inner')),
            boxes: [
                ...[...document.querySelectorAll('.banner')].map(node => ({selector:'.banner', ...rect(node)})),
                ...(draft ? [{selector:'#draftBar', ...rect(document.getElementById('draftBar'))}] : []),
            ],
            overflows: document.documentElement.scrollWidth > innerWidth,
        };
    }""",
        draft,
    )
    viewport, sidebar, main = layout["viewport"], layout["sidebar"], layout["main"]
    assert main["width"] <= 1450
    left_gap = main["x"] - sidebar
    right_gap = viewport - main["x"] - main["width"]
    assert abs(left_gap - right_gap) <= 1, (left_gap, right_gap)
    gutter = layout["gutter"]
    content_left = main["x"] + gutter
    content_width = main["width"] - 2 * gutter
    footer = layout["footer"]
    assert abs(footer["x"] - main["x"]) <= 1
    assert abs(footer["width"] - main["width"]) <= 1
    for bounds in layout["boxes"]:
        assert abs(bounds["x"] - content_left) <= 1, (bounds, main)
        assert abs(bounds["width"] - content_width) <= 1, (bounds, main)
    assert not layout["overflows"]


@pytest.mark.parametrize(
    "width,height",
    [
        (320, 640),
        (390, 844),
        (834, 1024),
        (1440, 900),
        (1920, 1080),
        (2560, 1080),
        (3440, 1440),
        (5120, 1440),
    ],
)
def test_all_routes_share_centered_workspace(page, dashboard, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(dashboard["url"])
    EVIDENCE.mkdir(exist_ok=True)
    # Desktop exercise both navigation preferences; mobile keeps its bottom bar.
    for state in range(2 if width > 740 else 1):
        if state:
            page.locator("#navToggle").click()
        for route in ROUTES:
            page.evaluate("route => location.hash = '#/' + route", route)
            expect(page.locator(f"#view-{route}")).to_be_visible()
            page.evaluate("""() => {
                scrollTo(0, 0);
                document.querySelectorAll('.banner').forEach(el => el.hidden = false);
            }""")
            assert_workspace_alignment(page)
            if route in ("overview", "console") and width in (390, 1440, 3440, 5120):
                # All banners above are forced visible to check their bounds;
                # render the ordinary fixture state for visual evidence.
                page.evaluate(
                    "document.querySelectorAll('.banner').forEach(el => el.hidden = true)"
                )
                page.screenshot(path=str(EVIDENCE / f"{route}-{width}-{state}.png"))


@pytest.mark.parametrize("width,height", [(1440, 900), (3440, 1440), (3440, 540)])
@pytest.mark.parametrize("language", ["ru", "en"])
def test_saved_draft_follows_workspace_after_resize_and_sidebar_toggle(
    page,
    dashboard,
    editing,  # noqa: F811
    width,
    height,
    language,
):
    page.context.add_cookies([{"name": "pz_language", "value": language, "url": dashboard["url"]}])
    page.set_viewport_size({"width": width, "height": height})
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    expect(page.locator("#configProfile")).to_be_enabled()
    expect(page.locator('[data-key="PublicName"]')).to_be_enabled()
    page.locator('[data-key="PublicName"]').fill("Ultrawide draft")
    page.locator('[data-key="PublicName"]').press("Tab")
    expect(page.locator("#draftSaved")).to_have_text(
        "Draft saved" if language == "en" else "Черновик сохранён"
    )
    EVIDENCE.mkdir(exist_ok=True)
    for state in range(2):
        if state:
            page.locator("#navToggle").click()
        assert_workspace_alignment(page, draft=True)
        bar = page.locator("#draftBar").bounding_box()
        if height > 600:
            assert bar["y"] + bar["height"] < page.locator(".site-footer").bounding_box()["y"]
        else:
            assert (
                page.locator("#draftBar").evaluate("el => getComputedStyle(el).position")
                == "relative"
            )
            page.locator("#draftBar").scroll_into_view_if_needed()
        if width == 3440:
            page.screenshot(
                path=str(EVIDENCE / f"settings-{language}-{width}-{height}-{state}.png")
            )
    # Resizing a real saved draft keeps the mobile controls in the same viewport.
    page.set_viewport_size({"width": 390, "height": 844})
    assert_workspace_alignment(page, draft=True)
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Ultrawide draft")
    assert dashboard["actions"] == []
