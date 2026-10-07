"""Reordering previews, cancellation and scroll use isolated real draft fixtures."""

from pathlib import Path
import re

import pytest
from playwright.sync_api import expect

from test_editors import editing as editing, long_order as long_order, navigate
from test_configeditor import editor, env as env


def open_order(page, dashboard, width=1440, language="ru"):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"])
    if language == "en":
        page.evaluate("localStorage.setItem('pz-language', 'en')")
        page.reload()
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.locator('#modTabs [data-tab="order"]').click()
    expect(page.locator("#modOrderList .order-row")).to_have_count(210)
    page.locator("#modOrderList .order-row").first.evaluate(
        "el => el.scrollIntoView({block: 'start', behavior: 'instant'})"
    )


def start_drag(page, mid="library"):
    grip = page.locator(f'[data-order-id="{mid}"] .order-grip')
    grip.scroll_into_view_if_needed()
    box = grip.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x, y + 8, steps=2)
    expect(page.locator(".order-drag-preview")).to_be_visible()
    return x, y


def assert_clean(page):
    expect(page.locator(".order-placeholder, .order-held, .order-drag-preview")).to_have_count(0)
    expect(page.locator("body")).not_to_have_class(re.compile(".*order-dragging.*"))


@pytest.mark.parametrize("width,language", [(1440, "ru"), (390, "en"), (320, "ru")])
def test_pointer_preview_cancel_and_drop(page, dashboard, long_order, width, language):
    data, ids, original = long_order
    open_order(page, dashboard, width, language)
    revision = editor.draft("world.ini")["draftRevision"]
    x, _ = start_drag(page)
    target = page.locator('[data-order-id="mod001"]').bounding_box()
    page.mouse.move(x, target["y"] + target["height"] - 4, steps=8)
    expect(page.locator("#modOrderList .order-row").nth(2)).to_have_attribute(
        "data-order-id", "library"
    )
    assert editor.draft("world.ini")["draftRevision"] == revision
    # Inspect the lifted row and actual gap at both desktop and phone widths.
    output = Path(__file__).resolve().parents[2] / ".tmp-reorder-evidence"
    output.mkdir(exist_ok=True)
    page.screenshot(path=str(output / f"drag-{width}-{language}.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.keyboard.press("Escape")
    page.mouse.up()
    assert_clean(page)
    expect(page.locator("#modOrderList .order-row").first).to_have_attribute(
        "data-order-id", "library"
    )
    expect(page.locator('[data-order-id="library"] .order-grip')).to_be_focused()
    assert editor.draft("world.ini")["draftRevision"] == revision
    x, _ = start_drag(page)
    target = page.locator('[data-order-id="mod001"]').bounding_box()
    page.mouse.move(x, target["y"] + target["height"] - 4, steps=8)
    page.mouse.up()
    expect(page.locator('[data-order-id="library"] .order-position')).to_have_text("3")
    assert editor.mod_response("world.ini", draft_mode=True)["mods"] == [
        "plugin",
        "mod001",
        "library",
        *ids[3:],
    ]
    assert_clean(page)
    assert (data / "Server/world.ini").read_bytes() == original.encode()
    assert dashboard["actions"] == []


@pytest.mark.parametrize("width", [390, 1440])
def test_drag_autoscroll_both_directions_and_wheel(page, dashboard, long_order, width):
    _, ids, _ = long_order
    open_order(page, dashboard, width)
    x, _ = start_drag(page)
    before = page.evaluate("scrollY")
    # Use the visible list edge above fixed navigation/footer on each device.
    bottom = page.evaluate("""() => Math.min(innerHeight,
        ...[...document.querySelectorAll('.site-footer, .nav, #draftBar')]
            .filter(el => !el.hidden && getComputedStyle(el).position === 'fixed')
            .map(el => el.getBoundingClientRect())
            .filter(box => box.width > innerWidth / 2 && box.top > innerHeight / 2)
            .map(box => box.top))""")
    page.mouse.move(x, bottom - 4, steps=4)
    page.wait_for_function("start => scrollY > start + 400", arg=before)
    index = page.locator("#modOrderList .order-row").evaluate_all(
        "rows => rows.findIndex(row => row.dataset.orderId === 'library')"
    )
    assert index > 3
    page.mouse.move(x, 450)
    before_wheel = page.evaluate("scrollY")
    page.mouse.wheel(0, 600)
    page.wait_for_function("start => scrollY > start + 300", arg=before_wheel)
    expect(page.locator(".order-drag-preview")).to_be_visible()
    before_up = page.evaluate("scrollY")
    top = page.locator(".order-toolbar").bounding_box()
    page.mouse.move(x, top["y"] + top["height"] + 2)
    page.wait_for_function("start => scrollY < start - 300", arg=before_up)
    page.keyboard.press("Escape")
    stopped = page.evaluate("scrollY")
    page.wait_for_timeout(100)
    assert page.evaluate("scrollY") == stopped
    page.mouse.up()
    assert_clean(page)
    assert editor.mod_response("world.ini", draft_mode=True)["mods"] == ids
    assert dashboard["actions"] == []
    # A second gesture commits the distant position reached by auto-scrolling.
    x, _ = start_drag(page)
    before = page.evaluate("scrollY")
    page.mouse.move(x, bottom - 4)
    page.wait_for_function("start => scrollY > start + 400", arg=before)
    page.mouse.move(x, 450)
    page.wait_for_timeout(100)
    destination = page.locator("#modOrderList .order-row").evaluate_all(
        "rows => rows.findIndex(row => row.dataset.orderId === 'library')"
    )
    page.mouse.up()
    expect(page.locator('[data-order-id="library"] .order-position')).to_have_text(
        str(destination + 1)
    )
    result = editor.mod_response("world.ini", draft_mode=True)["mods"]
    expected = ids[1:]
    expected.insert(destination, "library")
    assert result == expected
    assert_clean(page)


def test_keyboard_reorder_cancel_reduced_motion_and_filtered_positions(page, dashboard, long_order):
    _, ids, _ = long_order
    page.emulate_media(reduced_motion="reduce")
    open_order(page, dashboard)
    page.locator("#orderQuery").fill("mod00")
    grip = page.locator('[data-order-id="mod001"] .order-grip')
    revision = editor.draft("world.ini")["draftRevision"]
    grip.focus()
    grip.press("Space")
    expect(grip).to_have_attribute("aria-pressed", "true")
    grip.press("ArrowDown")
    expect(page.locator('[data-order-id="mod001"] .order-position')).to_have_text("4")
    assert (
        page.locator("#modOrderList").evaluate("el => el.getAnimations({subtree: true}).length")
        == 0
    )
    grip.press("Escape")
    expect(grip).to_be_focused()
    assert editor.draft("world.ini")["draftRevision"] == revision
    grip.press("Enter")
    grip.press("End")
    grip.press("Enter")
    expect(page.locator('[data-order-id="mod001"] .order-position')).to_have_text("11")
    assert editor.mod_response("world.ini", draft_mode=True)["mods"] == [
        *ids[:2],
        *ids[3:11],
        "mod001",
        *ids[11:],
    ]
    expect(grip).to_be_focused()
    expect(page.locator("#orderQuery")).to_have_value("mod00")
    assert_clean(page)


@pytest.mark.parametrize("cancel", ["pointercancel", "route", "tab", "blur"])
def test_interrupted_drag_restores_order(page, dashboard, long_order, cancel):
    _, ids, _ = long_order
    open_order(page, dashboard)
    x, y = start_drag(page)
    page.mouse.move(x, y + 190)
    if cancel == "pointercancel":
        page.locator('[data-order-id="library"] .order-grip').dispatch_event(
            "pointercancel", {"pointerId": 1}
        )
    elif cancel == "route":
        page.evaluate("location.hash = '#/overview'")
        expect(page.locator("#view-overview")).to_be_visible()
    elif cancel == "tab":
        page.locator('#modTabs [data-tab="composition"]').evaluate("el => el.click()")
    else:
        page.evaluate("window.dispatchEvent(new Event('blur'))")
    page.mouse.up()
    assert_clean(page)
    assert editor.mod_response("world.ini", draft_mode=True)["mods"] == ids


def test_touch_drag_and_keyboard_focus_change_cancel(page, dashboard, long_order):
    _, ids, _ = long_order
    open_order(page, dashboard, 390)
    grip = page.locator('[data-order-id="library"] .order-grip')
    assert grip.evaluate("el => getComputedStyle(el).touchAction") == "none"
    assert (
        page.locator("#modOrderList").evaluate("el => getComputedStyle(el).touchAction") == "auto"
    )
    session = page.context.new_cdp_session(page)
    box = grip.bounding_box()
    x, y = box["x"] + 22, box["y"] + 22
    session.send(
        "Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]}
    )
    session.send(
        "Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y + 12}]}
    )
    expect(page.locator(".order-drag-preview")).to_be_visible()
    target = page.locator('[data-order-id="mod001"]').bounding_box()
    session.send(
        "Input.dispatchTouchEvent",
        {"type": "touchMove", "touchPoints": [{"x": x, "y": target["y"] + target["height"] / 2}]},
    )
    session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    expect(page.locator('[data-order-id="library"] .order-position')).to_have_text("3")
    assert editor.mod_response("world.ini", draft_mode=True)["mods"] == [
        "plugin",
        "mod001",
        "library",
        *ids[3:],
    ]
    assert_clean(page)
    grip.press("Space")
    grip.press("ArrowDown")
    page.locator("#orderQuery").focus()
    assert_clean(page)
    expect(page.locator("#orderQuery")).to_be_focused()
    expect(page.locator('[data-order-id="library"] .order-position')).to_have_text("3")


def test_drop_stays_in_place_while_saving_and_failed_save_restores_order(
    page, dashboard, long_order
):
    _, ids, _ = long_order
    open_order(page, dashboard)
    pending = []

    def hold_save(route):
        if route.request.method == "POST":
            pending.append(route)
        else:
            route.fallback()

    page.route("**/api/config-draft", hold_save)
    x, _ = start_drag(page)
    box = page.locator('[data-order-id="mod001"]').bounding_box()
    page.mouse.move(x, box["y"] + box["height"] - 4, steps=5)
    page.mouse.up()
    expect(page.locator('[data-order-id="library"] .order-position')).to_have_text("3")
    expect(page.locator("#draftSaved")).to_have_text("Сохраняется черновик…")
    assert_clean(page)
    assert len(pending) == 1
    assert editor.mod_response("world.ini", draft_mode=True)["mods"] == ids
    pending.pop().fulfill(status=409, json={"ok": False, "error": "Тест: сохранение не удалось"})
    expect(page.locator('[data-order-id="library"] .order-position')).to_have_text("1")
    expect(page.locator("#configError")).to_have_text("Тест: сохранение не удалось")
    expect(page.locator('[data-order-id="library"] .order-grip')).to_be_focused()
    assert editor.mod_response("world.ini", draft_mode=True)["mods"] == ids
