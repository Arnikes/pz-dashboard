"""Console command delivery and log reading against the shipped browser UI."""

import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def open_console(page, dashboard, language="ru", width=1440):
    # Explicit fixtures own every update; background polling must not overwrite
    # a held response or a simulated connection state during these scenarios.
    page.add_init_script("window.setInterval = () => 0;")
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(dashboard["url"] + "/#/console")
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect(page.locator("#consoleInput")).to_be_enabled()
    page.evaluate("liveSource?.close(); liveSource=null; clearTimeout(sseStartupTimer)")


def at_bottom(locator):
    return locator.evaluate("el => el.scrollHeight - el.clientHeight - el.scrollTop <= 2")


def test_pending_command_serializes_controls_and_keeps_its_response(page, dashboard):
    held = []

    def hold(route):
        held.append(route)
        page.evaluate("count => window.heldRconCount = count", len(held))

    page.route("**/api/rcon", hold)
    open_console(page, dashboard)
    page.locator("#consoleInput").fill('servermsg "One delivery"')
    page.locator("#consoleInput").press("Enter")
    page.wait_for_function("window.heldRconCount === 1")
    entry = page.locator("#consoleOut .console-entry")
    expect(entry).to_have_count(1)
    expect(entry).to_have_attribute("data-state", "pending")
    expect(entry.locator(".console-command")).to_contain_text('servermsg "One delivery"')
    for control in ["#consoleForm button", "#consoleInput", "#btnSaveWorld"]:
        expect(page.locator(control)).to_be_disabled()
    for chip in page.locator("#quickCmds button").all():
        expect(chip).to_be_disabled()
    # A queued submit must also be rejected after the visible controls lock.
    page.locator("#consoleForm").evaluate("el => {el.requestSubmit(); el.requestSubmit();}")
    assert len(held) == 1
    held[0].fulfill(json={"ok": True, "output": "Message delivered"})
    expect(entry).to_have_attribute("data-state", "success")
    expect(entry.locator(".console-response")).to_have_text("Message delivered")
    expect(page.locator("#consoleInput")).to_be_enabled()
    assert held[0].request.post_data_json == {"command": 'servermsg "One delivery"'}


def test_console_new_response_preserves_transcript_reading_position(page, dashboard):
    replies = []

    def command(route):
        replies.append(route.request.post_data_json)
        output = (
            "\n".join(f"Help entry {i}" for i in range(100))
            if len(replies) == 1
            else "Latest answer"
        )
        route.fulfill(json={"ok": True, "output": output})

    page.route("**/api/rcon", command)
    open_console(page, dashboard)
    page.locator("#consoleInput").fill("help")
    page.locator("#consoleInput").press("Enter")
    expect(page.locator("#consoleOut")).to_contain_text("Help entry 99")
    output = page.locator("#consoleOut")
    output.evaluate("el => el.scrollTop=50")
    before = output.evaluate("el => el.scrollTop")
    page.locator("#consoleInput").fill("players")
    page.locator("#consoleInput").press("Enter")
    expect(output).to_contain_text("Latest answer")
    assert abs(output.evaluate("el => el.scrollTop") - before) <= 1


def test_rejected_form_keeps_draft_and_quick_commands_preserve_it(page, dashboard):
    commands = []

    def command(route):
        commands.append(route.request.post_data_json["command"])
        if len(commands) == 1:
            route.fulfill(status=409, json={"ok": False, "error": "Server busy"})
        else:
            route.fulfill(json={"ok": True, "output": "Saved"})

    page.route("**/api/rcon", command)
    open_console(page, dashboard)
    draft = 'servermsg "Keep this text"'
    page.locator("#consoleInput").fill(draft)
    page.locator("#consoleInput").press("Enter")
    expect(page.locator("#consoleOut .console-entry")).to_have_attribute("data-state", "error")
    expect(page.locator("#consoleInput")).to_have_value(draft)
    page.locator('#quickCmds [data-cmd="save"]').click()
    expect(page.locator("#consoleOut .console-entry").last).to_have_attribute(
        "data-state", "success"
    )
    expect(page.locator("#consoleInput")).to_have_value(draft)
    page.locator("#consoleInput").fill("players")
    page.locator("#consoleInput").press("Enter")
    expect(page.locator("#consoleOut .console-entry").last).to_have_attribute(
        "data-state", "success"
    )
    page.locator("#consoleInput").fill(draft)
    assert commands == [draft, "save", "players"]
    # Down at the end of history must not replace a newly typed draft.
    page.locator("#consoleInput").press("ArrowDown")
    expect(page.locator("#consoleInput")).to_have_value(draft)
    page.locator("#consoleInput").press("ArrowUp")
    expect(page.locator("#consoleInput")).to_have_value("players")
    page.locator("#consoleInput").press("ArrowDown")
    expect(page.locator("#consoleInput")).to_have_value(draft)
    assert draft not in page.evaluate("JSON.stringify([localStorage, sessionStorage])")


