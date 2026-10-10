"""Toast reading time and stack interactions, with a controlled browser clock."""

from datetime import datetime, timezone

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def open_toasts(page, dashboard):
    page.clock.install(time=datetime(2030, 1, 1, tzinfo=timezone.utc))
    page.clock.pause_at(datetime(2030, 1, 1, 0, 1, tzinfo=timezone.utc))
    page.goto(dashboard["url"])
    expect(page.locator("#startupLoader")).to_be_hidden()
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


def test_long_feedback_gets_reading_time_and_named_dismiss_control(page, dashboard):
    open_toasts(page, dashboard)
    message = "Long feedback. " * 40
    page.evaluate("message => toast(message, 'error')", message)
    expect(page.locator(".toast-close")).to_have_attribute(
        "aria-label", f"Закрыть уведомление: {message}"
    )
    page.clock.run_for(19999)
    expect(page.locator(".toast")).to_have_count(1)
    page.clock.run_for(1)
    expect(page.locator(".toast")).to_have_count(0)
    page.evaluate("message => toast(message, 'error', 1000)", message)
    page.clock.run_for(1000)
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


def settled_toast_boxes(page):
    # The mocked JS clock does not advance CSS animations or transitions.
    return page.locator("#toasts").evaluate("""async root => {
      await Promise.all(root.getAnimations({subtree: true}).map(animation => animation.finished));
      return [...root.children].map(node => node.getBoundingClientRect().toJSON());
    }""")


@pytest.mark.parametrize("width", [320, 390, 1440])
@pytest.mark.parametrize("motion", ["no-preference", "reduce"])
def test_stack_layout_long_messages_and_expansion(page, dashboard, width, motion):
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion=motion)
    open_toasts(page, dashboard)
    page.evaluate("""() => {
      toast('Проверка модов завершена', 'ok');
      toast('Изменения записаны. Перезапустите сервер, чтобы применить настройки.', 'info');
      toast('Не удалось выполнить операцию: ' + 'WorkshopID'.repeat(20), 'error');
    }""")
    boxes = settled_toast_boxes(page)
    assert boxes[0]["y"] < boxes[1]["y"] < boxes[2]["y"]
    assert boxes[0]["width"] < boxes[1]["width"] < boxes[2]["width"]
    assert all(box["x"] >= 0 and box["right"] <= width for box in boxes)
    page.screenshot(path=f".tmp-toast-evidence/stack-{width}.png")
    page.locator('.toast[data-front="true"] button').focus()
    boxes = settled_toast_boxes(page)
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


def toast_motion_frame(page, time):
    return page.locator("#toasts").evaluate(
        """(root, time) => {
          for (const animation of root.getAnimations({subtree: true})) {
            animation.pause();
            animation.currentTime = time;
          }
          return [...root.children].map(node => node.getBoundingClientRect().toJSON());
        }""",
        time,
    )


def freeze_next_toast_transition(page, event):
    # Pause in the event's own task: under parallel load the transition can finish
    # before the next Playwright command reaches the browser.
    page.locator("#toasts").evaluate(
        """(root, event) => root.addEventListener(event, () => {
          const animations = root.getAnimations({subtree: true});
          document.querySelectorAll('.toast-exit').forEach(box => animations.push(...box.getAnimations()));
          animations.forEach(animation => { animation.pause(); animation.currentTime = 0; });
        }, {once: true})""",
        event,
    )


@pytest.mark.parametrize("width", [390, 1440])
def test_expansion_moves_up_from_stack_and_keeps_front_anchored(page, dashboard, width):
    page.set_viewport_size({"width": width, "height": 900})
    open_toasts(page, dashboard)
    page.evaluate("""() => {
      toast('Длинное старое сообщение. '.repeat(8), 'warning');
      toast('Короткое сообщение', 'ok');
      toast('Самое новое сообщение', 'info');
    }""")
    collapsed = settled_toast_boxes(page)
    freeze_next_toast_transition(page, "focusin")
    page.locator('.toast[data-front="true"] button').focus()
    start = toast_motion_frame(page, 0)
    middle = toast_motion_frame(page, 100)
    end = toast_motion_frame(page, 240)
    for before, after in zip(collapsed, start):
        for coordinate in ("x", "y", "width", "height"):
            assert abs(before[coordinate] - after[coordinate]) < 1
    for index in range(2):
        assert end[index]["bottom"] < middle[index]["bottom"] < start[index]["bottom"]
    assert all(
        abs(boxes[-1]["bottom"] - collapsed[-1]["bottom"]) < 1 for boxes in (start, middle, end)
    )
    assert all(a["bottom"] <= b["top"] for a, b in zip(end, end[1:]))
    page.screenshot(path=f".tmp-toast-evidence/motion-expanded-{width}.png")


def test_interrupted_expansion_and_collapse_keep_current_position(page, dashboard):
    open_toasts(page, dashboard)
    page.evaluate("for (let i = 0; i < 5; i++) toast(`Сообщение ${i}`, 'info')")
    settled_toast_boxes(page)
    freeze_next_toast_transition(page, "focusin")
    page.locator('.toast[data-front="true"] button').focus()
    before = toast_motion_frame(page, 70)
    freeze_next_toast_transition(page, "keydown")
    page.keyboard.press("Escape")
    after = toast_motion_frame(page, 0)
    # The visible rear cards reverse direction without jumping to either endpoint.
    for index in (2, 3, 4):
        assert abs(before[index]["y"] - after[index]["y"]) < 1
        assert abs(before[index]["width"] - after[index]["width"]) < 1
    before = toast_motion_frame(page, 70)
    freeze_next_toast_transition(page, "focusin")
    page.locator('.toast[data-front="true"] button').focus()
    after = toast_motion_frame(page, 0)
    for index in (2, 3, 4):
        assert abs(before[index]["y"] - after[index]["y"]) < 1


