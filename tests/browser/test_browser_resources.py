"""Browser resource regressions using real scripts and isolated API fixtures."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("transport", ["sse", "polling"])
def test_connection_loss_never_adds_header_timer(page, dashboard, width, transport):
    page.set_viewport_size({"width": width, "height": 844})
    if transport == "sse":
        page.add_init_script("""window.EventSource = class extends EventTarget {
            constructor() { super(); window.testStream = this; }
            close() {}
        };""")
    else:
        page.add_init_script("window.EventSource = undefined;")
    page.goto(dashboard["url"])
    if transport == "sse":
        page.wait_for_function("window.testStream")
        page.evaluate(
            """data => window.testStream.dispatchEvent(
            new MessageEvent('overview', {data:JSON.stringify(data)}))""",
            dashboard["overview"],
        )
    expect(page.locator("#btnStop")).to_be_enabled()
    expect(page.locator("#connBanner")).to_be_hidden()
    header = page.locator(".topbar")
    original_text = header.inner_text()
    original_height = header.bounding_box()["height"]
    page.clock.install()
    page.clock.pause_at(page.evaluate("Date.now()"))
    if transport == "polling":
        page.route("**/api/overview", lambda route: route.abort())
    for elapsed in [15000, 45000, 60000]:
        page.clock.run_for(elapsed)
        expect(page.locator("#connBanner")).to_be_visible()
        expect(page.locator("#connBanner")).to_contain_text("Нет связи с пультом")
        expect(page.locator("#freshness, .header-meta, #clock")).to_have_count(0)
        expect(header).to_have_text(original_text, use_inner_text=True)
        assert header.bounding_box()["height"] == original_height
        assert page.locator("#connBanner").evaluate("el => !el.closest('.topbar')")
    if transport == "sse":
        page.evaluate(
            """data => window.testStream.dispatchEvent(
            new MessageEvent('overview', {data:JSON.stringify(data)}))""",
            dashboard["overview"],
        )
    else:
        page.unroute("**/api/overview")
        page.evaluate("refreshOverview()")
    expect(page.locator("#connBanner")).to_be_hidden()
    expect(header).to_have_text(original_text, use_inner_text=True)
    page.set_viewport_size({"width": 1440 if width == 390 else 390, "height": 844})
    if width == 390:
        expect(page.locator(".topbar #btnLogout")).to_be_visible()
    else:
        expect(page.locator(".health-tools #btnLogout")).to_have_count(1)
    assert dashboard["actions"] == []


def test_hidden_tab_closes_stream_and_reopens_once(page, dashboard):
    page.add_init_script("""
        window.testStreams = [];
        window.EventSource = class {
            constructor(url) { this.url = url; this.closed = false; window.testStreams.push(this); }
            addEventListener() {}
            close() { this.closed = true; }
        };
        window.testHidden = false;
        Object.defineProperty(document, 'hidden', {get: () => window.testHidden});
    """)
    page.goto(dashboard["url"])
    page.wait_for_function("window.testStreams.length === 1")
    page.evaluate("""() => {
        window.testHidden = true;
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    assert page.evaluate("window.testStreams[0].closed")
    page.evaluate("startSse()")
    assert page.evaluate("window.testStreams.length") == 1
    page.evaluate("""() => {
        window.testHidden = false;
        document.dispatchEvent(new Event('visibilitychange'));
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    assert page.evaluate("window.testStreams.length") == 2
    assert not page.evaluate("window.testStreams[1].closed")


def test_polling_has_no_overlapping_requests_and_pauses_hidden_tab(page, dashboard):
    page.goto(dashboard["url"])
    page.evaluate("""() => {
        liveSource?.close(); liveSource = null; clearTimeout(sseStartupTimer);
        S.demo = true;
        window.testPolls = [];
        window.setInterval = callback => window.testPolls.push(callback);
        window.testHidden = false;
        Object.defineProperty(document, 'hidden', {get: () => window.testHidden});
        window.overviewCalls = 0;
        refreshOverview = () => {
            window.overviewCalls++;
            return new Promise(resolve => { window.finishOverview = resolve; });
        };
        startPolling();
        startPolling();
        window.testPolls[0]();
        window.testPolls[0]();
    }""")
    assert page.evaluate("window.testPolls.length") == 10
    assert page.evaluate("window.overviewCalls") == 1
    page.evaluate("window.finishOverview()")
    page.evaluate("""() => {
        window.testHidden = true;
        window.testPolls[0]();
    }""")
    assert page.evaluate("window.overviewCalls") == 1
    page.evaluate("""() => {
        window.testHidden = false;
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    assert page.evaluate("window.overviewCalls") == 2


def test_identical_logs_keep_dom_and_recover_after_error_or_remote_mode(page, dashboard):
    # Exercise explicit frames without a periodic demo refresh replacing them.
    page.add_init_script("window.setInterval = () => 0;")
    page.goto(dashboard["url"] + "/?demo=1#/console")
    page.wait_for_function("S.logsUpdatedAt > 0")
    page.evaluate("""() => {
        window.logFrame = {ok:true,text:'2026-10-01T12:00:00Z ERROR test'};
        applyLogs(window.logFrame);
        window.firstLogNode = document.querySelector('#logsOut').firstChild;
        applyLogs({ok:false,error:'offline'});
        applyLogs(window.logFrame);
    }""")
    expect(page.locator("#logsError")).to_be_hidden()
    assert page.evaluate("document.querySelector('#logsOut').firstChild === window.firstLogNode")
    page.evaluate("""() => {
        S.overview = {...S.overview, mode:'remote'};
        applyLogs(window.logFrame);
        S.overview.mode = 'local';
        applyLogs(window.logFrame);
    }""")
    expect(page.locator("#logsOut")).to_contain_text("ERROR test")