@pytest.mark.parametrize("language", ["ru", "en"])
def test_successful_empty_response_is_localized(page, dashboard, language):
    page.route("**/api/rcon", lambda route: route.fulfill(json={"ok": True, "output": ""}))
    open_console(page, dashboard, language=language)
    page.locator("#consoleInput").fill("save")
    page.locator("#consoleInput").press("Enter")
    expect(page.locator("#consoleOut .console-entry")).to_have_attribute("data-state", "success")
    expect(page.locator("#consoleOut .console-response")).to_have_text(
        "(без ответа)" if language == "ru" else "(no response)"
    )


def test_open_announcement_rechecks_operation_lock_and_keeps_text(page, dashboard):
    commands = []
    page.route(
        "**/api/rcon",
        lambda route: (
            commands.append(route.request.post_data_json),
            route.fulfill(json={"ok": True, "output": "Announced"}),
        ),
    )
    open_console(page, dashboard)
    page.locator("#consoleInput").fill("Unsent draft")
    page.locator('#quickCmds [data-act="broadcast"]').click()
    page.locator("#bcText").fill("Server restarts later")
    # An external operation can arrive while an administrator is composing.
    page.evaluate("S.actionPending=true; updateButtons()")
    page.locator("#modalOk").click()
    expect(page.locator("#modalError")).to_be_visible()
    expect(page.locator("#bcText")).to_have_value("Server restarts later")
    assert commands == []
    page.evaluate("S.actionPending=false; updateButtons()")
    page.locator("#modalOk").click()
    expect(page.locator("#modalRoot")).to_be_hidden()
    expect(page.locator("#consoleInput")).to_have_value("Unsent draft")
    expect(page.locator("#consoleOut .console-response")).to_have_text("Announced")
    assert commands == [{"command": 'servermsg "Server restarts later"'}]


def test_player_action_shares_pending_guard_and_preserves_console_draft(page, dashboard):
    dashboard["players"].update(names=["Maple"], count=1)
    held = []
    page.route("**/api/rcon", lambda route: held.append(route))
    open_console(page, dashboard)
    page.locator("#consoleInput").fill("Unsent draft")
    page.evaluate("location.hash='#/players'")
    page.locator('[data-p="kick"]').click()
    page.locator("#paReason").fill("Test reason")
    page.locator("#modalOk").click()
    expect(page.locator("#modalOk")).to_be_disabled()
    expect(page.locator('[data-p="kick"]')).to_be_disabled()
    page.locator("#consoleForm").evaluate("el => el.requestSubmit()")
    expect(page.locator("#consoleOut .console-entry")).to_have_attribute("data-state", "pending")
    assert len(held) == 1
    held[0].fulfill(json={"ok": True, "output": "Kicked Maple"})
    expect(page.locator("#modalRoot")).to_be_hidden()
    expect(page.locator("#consoleInput")).to_have_value("Unsent draft")
    expect(page.locator("#consoleOut .console-response")).to_have_text("Kicked Maple")
    assert held[0].request.post_data_json == {"command": 'kickuser "Maple" "Test reason"'}


@pytest.mark.parametrize("failure", ["network", "rejected", "malformed", "server", "missing-ok"])
def test_unconfirmed_command_has_unknown_result_and_never_retries(page, dashboard, failure):
    commands = []

    def command(route):
        commands.append(route.request.post_data_json)
        if failure == "network":
            route.abort()
        elif failure == "malformed":
            route.fulfill(content_type="application/json", body="{invalid")
        elif failure == "server":
            route.fulfill(status=503, json={"ok": False, "error": "Upstream failed"})
        elif failure == "missing-ok":
            route.fulfill(json={"output": "Unconfirmed"})
        else:
            route.fulfill(json={"ok": False, "error": "RCON response incomplete"})

    page.route("**/api/rcon", command)
    open_console(page, dashboard, language="en")
    page.locator("#consoleInput").fill('servermsg "Send once"')
    page.locator("#consoleInput").press("Enter")
    entry = page.locator("#consoleOut .console-entry")
    expect(entry).to_have_attribute("data-state", "unknown")
    expect(entry.locator(".console-response")).to_contain_text(
        re.compile("result|outcome|may have|possibly|unknown", re.I)
    )
    expect(page.locator("#consoleInput")).to_be_enabled()
    # Recovery of monitoring data must not repeat a possibly executed command.
    page.evaluate("refreshOverview()")
    expect(entry).to_have_attribute("data-state", "unknown")
    assert commands == [{"command": 'servermsg "Send once"'}]