def test_overflowing_stack_opens_at_newest_and_can_scroll_to_oldest(page, dashboard):
    page.set_viewport_size({"width": 390, "height": 600})
    open_toasts(page, dashboard)
    page.evaluate("for (let i = 0; i < 12; i++) toast(`Сообщение ${i}`, 'info')")
    settled_toast_boxes(page)
    page.locator('.toast[data-front="true"] button').focus()
    settled_toast_boxes(page)
    measurements = page.locator("#toasts").evaluate("""root => {
      const bounds = root.getBoundingClientRect();
      const front = root.lastElementChild.getBoundingClientRect();
      return {top: bounds.top, bottom: bounds.bottom, frontTop: front.top, frontBottom: front.bottom,
        scroll: root.scrollTop, scrollHeight: root.scrollHeight, height: root.clientHeight};
    }""")
    assert measurements["scrollHeight"] > measurements["height"]
    assert measurements["scroll"] > 0
    assert measurements["top"] <= measurements["frontTop"]
    assert measurements["frontBottom"] <= measurements["bottom"] + 1
    assert measurements["top"] >= page.locator(".topbar").bounding_box()["height"]
    page.evaluate("toast('Новое сообщение в раскрытой стопке', 'ok')")
    settled_toast_boxes(page)
    expect(page.locator('.toast[data-front="true"] .toast-close')).to_be_in_viewport()
    page.locator("#toasts").evaluate("root => root.scrollTop = 0")
    page.evaluate("toast('Новое сообщение во время чтения старых', 'info')")
    settled_toast_boxes(page)
    assert page.locator("#toasts").evaluate("root => root.scrollTop") == 0
    expect(page.locator(".toast-close").first).to_be_in_viewport()


def test_dismissing_scrolled_out_message_does_not_flash_outside_stack(page, dashboard):
    page.set_viewport_size({"width": 390, "height": 600})
    open_toasts(page, dashboard)
    page.evaluate("for (let i = 0; i < 12; i++) toast(`Сообщение ${i}`, 'info')")
    settled_toast_boxes(page)
    page.locator('.toast[data-front="true"] button').focus()
    settled_toast_boxes(page)
    assert (
        page.locator(".toast").first.bounding_box()["y"]
        < page.locator("#toasts").bounding_box()["y"]
    )
    page.locator(".toast-close").first.evaluate("button => button.click()")
    expect(page.locator(".toast-exit")).to_have_count(0)
    expect(page.locator(".toast")).to_have_count(11)


def test_escape_returns_focus_and_resumes_remaining_reading_time(page, dashboard):
    open_toasts(page, dashboard)
    page.locator("#btnCommands").focus()
    page.evaluate("toast('Первое', 'info', 1000); toast('Второе', 'ok', 1000)")
    page.clock.run_for(400)
    page.locator('.toast[data-front="true"] button').focus()
    page.clock.run_for(10000)
    page.keyboard.press("Escape")
    expect(page.locator("#btnCommands")).to_be_focused()
    expect(page.locator("#toasts")).to_have_attribute("data-expanded", "false")
    page.clock.run_for(599)
    expect(page.locator(".toast")).to_have_count(2)
    page.clock.run_for(1)
    expect(page.locator(".toast")).to_have_count(1)


@pytest.mark.parametrize("motion", ["no-preference", "reduce"])
def test_dismissal_reflows_remaining_cards_and_exit_cannot_receive_input(page, dashboard, motion):
    page.emulate_media(reduced_motion=motion)
    open_toasts(page, dashboard)
    page.locator("#btnCommands").focus()
    page.evaluate("toast('Первое', 'info'); toast('Второе', 'ok'); toast('Третье', 'warning')")
    page.locator('.toast[data-front="true"] button').focus()
    before = settled_toast_boxes(page)
    freeze_next_toast_transition(page, "click")
    page.keyboard.press("Enter")
    expect(page.locator(".toast")).to_have_count(2)
    expect(page.locator('.toast[data-front="true"] button')).to_be_focused()
    if motion == "no-preference":
        start = toast_motion_frame(page, 0)
        assert abs(start[0]["y"] - before[0]["y"]) < 1
        assert page.locator(".toast-exit").evaluate(
            "box => box.inert && box.getAttribute('aria-hidden') === 'true'"
        )
        page.locator(".toast-exit").evaluate(
            "box => box.getAnimations().forEach(animation => animation.finish())"
        )
    else:
        assert (
            page.locator("#toasts").evaluate("root => root.getAnimations({subtree: true}).length")
            == 0
        )
    expect(page.locator(".toast-exit")).to_have_count(0)


def test_single_toast_touch_does_not_pause_until_an_outside_tap(browser, dashboard):
    context = browser.new_context(has_touch=True, viewport={"width": 390, "height": 900})
    page = context.new_page()
    page.route("**/api/**", lambda route: route.fulfill(json={"ok": True, "items": []}))
    try:
        open_toasts(page, dashboard)
        page.evaluate("toast('Сообщение', 'info', 1000)")
        page.locator(".toast-message").tap()
        expect(page.locator("#toasts")).to_have_attribute("data-expanded", "false")
        page.clock.run_for(1000)
        expect(page.locator(".toast")).to_have_count(0)
    finally:
        context.close()
