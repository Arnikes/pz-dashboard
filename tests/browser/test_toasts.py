"""Toast reading time and stack interactions, with a controlled browser clock."""

from datetime import datetime, timezone

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def open_toasts(page, dashboard):
    page.clock.install(time=datetime(2030, 1, 1, tzinfo=timezone.utc))
    page.clock.pause_at(datetime(2030, 1, 1, 0, 1, tzinfo=timezone.utc))
    page.goto(dashboard["url"])
    page.mouse.move(0, 0)
    page.evaluate("""() => {
      window.toastWindowFocused = true;
      window.toastDocumentHidden = false;
      document.hasFocus = () => window.toastWindowFocused;
      Object.defineProperty(document, 'hidden', {get: () => window.toastDocumentHidden});
      window.dispatchEvent(new Event('focus'));
    }""")


def test_toast_counts_only_remaining_focused_time(page, dashboard):
    open_toasts(page, dashboard)
    page.evaluate("toast('Короткое сообщение', 'ok', 1000)")
    page.clock.run_for(400)
    page.evaluate("toastWindowFocused = false; window.dispatchEvent(new Event('blur'))")
    page.clock.run_for(10000)
    expect(page.locator(".toast")).to_have_count(1)
    page.evaluate("toast('Сообщение в фоне', 'error', 2000)")
    page.clock.run_for(10000)
    expect(page.locator(".toast")).to_have_count(2)
    page.evaluate("toastWindowFocused = true; window.dispatchEvent(new Event('focus'))")
    page.clock.run_for(1999)
    expect(page.locator(".toast")).to_have_count(2)
    page.clock.run_for(1)
    expect(page.locator(".toast")).to_have_count(1)
    page.clock.run_for(599)
    expect(page.locator(".toast")).to_have_count(1)
    page.clock.run_for(1)
    expect(page.locator(".toast")).to_have_count(0)


def test_hidden_tab_pauses_even_if_window_has_focus(page, dashboard):
    open_toasts(page, dashboard)
    page.evaluate("toast('Не пропустить', 'info', 1000)")
    page.clock.run_for(250)
    page.evaluate(
        "toastDocumentHidden = true; document.dispatchEvent(new Event('visibilitychange'))"
    )
    page.clock.run_for(10000)
    expect(page.locator(".toast")).to_have_count(1)
    page.evaluate(
        "toastDocumentHidden = false; document.dispatchEvent(new Event('visibilitychange'))"
    )
    page.clock.run_for(749)
    expect(page.locator(".toast")).to_have_count(1)
    page.clock.run_for(1)
    expect(page.locator(".toast")).to_have_count(0)


def test_hover_resumes_remaining_time_without_stuck_toasts(page, dashboard):
    open_toasts(page, dashboard)
    page.evaluate("toast('Прочитать сообщение', 'info', 1000)")
    page.clock.run_for(400)
    page.locator(".toast").hover()
    page.clock.run_for(10000)
    expect(page.locator(".toast")).to_have_count(1)
    page.mouse.move(0, 0)
    page.clock.run_for(599)
    expect(page.locator(".toast")).to_have_count(1)
    page.clock.run_for(1)
    expect(page.locator(".toast")).to_have_count(0)


def test_stack_waits_for_each_message_and_keyboard_dismissal_keeps_focus(page, dashboard):
    open_toasts(page, dashboard)
    page.locator("#btnCommands").focus()
    page.evaluate("for (let i = 0; i < 5; i++) toast(`Сообщение ${i}`, 'info', 1000)")
    expect(page.locator('.toast[data-front="true"]')).to_have_text("Сообщение 4")
    assert page.locator('.toast[data-hidden="true"]').count() == 2
    page.clock.run_for(1000)
    expect(page.locator(".toast")).to_have_count(4)
    expect(page.locator('.toast[data-front="true"]')).to_have_text("Сообщение 3")
    page.locator('.toast[data-front="true"] button').focus()
    expect(page.locator("#toasts")).to_have_attribute("data-expanded", "true")
    page.clock.run_for(10000)
    expect(page.locator(".toast")).to_have_count(4)
    page.keyboard.press("Enter")
    expect(page.locator(".toast")).to_have_count(3)
    expect(page.locator('.toast[data-front="true"] button')).to_be_focused()
    for _ in range(3):
        page.keyboard.press("Enter")
    expect(page.locator(".toast")).to_have_count(0)
    expect(page.locator("#btnCommands")).to_be_focused()


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_stack_layout_long_messages_and_expansion(page, dashboard, width):
    page.set_viewport_size({"width": width, "height": 900})
    open_toasts(page, dashboard)
    page.evaluate("""() => {
      toast('Проверка модов завершена', 'ok');
      toast('Изменения записаны. Перезапустите сервер, чтобы применить настройки.', 'info');
      toast('Не удалось выполнить операцию: ' + 'WorkshopID'.repeat(20), 'error');
    }""")
    page.clock.run_for(200)
    boxes = page.locator(".toast").evaluate_all(
        "nodes => nodes.map(n => n.getBoundingClientRect().toJSON())"
    )
    assert boxes[0]["y"] < boxes[1]["y"] < boxes[2]["y"]
    assert boxes[0]["width"] < boxes[1]["width"] < boxes[2]["width"]
    assert all(box["x"] >= 0 and box["right"] <= width for box in boxes)
    page.screenshot(path=f".tmp-toast-evidence/stack-{width}.png")
    page.locator('.toast[data-front="true"] button').focus()
    page.clock.run_for(200)
    boxes = page.locator(".toast").evaluate_all(
        "nodes => nodes.map(n => n.getBoundingClientRect().toJSON())"
    )
    assert all(a["bottom"] <= b["top"] for a, b in zip(boxes, boxes[1:]))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=f".tmp-toast-evidence/expanded-{width}.png")


def test_touch_can_expand_stack_and_outside_tap_resumes_timer(browser, dashboard):
    context = browser.new_context(has_touch=True, viewport={"width": 390, "height": 900})
    page = context.new_page()
    page.route("**/api/**", lambda route: route.fulfill(json={"ok": True, "items": []}))
    try:
        open_toasts(page, dashboard)
        page.evaluate("toast('Первое', 'info', 1000); toast('Второе', 'info', 1000)")
        page.locator('.toast[data-front="true"] .toast-message').tap()
        expect(page.locator("#toasts")).to_have_attribute("data-expanded", "true")
        page.clock.run_for(10000)
        expect(page.locator(".toast")).to_have_count(2)
        page.touchscreen.tap(5, 5)
        expect(page.locator("#toasts")).to_have_attribute("data-expanded", "false")
        page.clock.run_for(1000)
        expect(page.locator(".toast")).to_have_count(1)
    finally:
        context.close()