def test_rcon_waits_past_general_api_timeout_then_reports_unknown(page, dashboard):
    held = []
    page.clock.install()
    page.route("**/api/rcon", lambda route: held.append(route))
    open_console(page, dashboard, language="en")
    page.locator("#consoleInput").fill("save")
    page.locator("#consoleInput").press("Enter")
    entry = page.locator("#consoleOut .console-entry")
    expect(entry).to_have_attribute("data-state", "pending")
    page.clock.run_for(10000)
    expect(entry).to_have_attribute("data-state", "pending")
    page.clock.run_for(21000)
    expect(entry).to_have_attribute("data-state", "unknown")
    expect(page.locator("#consoleInput")).to_be_enabled()
    assert len(held) == 1


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_last_rcon_failure_allows_an_explicit_attempt_without_claiming_ready(page, dashboard, mode):
    commands = []
    page.route(
        "**/api/rcon",
        lambda route: (
            commands.append(route.request.post_data_json),
            route.fulfill(json={"ok": True, "output": "Recovered"}),
        ),
    )
    open_console(page, dashboard)
    frame = {**dashboard["overview"], "mode": mode, "rcon": {"state": "error", "error": "Offline"}}
    page.evaluate("frame => applyOverview(frame)", frame)
    expect(page.locator("#consoleInput")).to_be_enabled()
    expect(page.locator("#consoleOut")).not_to_contain_text("Консоль доступна")
    expect(page.locator("#consoleAvailability")).to_contain_text("RCON")
    page.locator("#consoleInput").fill("players")
    page.locator("#consoleInput").press("Enter")
    expect(page.locator("#consoleOut .console-response")).to_have_text("Recovered")
    assert commands == [{"command": "players"}]


@pytest.mark.parametrize("blocker", ["stopped", "unconfigured", "operation", "demo"])
def test_blocked_console_keeps_draft_and_sends_nothing(page, dashboard, blocker):
    commands = []
    page.route("**/api/rcon", lambda route: commands.append(route.request.post_data_json))
    open_console(page, dashboard)
    page.locator("#consoleInput").fill("players")
    if blocker == "operation":
        page.evaluate("S.actionPending=true; updateButtons()")
    elif blocker == "demo":
        page.evaluate("S.demo=true; updateButtons()")
    else:
        frame = {**dashboard["overview"]}
        if blocker == "stopped":
            frame["containerInfo"] = {"running": False}
        else:
            frame["rconConfigured"] = False
        page.evaluate("frame => applyOverview(frame)", frame)
    expect(page.locator("#consoleInput")).to_be_disabled()
    page.locator("#consoleForm").evaluate("el => el.requestSubmit()")
    expect(page.locator("#consoleInput")).to_have_value("players")
    assert commands == []


@pytest.mark.parametrize("change_period", [False, True])
def test_late_log_response_cannot_replace_newer_snapshot(page, dashboard, change_period):
    page.route(
        "**/api/logs**", lambda route: route.fulfill(json={"ok": True, "text": "INFO initial"})
    )
    open_console(page, dashboard)
    expect(page.locator("#logsOut")).to_contain_text("initial")
    held = []

    def hold(route):
        held.append(route)
        page.evaluate("count => window.heldLogCount=count", len(held))

    page.route("**/api/logs**", hold)
    page.evaluate("() => {window.oldLogRefresh=refreshLogs();}")
    page.wait_for_function("window.heldLogCount === 1")
    if change_period:
        page.evaluate("S.logsSince=Date.parse('2026-11-10T12:00:00Z')")
    page.evaluate("() => {window.newLogRefresh=refreshLogs();}")
    page.wait_for_function("window.heldLogCount === 2")
    held[1].fulfill(json={"ok": True, "text": "2026-11-10T12:01:00Z INFO current snapshot"})
    page.evaluate("async () => {await window.newLogRefresh}")
    expect(page.locator("#logsOut")).to_contain_text("current snapshot")
    held[0].fulfill(json={"ok": True, "text": "2026-11-10T12:00:00Z ERROR stale snapshot"})
    page.evaluate("async () => {await window.oldLogRefresh}")
    expect(page.locator("#logsOut")).to_contain_text("current snapshot")
    expect(page.locator("#logsOut")).not_to_contain_text("stale snapshot")


