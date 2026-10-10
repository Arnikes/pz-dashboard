"""Recovery before notice, retry countdown, and foreground connection races."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def open_connection(page, dashboard, transport="sse", language="ru"):
    page.clock.install(time="2026-01-01T00:00:00Z")
    page.clock.pause_at("2026-01-01T00:00:01Z")
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.add_init_script(
        """window.testHidden = false;
      Object.defineProperty(document, 'hidden', {get: () => window.testHidden});
      window.testStreams = [];
      window.EventSource = class extends EventTarget {
        constructor() { super(); testStreams.push(this); }
        close() { this.closed = true; }
      };"""
        if transport == "sse"
        else """window.EventSource = undefined;
      window.testHidden = false;
      Object.defineProperty(document, 'hidden', {get: () => window.testHidden});"""
    )
    page.goto(dashboard["url"])
    expect(page.locator("#startupLoader")).to_be_hidden()
    if transport == "sse":
        page.wait_for_function("testStreams.length === 1")
        send_overview(page, dashboard)
    expect(page.locator("#btnStop")).to_be_enabled()


def send_overview(page, dashboard):
    page.evaluate(
        """data => testStreams.at(-1).dispatchEvent(
      new MessageEvent('overview', {data: JSON.stringify(data)}))""",
        dashboard["overview"],
    )


def visibility(page, hidden):
    page.evaluate(
        """hidden => {
      testHidden = hidden;
      document.dispatchEvent(new Event('visibilitychange'));
    }""",
        hidden,
    )


def hold_overviews(page):
    held = []

    def hold(route):
        held.append(route)
        # The attempt starts before fetch reaches Python's route callback.
        # Publish receipt so browser waits also pump the interception handler.
        page.evaluate("count => window.testHeldOverviewCount = count", len(held))

    page.route("**/api/overview", hold)
    return held


def wait_for_overviews(page, count):
    page.wait_for_function("count => window.testHeldOverviewCount === count", arg=count)


@pytest.mark.parametrize("transport", ["sse", "polling"])
def test_foreground_checks_fresh_data_without_flashing_notice(page, dashboard, transport):
    open_connection(page, dashboard, transport)
    held = hold_overviews(page)
    visibility(page, True)
    page.clock.run_for(60000)
    assert not held
    expect(page.locator("#connBanner")).to_be_hidden()
    visibility(page, False)
    page.wait_for_function("!!connectionAttempt")
    wait_for_overviews(page, 1)
    expect(page.locator("#connBanner")).to_be_hidden()
    assert len(held) == 1
    # Repeated foreground signals must share the same recovery attempt.
    visibility(page, False)
    page.evaluate("window.dispatchEvent(new Event('online'))")
    assert len(held) == 1
    held[0].fulfill(json=dashboard["overview"])
    page.wait_for_function("connectionAttempt === null")
    expect(page.locator("#connBanner")).to_be_hidden()
    assert page.evaluate("connectionTimer === null")
    if transport == "sse":
        assert page.evaluate("testStreams.length") == 2
    assert dashboard["actions"] == []


@pytest.mark.parametrize("transport", ["sse", "polling"])
@pytest.mark.parametrize("language", ["ru", "en"])
def test_failed_recovery_countdown_manual_retry_and_success(page, dashboard, transport, language):
    open_connection(page, dashboard, transport, language)
    held = hold_overviews(page)
    visibility(page, True)
    visibility(page, False)
    page.wait_for_function("!!connectionAttempt")
    wait_for_overviews(page, 1)
    expect(page.locator("#connBanner")).to_be_hidden()
    held[0].fulfill(status=503, json={"ok": False, "error": "Unavailable"})
    notice = page.locator("#connBanner")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text(
        "Console disconnected" if language == "en" else "Нет связи с пультом"
    )
    expect(page.locator("#connRetry > span").first).to_have_text("Retry")
    expect(page.locator(".retry-seconds")).to_have_text("10")
    page.clock.run_for(4000)
    expect(page.locator(".retry-seconds")).to_have_text("6")
    assert float(
        notice.evaluate("el => el.style.getPropertyValue('--retry-progress')")
    ) == pytest.approx(0.4)
    assert len(held) == 1
    page.locator("#connRetry").click()
    page.wait_for_function("!!connectionAttempt")
    wait_for_overviews(page, 2)
    expect(page.locator("#connRetry")).to_be_disabled()
    expect(notice).to_have_attribute("aria-busy", "true")
    assert len(held) == 2
    held[1].abort()
    expect(page.locator(".retry-seconds")).to_have_text("10")
    page.clock.run_for(9900)
    assert len(held) == 2
    with page.expect_request("**/api/overview"):
        page.clock.run_for(100)
    page.wait_for_function("!!connectionAttempt")
    wait_for_overviews(page, 3)
    assert len(held) == 3
    held[2].fulfill(json=dashboard["overview"])
    expect(notice).to_be_hidden()
    assert page.evaluate("connectionTimer === null && connectionAttempt === null")
    assert dashboard["actions"] == []


def test_hidden_window_discards_failed_inflight_attempt(page, dashboard):
    open_connection(page, dashboard)
    held = hold_overviews(page)
    page.evaluate("void retryConnection()")
    page.wait_for_function("!!connectionAttempt")
    wait_for_overviews(page, 1)
    visibility(page, True)
    held[0].abort()
    page.clock.run_for(30000)
    expect(page.locator("#connBanner")).to_be_hidden()
    assert page.evaluate("connectionTimer === null && connectionAttempt === null")
    visibility(page, False)
    page.wait_for_function("!!connectionAttempt")
    wait_for_overviews(page, 2)
    assert len(held) == 2
    held[1].fulfill(json=dashboard["overview"])
    expect(page.locator("#connBanner")).to_be_hidden()


def test_fresh_stream_cancels_probe_and_invalid_frames_do_not_hide_notice(page, dashboard):
    open_connection(page, dashboard)
    held = hold_overviews(page)
    page.evaluate("void retryConnection()")
    page.wait_for_function("!!connectionAttempt")
    wait_for_overviews(page, 1)
    send_overview(page, dashboard)
    held[0].abort()
    expect(page.locator("#connBanner")).to_be_hidden()
    page.evaluate("void retryConnection()")
    page.wait_for_function("!!connectionAttempt")
    wait_for_overviews(page, 2)
    held[1].abort()
    expect(page.locator("#connBanner")).to_be_visible()
    page.evaluate("""() => {
      testStreams.at(-1).dispatchEvent(new MessageEvent('overview', {data: '{'}));
      testStreams.at(-1).dispatchEvent(new MessageEvent('overview', {data: '{"ok":false}'}));
    }""")
    expect(page.locator("#connBanner")).to_be_visible()
    send_overview(page, dashboard)
    expect(page.locator("#connBanner")).to_be_hidden()


def test_failed_boot_health_checks_overview_before_notice(page, dashboard):
    page.route("**/api/health", lambda route: route.abort())
    held = hold_overviews(page)
    page.goto(dashboard["url"])
    page.wait_for_function("!!connectionAttempt")
    wait_for_overviews(page, 1)
    expect(page.locator("#connBanner")).to_be_hidden()
    assert len(held) == 1
    held[0].abort()
    expect(page.locator("#connBanner")).to_be_visible()


def test_silent_probe_timeout_and_hidden_countdown(page, dashboard):
    open_connection(page, dashboard)
    held = hold_overviews(page)
    page.evaluate("void retryConnection()")
    page.wait_for_function("!!connectionAttempt")
    wait_for_overviews(page, 1)
    page.clock.run_for(4999)
    expect(page.locator("#connBanner")).to_be_hidden()
    page.clock.run_for(1)
    expect(page.locator("#connBanner")).to_be_visible()
    expect(page.locator(".retry-seconds")).to_have_text("10")
    visibility(page, True)
    page.clock.run_for(30000)
    assert len(held) == 1
    assert page.evaluate("connectionTimer === null")
    expect(page.locator("#connBanner")).to_be_hidden()


@pytest.mark.parametrize("width,height", [(320, 900), (390, 550), (1440, 900)])
@pytest.mark.parametrize("motion", ["reduce", "no-preference"])
def test_shared_alerts_stack_and_clear_mobile_controls(
    page, dashboard, tmp_path, width, height, motion
):
    page.set_viewport_size({"width": width, "height": height})
    page.emulate_media(reduced_motion=motion)
    open_connection(page, dashboard, language="en")
    page.route("**/api/overview", lambda route: route.abort())
    page.evaluate("void retryConnection()")
    expect(page.locator("#connBanner")).to_be_visible()
    page.evaluate("""() => {
      const update = document.querySelector('.pwa-update-alert');
      update.hidden = false;
      toast('Companion notification', 'ok', 120000);
    }""")
    page.wait_for_function("""() => {
      const a = document.querySelector('#connBanner').getBoundingClientRect();
      const b = document.querySelector('.pwa-update-alert').getBoundingClientRect();
      return a.bottom <= b.top || b.bottom <= a.top;
    }""")
    boxes = page.locator(".floating-alert").evaluate_all("""async elements => {
      await document.fonts.ready;
      await Promise.all(elements.flatMap(el => el.getAnimations()).map(a => a.finished));
      return elements.map(el => el.getBoundingClientRect().toJSON());
    }""")
    assert all(box["x"] >= 0 and box["right"] <= width for box in boxes)
    assert page.locator("#connRetry").bounding_box()["height"] >= 44
    if width <= 740:
        page.wait_for_function("""() => document.querySelector('.toast').getBoundingClientRect().bottom
          < Math.min(...[...document.querySelectorAll('.floating-alert')].map(el => el.getBoundingClientRect().top))""")
        footer_top = page.locator(".site-footer").bounding_box()["y"]
        assert all(box["bottom"] < footer_top for box in boxes)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"connection-{width}-{height}-{motion}.png"))
