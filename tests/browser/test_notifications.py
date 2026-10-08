"""Unified feedback, durable read receipts, and notification center interaction."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser

HISTORY = [
    {
        "op": "backup",
        "finishedAt": "2026-10-08T12:02:00Z",
        "ok": False,
        "message": "Disk unavailable",
    },
    {
        "op": "restart",
        "finishedAt": "2026-10-08T12:01:00Z",
        "ok": True,
        "message": "Server restarted",
    },
]


def render_history(page, history=HISTORY):
    page.evaluate("history => renderOp({active: null, history})", history)


def test_read_results_do_not_repeat_after_replay_reload_or_clear(page, dashboard):
    page.route("**/api/ops", lambda route: route.fulfill(json={"active": None, "history": HISTORY}))
    page.goto(dashboard["url"])
    expect(page.locator("#notificationCount")).to_have_text("2")
    expect(page.locator(".toast")).to_have_count(0)
    page.locator("#btnNotifications").click()
    expect(page.locator(".notification-item")).to_have_count(2)
    expect(page.locator("#notificationCount")).to_be_hidden()
    expect(page.locator('.notification-item[data-read="true"]')).to_have_count(2)
    page.locator("#notificationClose").click()
    expect(page.locator("#btnNotifications")).to_be_focused()
    render_history(page)
    page.reload()
    expect(page.locator("#btnStop")).to_be_enabled()
    render_history(page)
    expect(page.locator(".toast")).to_have_count(0)
    expect(page.locator("#notificationCount")).to_be_hidden()
    page.locator("#btnNotifications").click()
    expect(page.locator(".notification-item")).to_have_count(2)
    page.locator("#notificationClear").click()
    expect(page.locator(".notification-item")).to_have_count(0)
    page.reload()
    expect(page.locator("#btnStop")).to_be_enabled()
    render_history(page)
    page.locator("#btnNotifications").click()
    expect(page.locator(".notification-item")).to_have_count(0)
    expect(page.locator("#notificationEmpty")).to_be_visible()
    expect(page.locator("#notificationClear")).to_be_disabled()


def test_new_operations_and_toasts_share_inbox_without_duplicate_results(page, dashboard):
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    render_history(page)
    render_history(page)
    expect(page.locator(".toast")).to_have_count(2)
    page.evaluate("toast('Settings saved', 'ok'); toast('Settings saved', 'ok')")
    expect(page.locator("#notificationCount")).to_have_text("4")
    page.locator("#btnNotifications").click()
    expect(page.locator(".notification-item")).to_have_count(4)
    expect(page.locator(".toast")).to_have_count(0)
    page.locator('[data-notification-filter="error"]').click()
    expect(page.locator(".notification-item")).to_have_count(1)
    expect(page.locator("#notificationList")).to_contain_text("Disk unavailable")
    page.locator('[data-notification-filter="ok"]').click()
    expect(page.locator(".notification-item")).to_have_count(3)
    page.locator('[data-notification-filter="unread"]').click()
    expect(page.locator(".notification-item")).to_have_count(0)
    page.locator("#notificationClose").click()
    # A new run with the same message is a new result; a cancelled run is not an error.
    new_run = dict(HISTORY[0], finishedAt="2026-10-08T12:03:00Z", cancelled=True)
    render_history(page, [new_run, *HISTORY])
    expect(page.locator("#notificationCount")).to_have_text("1")
    expect(page.locator('.toast[data-kind="warning"]')).to_have_count(1)
    page.locator(".toast-close").click()
    expect(page.locator("#notificationCount")).to_be_hidden()
    render_history(page, [new_run, *HISTORY])
    expect(page.locator(".toast")).to_have_count(0)


def test_center_keyboard_filters_and_mark_all_when_new_feedback_arrives(page, dashboard):
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    page.locator("#btnNotifications").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#btnNotifications")).to_have_attribute("aria-expanded", "true")
    page.evaluate("toast('<img src=x onerror=alert(1)>', 'error')")
    expect(page.locator("#notificationCount")).to_have_text("1")
    expect(page.locator("#notificationList img")).to_have_count(0)
    page.locator("#notificationReadAll").click()
    expect(page.locator("#notificationCount")).to_be_hidden()
    expect(page.locator(".toast")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(page.locator("#btnNotifications")).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#btnNotifications")).to_be_focused()
    page.locator("#btnNotifications").click()
    page.mouse.click(1, 1)
    expect(page.locator("#notificationCenter")).not_to_be_visible()


def test_identical_same_second_runs_use_server_ids(page, dashboard):
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    runs = [dict(HISTORY[0], id="run-a"), dict(HISTORY[0], id="run-b")]
    render_history(page, runs)
    render_history(page, runs)
    expect(page.locator("#notificationCount")).to_have_text("2")
    page.locator("#btnNotifications").click()
    expect(page.locator(".notification-item")).to_have_count(2)
    page.locator("#notificationClear").click()
    render_history(page, [dict(run, message="Translated server message") for run in runs])
    expect(page.locator(".notification-item")).to_have_count(0)


def test_clear_removes_toasts_beyond_inbox_retention(page, dashboard):
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    page.evaluate("for (let i = 0; i < 105; i++) toast(`Message ${i}`, 'info', 120000)")
    expect(page.locator("#notificationCount")).to_have_text("99+")
    page.locator("#btnNotifications").click()
    expect(page.locator(".notification-item")).to_have_count(100)
    # Reading the retained inbox removes its toasts; the oldest five are queued.
    expect(page.locator(".toast")).to_have_count(5)
    page.locator("#notificationClear").click()
    expect(page.locator(".toast")).to_have_count(0)
    expect(page.locator(".notification-item")).to_have_count(0)


def test_read_and_clear_propagate_to_other_tabs(page, dashboard):
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    other = page.context.new_page()
    other.route("**/api/**", lambda route: route.fulfill(json={"ok": True, "items": []}))
    try:
        other.goto(dashboard["url"])
        render_history(page)
        expect(other.locator("#notificationCount")).to_have_text("2")
        other.locator("#btnNotifications").click()
        expect(page.locator("#notificationCount")).to_be_hidden()
        expect(page.locator(".toast")).to_have_count(0)
        other.locator("#notificationClear").click()
        render_history(page)
        page.locator("#btnNotifications").click()
        expect(page.locator(".notification-item")).to_have_count(0)
    finally:
        other.close()


@pytest.mark.parametrize("width,height", [(390, 360), (1280, 400)])
def test_center_remains_scrollable_in_short_windows(page, dashboard, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    render_history(page)
    page.locator("#btnNotifications").click()
    panel = page.locator("#notificationCenter").bounding_box()
    assert panel["y"] + panel["height"] <= height
    page.locator("#notificationClear").click()
    expect(page.locator(".notification-item")).to_have_count(0)


@pytest.mark.parametrize("store", ["blocked", "invalid"])
def test_unavailable_or_corrupt_storage_keeps_feedback_usable(page, dashboard, store):
    if store == "blocked":
        page.add_init_script("Storage.prototype.setItem = () => { throw new Error('blocked') }")
    else:
        page.add_init_script("localStorage.setItem('pz-notifications-v1', '{broken')")
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    render_history(page)
    page.locator("#btnNotifications").click()
    expect(page.locator(".notification-item")).to_have_count(2)
    page.locator("#notificationClear").click()
    render_history(page)
    expect(page.locator(".notification-item")).to_have_count(0)


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("width", [320, 390, 1440])
def test_center_layout_and_localization(page, dashboard, language, width):
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    render_history(page)
    page.locator("#btnNotifications").click()
    expect(page.locator("#notificationHeading")).to_have_text(
        "Notifications" if language == "en" else "Уведомления"
    )
    expect(page.locator("#notificationClear")).to_have_text(
        "Clear all" if language == "en" else "Очистить все"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    panel = page.locator("#notificationCenter").bounding_box()
    assert panel["x"] >= 0 and panel["x"] + panel["width"] <= width
    assert panel["y"] >= 0 and panel["y"] + panel["height"] < 900
    assert page.locator("#notificationClear").bounding_box()["height"] >= 44
    out = Path(".tmp-notification-evidence")
    out.mkdir(exist_ok=True)
    page.screenshot(path=str(out / f"center-{language}-{width}.png"))
    page.locator("#notificationClose").click()
    render_history(page, [dict(HISTORY[0], finishedAt="2026-10-08T12:03:00Z")])
    page.evaluate("toast('Settings saved', 'ok')")
    page.locator('.toast[data-front="true"] .toast-close').focus()
    page.wait_for_function("!document.getAnimations().some(a => a.playState === 'running')")
    page.screenshot(path=str(out / f"toast-{language}-{width}.png"))