def test_log_levels_use_prefixes_and_preserve_error_stack_context(page, dashboard):
    rows = [
        "2026-11-10T12:00:00Z INFO player says ERROR WARN Exception",
        "2026-11-10T12:00:01Z ERROR java.lang.IllegalStateException: failed",
        "2026-11-10T12:00:02Z \tat zombie.network.GameServer.main(GameServer.java:42)",
        "2026-11-10T12:00:03Z Caused by: java.io.IOException: disk offline",
        "2026-11-10T12:00:04Z LOG  : General f:0, t:1765668999341, st:214952175> healthy again",
        "2026-11-10T12:00:05Z WARN : General f:0, t:1765668999342, st:214952176> slow save",
    ]
    page.route(
        "**/api/logs**", lambda route: route.fulfill(json={"ok": True, "text": "\n".join(rows)})
    )
    open_console(page, dashboard)
    expect(page.locator("#logsOut .l-err")).to_have_count(3)
    expect(page.locator("#logsOut .l-warn")).to_have_count(1)
    page.locator('#logLevels [data-level="error"]').click()
    expect(page.locator("#logsOut")).to_contain_text("GameServer.java:42")
    expect(page.locator("#logsOut")).to_contain_text("Caused by:")
    expect(page.locator("#logsOut")).not_to_contain_text("player says")
    expect(page.locator("#logsOut")).not_to_contain_text("healthy again")
    expect(page.locator("#logsOut")).not_to_contain_text("slow save")


def test_log_refresh_preserves_last_success_and_recovers_without_rebuilding(page, dashboard):
    page.clock.install()
    frame = {"ok": True, "text": "2026-11-10T12:00:00Z INFO stable row"}
    page.route("**/api/logs**", lambda route: route.fulfill(json=frame))
    open_console(page, dashboard, language="en")
    expect(page.locator("#logsOut")).to_contain_text("stable row")
    freshness = page.locator("#logsFreshness").inner_text()
    assert freshness
    page.locator("#logsOut > span").evaluate("el => window.stableLogNode=el")
    held = []
    page.route("**/api/logs**", lambda route: held.append(route))
    page.locator("#logsRefresh").click()
    expect(page.locator("#logsRefresh")).to_be_disabled()
    held[0].fulfill(json={"ok": False, "error": "Docker temporarily unavailable"})
    expect(page.locator("#logsError")).to_contain_text("Docker temporarily unavailable")
    expect(page.locator("#logsFreshness")).to_have_text(freshness)
    expect(page.locator("#logsOut")).to_contain_text("stable row")
    page.clock.run_for(2000)
    page.locator("#logsRefresh").click()
    expect(page.locator("#logsRefresh")).to_be_disabled()
    held[1].fulfill(json=frame)
    expect(page.locator("#logsError")).to_be_hidden()
    expect(page.locator("#logsFreshness")).not_to_have_text(freshness)
    assert page.locator("#logsOut > span").evaluate("el => el === window.stableLogNode")


def test_log_follow_and_latest_preserve_reading_and_keyboard_focus(page, dashboard):
    frame = {"ok": True, "text": "\n".join(f"INFO log line {i}" for i in range(200))}
    page.route("**/api/logs**", lambda route: route.fulfill(json=frame))
    open_console(page, dashboard)
    output = page.locator("#logsOut")
    expect(output).to_contain_text("line 199")
    assert at_bottom(output)
    page.locator("#logsAuto").uncheck()
    output.evaluate("el => {el.scrollTop=80; el.dispatchEvent(new Event('scroll'));}")
    before = output.evaluate("el => el.scrollTop")
    frame["text"] += "\nINFO arrived while reading"
    page.locator("#logsRefresh").click()
    expect(output).to_contain_text("arrived while reading")
    assert abs(output.evaluate("el => el.scrollTop") - before) <= 1
    page.locator("#logsAuto").check()
    assert at_bottom(output)
    output.evaluate("el => {el.scrollTop=80; el.dispatchEvent(new Event('scroll'));}")
    frame["text"] += "\nINFO another arrival"
    page.locator("#logsRefresh").click()
    expect(output).to_contain_text("another arrival")
    assert abs(output.evaluate("el => el.scrollTop") - 80) <= 1
    latest = page.locator("#logsLatest")
    latest.focus()
    latest.press("Enter")
    assert at_bottom(output)
    expect(page.locator("#logsAuto")).to_be_checked()
    expect(latest).to_be_focused()


@pytest.mark.parametrize("language,width", [("ru", 320), ("en", 390), ("ru", 1024), ("en", 1440)])
def test_console_regions_and_command_field_remain_usable(page, dashboard, language, width):
    open_console(page, dashboard, language=language, width=width)
    for selector in ["#consoleOut", "#logsOut"]:
        region = page.locator(selector)
        expect(region).to_have_attribute("tabindex", "0")
        expect(region).to_have_accessible_name(re.compile(r"\S"))
        assert region.get_attribute("aria-live") in (None, "off")
        region.focus()
        expect(region).to_be_focused()
    page.locator("#consoleInput").fill('servermsg "' + "Long readable command " * 10 + '"')
    if width <= 390:
        assert page.locator("#consoleInput").bounding_box()["width"] >= 180
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(page.locator("#logsLatest")).to_have_accessible_name(re.compile(r"\S"))
    expect(page.locator("#logsRefresh")).to_have_accessible_name(re.compile(r"\S"))
