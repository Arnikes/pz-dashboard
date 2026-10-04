"""Browser resource regressions using real scripts and isolated API fixtures."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


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
    page.goto(dashboard["url"] + "/?demo=1#/console")
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
