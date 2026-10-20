"""Operation locks cover every editing surface and survive asynchronous races."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from test_editors import editing, env  # noqa: F401

from browser_evidence import capture_evidence

pytestmark = pytest.mark.browser

ACTIVE = {
    "op": "mods-restart",
    "phase": "Countdown",
    "message": "Waiting for players",
    "startedAt": "2026-10-07T12:00:00Z",
    "cancellable": True,
}


def stop_stream(page):
    expect(page.locator("#startupLoader")).to_be_hidden()
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")


@pytest.mark.parametrize("language,width", [("ru", 390), ("en", 1440)])
def test_every_mutating_view_locks_and_recovers(page, dashboard, editing, language, width):  # noqa: F811
    dashboard["players"].update(names=["Alice"], count=1)
    page.set_viewport_size({"width": width, "height": 1000})
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    stop_stream(page)
    page.evaluate("""() => {
        renderPlayers({ok:true,names:['Alice'],count:1});
        renderBackups({ok:true,items:[{name:'world.tar',size:1024,mtime:'2026-10-07T12:00:00Z'}],journal:[]});
        document.getElementById('tgToken').value='unsaved-token';
        document.getElementById('consoleInput').value='players';
        settingsFeedback('watchdog','error','Retry saving');
    }""")
    page.evaluate("active=>renderOp({active,history:[]})", ACTIVE)
    controls = {
        "overview": ("operationAvailability", "#btnStop,#btnRestart,#btnSaveWorld"),
        "settings": (
            "settingsAvailability",
            "#configProfile,#configFields [data-key],#configVerify",
        ),
        "mods": ("modsAvailability", "#workshopInput,#workshopAdd button,#modImport,#modRescan"),
        "players": ("playersAvailability", "#playersBody button"),
        "maintenance": (
            "maintenanceAvailability",
            "#view-maintenance input,#view-maintenance select,#btnTgTest,#btnTgChats,.settings-feedback button",
        ),
        "backups": (
            "backupAvailability",
            "#btnBackup,#backupsBody button,#bkAutoSwitch,#bkAutoTime",
        ),
        "console": ("consoleAvailability", "#consoleInput,#consoleForm button,#quickCmds button"),
    }
    for view, (notice, selector) in controls.items():
        page.evaluate("view=>location.hash='#/'+view", view)
        expect(page.locator(f"#view-{view}")).to_be_visible()
        expect(page.locator(f"#{notice}")).to_be_hidden()
        widget = page.locator(f"#view-{view} .operation-loading").first
        expect(widget).to_have_attribute("aria-busy", "true")
        expect(widget.locator(":scope > .operation-loader")).to_be_visible()
        expect(widget.locator(":scope > .operation-loader .operation-label")).to_have_text(
            "Идёт операция: Авторестарт модов"
            if language == "ru"
            else "Operation in progress: Automatic mod restart"
        )
        assert (
            widget.locator(":scope > :not(.operation-loader)").first.evaluate(
                "el=>getComputedStyle(el).filter"
            )
            == "blur(2px)"
        )
        assert page.locator(selector).count() > 0
        assert page.locator(selector).evaluate_all("nodes=>nodes.every(node=>node.disabled)")
        expect(page.locator("#btnCancelMods")).to_be_enabled()
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        if view in ("settings", "maintenance"):
            target = Path(__file__).resolve().parents[2] / ".tmp-operation-lock-evidence"
            target.mkdir(exist_ok=True)
            capture_evidence(
                page, path=str(target / f"{view}-{language}-{width}.png"), full_page=True
            )

    page.evaluate("location.hash='#/events'")
    expect(page.locator("#view-events")).to_be_visible()
    expect(page.locator('#eventFilters [data-ef="all"]')).to_be_enabled()
    page.evaluate("renderOp({active:null,history:[]})")
    expect(page.locator(".operation-loading")).to_have_count(0)
    expect(page.locator(".operation-loader:visible")).to_have_count(0)
    for notice, _ in controls.values():
        expect(page.locator(f"#{notice}")).to_be_hidden()
    for control in (
        "#btnStop",
        "#wdRestart",
        "#tgToken",
        "#playerLanguage",
        "#btnTgTest",
        "#consoleInput",
        "#workshopInput",
    ):
        expect(page.locator(control)).to_be_enabled()
    expect(page.locator("#tgToken")).to_have_value("unsaved-token")
    expect(page.locator("#consoleInput")).to_have_value("players")


@pytest.mark.parametrize("language", ["ru", "en"])
def test_operation_loading_fallback_uses_selected_language(page, dashboard, language):
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.goto(dashboard["url"] + "/#/maintenance")
    expect(page.locator("#btnStop")).to_be_enabled()
    stop_stream(page)
    page.evaluate("S.actionPending=true;S.pendingOperation=null;updateButtons()")
    expect(page.locator("#sec-updates .operation-label")).to_have_text(
        "Идёт операция" if language == "ru" else "Operation in progress"
    )
    page.evaluate("S.pendingOperation='restart';updateButtons()")
    expect(page.locator("#sec-updates .operation-label")).to_have_text(
        "Идёт операция: Рестарт" if language == "ru" else "Operation in progress: Restart"
    )


def test_pending_editor_launch_locks_other_views_and_rejection_restores_input(
    page,
    dashboard,
    editing,  # noqa: F811
):
    held = []
    page.route("**/api/action", lambda route: held.append(route))
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    stop_stream(page)
    page.clock.install()
    page.evaluate("document.getElementById('tgToken').value='keep-token'")
    page.locator('[data-key="PublicName"]').fill("Keep draft after rejection")
    page.locator("#configApply").click()
    expect(page.locator("#modalRoot")).to_be_visible()
    page.locator("#modalOk").click()
    expect(page.locator("#settingsAvailability")).to_be_hidden()
    expect(page.locator("#view-settings .operation-loader")).to_be_visible()
    for control in ("#configProfile", "#wdRestart", "#tgToken", "#workshopInput", "#btnStop"):
        expect(page.locator(control)).to_be_disabled()
    assert len(held) == 1
    # Workshop validation retains the editor's longer request timeout.
    page.clock.run_for(10000)
    expect(page.locator("#configProfile")).to_be_disabled()
    expect(page.locator("#view-settings .operation-loader")).to_be_visible()
    held[0].fulfill(status=409, json={"ok": False, "error": "Rejected launch"})
    expect(page.locator("#modalError")).to_contain_text("Rejected launch")
    expect(page.locator("#configProfile")).to_be_enabled()
    expect(page.locator("#settingsAvailability")).to_be_hidden()
    expect(page.locator("#tgToken")).to_have_value("keep-token")
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Keep draft after rejection")


@pytest.mark.parametrize("kind", ["kick", "delete", "restart"])
def test_confirmation_opened_before_operation_cannot_send_changes(page, dashboard, kind):
    sent = []
    page.route(
        "**/api/rcon",
        lambda route: (sent.append(route.request.url), route.fulfill(json={"ok": True})),
    )
    page.route(
        "**/api/backup?*",
        lambda route: (sent.append(route.request.url), route.fulfill(json={"ok": True})),
    )
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    stop_stream(page)
    if kind == "kick":
        page.evaluate("confirmPlayerAction('kick','Alice')")
    elif kind == "delete":
        page.evaluate("confirmDeleteBackup('world.tar')")
    else:
        page.locator("#btnRestart").click()
    page.evaluate("active=>renderOp({active,history:[]})", ACTIVE)
    page.locator("#modalOk").click()
    expect(page.locator("#modalError")).to_contain_text("Идёт операция")
    assert sent == []
    assert dashboard["actions"] == []
    page.locator("#modalCancel").click()
    expect(page.locator("#btnCancelMods")).to_be_enabled()


@pytest.mark.parametrize("result", ["ok", "error", "cancelled"])
def test_finished_operation_unlocks_and_cancel_stays_available(page, dashboard, result):
    page.goto(dashboard["url"] + "/#/maintenance")
    expect(page.locator("#btnStop")).to_be_enabled()
    stop_stream(page)
    page.evaluate("active=>renderOp({active,history:[]})", ACTIVE)
    if result == "cancelled":
        page.route(
            "**/api/action",
            lambda route: route.fulfill(json={"ok": True, "cancelRequested": True}),
        )
        page.locator("#btnCancelMods").click()
        expect(page.locator("#btnCancelMods")).to_be_disabled()
        expect(page.locator("#wdRestart")).to_be_disabled()
    history = {
        "op": "mods-restart",
        "ok": result != "error",
        "cancelled": result == "cancelled",
        "finishedAt": "2026-10-07T12:01:00Z",
        "message": "Finished",
    }
    page.evaluate("h=>renderOp({active:null,history:[h]})", history)
    expect(page.locator("#btnStop")).to_be_enabled()
    expect(page.locator("#wdRestart")).to_be_enabled()
    expect(page.locator("#maintenanceAvailability")).to_be_hidden()
    expect(page.locator("#btnCancelMods")).to_be_hidden()
    page.locator("#btnNotifications").click()
    expect(page.locator("#notificationList")).to_contain_text("Finished")


def operation_snapshot(sequence, active=None, history=None, epoch="operation-lock-browser-tests"):
    return {
        "ok": True,
        "snapshot": {"epoch": epoch, "sequence": sequence},
        "active": active,
        "history": history or [],
    }


def fake_operation_stream(page):
    page.add_init_script("""window.EventSource = class extends EventTarget {
      constructor() {super(); window.operationStream = this;}
      close() {}
    };""")


def send_operation_frame(page, snapshot):
    page.evaluate(
        """snapshot => operationStream.dispatchEvent(new MessageEvent('ops', {
          data: JSON.stringify(snapshot)
        }))""",
        snapshot,
    )


@pytest.mark.parametrize(
    "status,payload",
    [(503, {"error": "Temporarily unavailable"}), (503, {}), (200, {}), (200, None)],
)
def test_failed_operation_refresh_cannot_release_active_lock(page, dashboard, status, payload):
    page.goto(dashboard["url"])
    stop_stream(page)
    snapshot = operation_snapshot(10, {**ACTIVE, "id": "refresh-operation"})
    page.route("**/api/ops", lambda route: route.fulfill(json=snapshot))
    page.evaluate("refreshOps()")
    expect(page.locator("#btnStop")).to_be_disabled()

    def fail_refresh(route):
        if payload is None:
            route.fulfill(status=status, content_type="application/json", body="{broken")
        else:
            route.fulfill(status=status, json=payload)

    page.route("**/api/ops", fail_refresh)
    page.evaluate("refreshOps()")
    expect(page.locator("#btnStop")).to_be_disabled()
    expect(page.locator("#opbar")).to_be_visible()
    expect(page.locator("#btnCancelMods")).to_be_enabled()


def test_idle_refresh_started_before_launch_cannot_unlock_accepted_operation(page, dashboard):
    page.goto(dashboard["url"])
    stop_stream(page)
    held = []
    page.route("**/api/ops", lambda route: held.append(route))
    with page.expect_request("**/api/ops"):
        page.evaluate("() => { void refreshOps(); }")
    snapshot = operation_snapshot(
        12,
        {"id": "accepted-operation", "op": "check-update", "phase": "Checking"},
    )
    page.route(
        "**/api/action",
        lambda route: route.fulfill(
            json={
                "ok": True,
                "started": "check-update",
                "operationId": "accepted-operation",
                "operationState": snapshot,
            }
        ),
    )
    page.route("**/api/ops", lambda route: route.fulfill(json=snapshot))
    page.evaluate("requestOperation({op:'check-update'})")
    expect(page.locator("#btnStop")).to_be_disabled()
    assert len(held) == 1
    held.pop().fulfill(json=operation_snapshot(11))
    page.wait_for_function("S.op?.active?.id === 'accepted-operation'")
    expect(page.locator("#btnStop")).to_be_disabled()
    expect(page.locator("#opbar")).to_be_visible()


def test_out_of_order_stream_frames_neither_unlock_nor_revive_operation(page, dashboard):
    fake_operation_stream(page)
    page.goto(dashboard["url"])
    page.wait_for_function("window.operationStream")
    active = {**ACTIVE, "id": "stream-operation"}
    send_operation_frame(page, operation_snapshot(20, active))
    expect(page.locator("#btnStop")).to_be_disabled()
    send_operation_frame(page, operation_snapshot(19))
    expect(page.locator("#btnStop")).to_be_disabled()
    history = {
        "id": active["id"],
        "op": active["op"],
        "ok": True,
        "finishedAt": "2026-10-20T12:01:00Z",
    }
    send_operation_frame(page, operation_snapshot(21, history=[history]))
    expect(page.locator("#btnStop")).to_be_enabled()
    send_operation_frame(page, operation_snapshot(20, active))
    expect(page.locator("#btnStop")).to_be_enabled()
    expect(page.locator("#opbar")).to_be_hidden()


def test_operation_epoch_rollover_rejects_frames_from_retired_process(page, dashboard):
    fake_operation_stream(page)
    old_active = {**ACTIVE, "id": "previous-process-operation"}
    snapshot = operation_snapshot(100, old_active, epoch="previous-process")
    page.route("**/api/ops", lambda route: route.fulfill(json=snapshot))
    page.goto(dashboard["url"])
    page.wait_for_function("window.operationStream")
    send_operation_frame(page, operation_snapshot(100, old_active, epoch="previous-process"))
    expect(page.locator("#btnStop")).to_be_disabled()
    snapshot = operation_snapshot(1, epoch="current-process")
    send_operation_frame(page, operation_snapshot(1, epoch="current-process"))
    expect(page.locator("#btnStop")).to_be_enabled()
    send_operation_frame(page, operation_snapshot(101, old_active, epoch="previous-process"))
    expect(page.locator("#btnStop")).to_be_enabled()
    current_active = {**ACTIVE, "id": "current-process-operation"}
    send_operation_frame(page, operation_snapshot(2, current_active, epoch="current-process"))
    expect(page.locator("#btnStop")).to_be_disabled()
    send_operation_frame(page, operation_snapshot(999, epoch="previous-process"))
    expect(page.locator("#btnStop")).to_be_disabled()


@pytest.mark.parametrize("preexisting_refresh", [False, True])
def test_unseen_old_stream_epoch_cannot_release_current_operation(
    page, dashboard, preexisting_refresh
):
    fake_operation_stream(page)
    active = {**ACTIVE, "id": "current-epoch-operation"}
    page.route(
        "**/api/ops",
        lambda route: route.fulfill(json=operation_snapshot(100, active, epoch="epoch-2")),
    )
    page.goto(dashboard["url"])
    page.wait_for_function("window.operationStream")
    held = []
    page.expose_function("heldOperationEpochReads", lambda: len(held))
    page.route("**/api/ops", lambda route: held.append(route))
    if preexisting_refresh:
        with page.expect_request("**/api/ops"):
            page.evaluate("() => {window.preexistingEpochRefresh = refreshOps();}")
    send_operation_frame(page, operation_snapshot(999, epoch="unseen-epoch-1"))
    expected_reads = 2 if preexisting_refresh else 1
    page.wait_for_function(
        "async count => await window.heldOperationEpochReads() === count", arg=expected_reads
    )
    expect(page.locator("#btnStop")).to_be_disabled()
    assert page.evaluate("S.op.snapshot.epoch") == "epoch-2"
    if preexisting_refresh:
        held[0].fulfill(json=operation_snapshot(1000, epoch="unseen-epoch-1"))
        page.evaluate("() => window.preexistingEpochRefresh")
        expect(page.locator("#btnStop")).to_be_disabled()
        assert page.evaluate("S.op.snapshot.epoch") == "epoch-2"
    send_operation_frame(page, operation_snapshot(1001, epoch="unseen-epoch-1"))
    assert len(held) == expected_reads
    held[-1].fulfill(json=operation_snapshot(101, active, epoch="epoch-2"))
    page.wait_for_function("S.op.snapshot.sequence === 101")
    expect(page.locator("#btnStop")).to_be_disabled()
    assert page.evaluate("S.op.active.id") == active["id"]


def test_new_stream_epoch_unlocks_only_after_direct_confirmation(page, dashboard):
    fake_operation_stream(page)
    active = {**ACTIVE, "id": "operation-before-process-restart"}
    page.route(
        "**/api/ops",
        lambda route: route.fulfill(json=operation_snapshot(100, active, epoch="epoch-2")),
    )
    page.goto(dashboard["url"])
    page.wait_for_function("window.operationStream")
    held = []
    page.expose_function("heldNewEpochConfirmation", lambda: len(held))
    page.route("**/api/ops", lambda route: held.append(route))
    send_operation_frame(page, operation_snapshot(1, epoch="epoch-3"))
    page.wait_for_function("async () => await window.heldNewEpochConfirmation() === 1")
    expect(page.locator("#btnStop")).to_be_disabled()
    expect(page.locator("#opbar")).to_be_visible()
    assert page.evaluate("S.op.snapshot.epoch") == "epoch-2"
    held.pop().fulfill(json=operation_snapshot(2, epoch="epoch-3"))
    expect(page.locator("#btnStop")).to_be_enabled()
    expect(page.locator("#opbar")).to_be_hidden()
    assert page.evaluate("S.op.snapshot.epoch") == "epoch-3"
    send_operation_frame(page, operation_snapshot(999, active, epoch="epoch-2"))
    expect(page.locator("#btnStop")).to_be_enabled()


def test_older_overlapping_refresh_cannot_replace_newer_process_epoch(page, dashboard):
    page.route(
        "**/api/ops", lambda route: route.fulfill(json=operation_snapshot(1, epoch="epoch-0"))
    )
    page.goto(dashboard["url"])
    stop_stream(page)
    held = []
    page.expose_function("heldOverlappingEpochReads", lambda: len(held))
    page.route("**/api/ops", lambda route: held.append(route))
    with page.expect_request("**/api/ops"):
        page.evaluate("() => {window.oldEpochRefresh = refreshOps();}")
    with page.expect_request("**/api/ops"):
        page.evaluate("() => {window.newEpochRefresh = refreshOps();}")
    page.wait_for_function("async () => await window.heldOverlappingEpochReads() === 2")
    active = {**ACTIVE, "id": "newest-process-operation"}
    held[1].fulfill(json=operation_snapshot(50, active, epoch="epoch-2"))
    page.evaluate("() => window.newEpochRefresh")
    expect(page.locator("#btnStop")).to_be_disabled()
    held[0].fulfill(json=operation_snapshot(999, epoch="unseen-epoch-1"))
    page.evaluate("() => window.oldEpochRefresh")
    expect(page.locator("#btnStop")).to_be_disabled()
    assert page.evaluate("S.op.snapshot.epoch") == "epoch-2"
    active = {**active, "phase": "Current process progress"}
    page.route(
        "**/api/ops",
        lambda route: route.fulfill(json=operation_snapshot(51, active, epoch="epoch-2")),
    )
    page.evaluate("refreshOps()")
    expect(page.locator("#opPhase")).to_contain_text("Current process progress")


def test_cached_stream_frame_after_launch_receipt_cannot_clear_admission_state(page, dashboard):
    fake_operation_stream(page)
    page.goto(dashboard["url"])
    page.wait_for_function("window.operationStream")
    send_operation_frame(page, operation_snapshot(60))
    state = operation_snapshot(
        62,
        {"id": "admitted-operation", "op": "check-update", "phase": "Checking updates"},
    )
    page.route(
        "**/api/action",
        lambda route: route.fulfill(
            json={
                "ok": True,
                "started": "check-update",
                "operationId": "admitted-operation",
                "operationState": state,
            }
        ),
    )
    held = []
    page.route("**/api/ops", lambda route: held.append(route))
    with page.expect_request("**/api/ops"):
        page.evaluate(
            """() => {
              window.launchFinished = false;
              requestOperation({op:'check-update'}).finally(() => {
                window.launchFinished = true;
              });
            }"""
        )
    page.wait_for_function("S.op?.active?.id === 'admitted-operation'")
    send_operation_frame(page, operation_snapshot(61))
    assert len(held) == 1
    held.pop().fulfill(status=503, json={"error": "Unavailable"})
    page.wait_for_function("window.launchFinished")
    expect(page.locator("#btnStop")).to_be_disabled()
    expect(page.locator("#opPhase")).to_contain_text("Checking updates")


def test_completion_before_launch_acknowledgement_is_not_revived(page, dashboard):
    fake_operation_stream(page)
    page.goto(dashboard["url"])
    page.wait_for_function("window.operationStream")
    held = []
    page.route("**/api/action", lambda route: held.append(route))
    page.evaluate(
        """() => {
          window.launchFinished = false;
          requestOperation({op:'check-update'}).catch(() => {}).finally(() => {
            window.launchFinished = true;
          });
        }"""
    )
    expect(page.locator("#btnStop")).to_be_disabled()
    history = {
        "id": "quick-operation",
        "op": "check-update",
        "ok": True,
        "finishedAt": "2026-10-20T12:01:00Z",
    }
    send_operation_frame(page, operation_snapshot(30, history=[history]))
    page.route(
        "**/api/ops",
        lambda route: route.fulfill(status=503, json={"error": "Unavailable"}),
    )
    assert len(held) == 1
    held.pop().fulfill(
        json={"ok": True, "started": "check-update", "operationId": "quick-operation"}
    )
    page.wait_for_function("window.launchFinished")
    expect(page.locator("#btnStop")).to_be_enabled()
    expect(page.locator("#opbar")).to_be_hidden()


def test_accepted_editor_operation_survives_failed_state_refresh(
    page,
    dashboard,
    editing,  # noqa: F811
):
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    stop_stream(page)
    page.route(
        "**/api/ops",
        lambda route: route.fulfill(status=503, json={"error": "Unavailable"}),
    )
    state = operation_snapshot(
        35,
        {"id": "config-operation", "op": "apply-config", "phase": "Writing configuration"},
    )
    page.route(
        "**/api/action",
        lambda route: route.fulfill(
            json={
                "ok": True,
                "operation": "apply-config",
                "operationId": "config-operation",
                "operationState": state,
            }
        ),
    )
    page.locator('[data-key="PublicName"]').fill("Operation stays locked")
    page.locator("#configApply").click()
    expect(page.locator("#modalRoot")).to_be_visible()
    page.locator("#modalOk").click()
    expect(page.locator("#modalRoot")).to_be_hidden()
    expect(page.locator("#configProfile")).to_be_disabled()
    expect(page.locator("#btnStop")).to_be_disabled()
    expect(page.locator("#opbar")).to_be_visible()
    expect(page.locator("#opPhase")).to_contain_text("Writing configuration")


def test_ambiguous_launch_timeout_stays_locked_until_fresh_state(page, dashboard):
    current_state = operation_snapshot(40)
    page.route("**/api/ops", lambda route: route.fulfill(json=current_state))
    page.goto(dashboard["url"])
    stop_stream(page)
    page.clock.install()
    held = []
    resolutions = []
    resolution = {"mode": "error", "sequence": 41}

    def respond(route):
        body = route.request.post_data_json
        if body["op"] == "check-update":
            held.append(route)
        else:
            assert body["op"] == "resolve-request"
            resolutions.append(body)
            if resolution["mode"] == "error":
                route.fulfill(status=503, json={"error": "Unavailable"})
            else:
                resolution["sequence"] += 1
                route.fulfill(
                    json={
                        "ok": True,
                        "requestPending": resolution["mode"] == "pending",
                        "operationState": operation_snapshot(resolution["sequence"]),
                    }
                )

    page.route("**/api/action", respond)
    page.evaluate(
        """() => {
          window.launchFinished = false;
          requestOperation({op:'check-update'}).catch(() => {}).finally(() => {
            window.launchFinished = true;
          });
        }"""
    )
    expect(page.locator("#btnStop")).to_be_disabled()
    assert len(held) == 1
    original = held[0].request.post_data_json
    assert original["requestEpoch"] == current_state["snapshot"]["epoch"]
    assert isinstance(original["requestId"], str) and original["requestId"]
    page.clock.run_for(9500)
    page.wait_for_function("window.launchFinished")
    expect(page.locator("#btnStop")).to_be_disabled()
    expect(page.locator("#opbar")).to_be_visible()
    current_state = operation_snapshot(41)
    page.evaluate("async () => applyOps(await api('/api/ops'))")
    expect(page.locator("#btnStop")).to_be_disabled()
    resolution["mode"] = "pending"
    page.clock.run_for(1600)
    page.wait_for_function("S.op?.snapshot?.sequence === 42")
    expect(page.locator("#btnStop")).to_be_disabled()
    expect(page.locator("#opbar")).to_be_visible()
    resolution["mode"] = "resolved"
    page.clock.run_for(1600)
    expect(page.locator("#btnStop")).to_be_enabled()
    expect(page.locator("#opbar")).to_be_hidden()
    assert len(held) == 1
    assert len(resolutions) >= 3
    assert all(
        body
        == {
            "op": "resolve-request",
            "requestId": original["requestId"],
            "requestEpoch": original["requestEpoch"],
        }
        for body in resolutions
    )


def test_terminal_request_resolution_preserves_newer_stream_snapshot(page, dashboard):
    fake_operation_stream(page)
    page.route("**/api/ops", lambda route: route.fulfill(json=operation_snapshot(100)))
    page.goto(dashboard["url"])
    page.wait_for_function("window.operationStream")
    page.clock.install()
    launches, resolutions = [], []
    page.expose_function("heldRequestResolutions", lambda: len(resolutions))

    def hold_request(route):
        if route.request.post_data_json["op"] == "resolve-request":
            resolutions.append(route)
        else:
            launches.append(route)

    page.route("**/api/action", hold_request)
    page.evaluate(
        """() => {
          window.launchFinished = false;
          requestOperation({op:'check-update'}).catch(() => {}).finally(() => {
            window.launchFinished = true;
          });
        }"""
    )
    expect(page.locator("#btnStop")).to_be_disabled()
    with page.expect_request(
        lambda request: (
            request.url.endswith("/api/action")
            and request.post_data_json.get("op") == "resolve-request"
        )
    ):
        page.clock.run_for(9500)
    page.wait_for_function("async () => await window.heldRequestResolutions() === 1")
    assert len(launches) == 1
    assert len(resolutions) == 1
    newest = {
        "id": "newer-completed-operation",
        "op": "backup",
        "ok": True,
        "finishedAt": "2026-10-20T12:01:00Z",
    }
    send_operation_frame(page, operation_snapshot(103, history=[newest]))
    expect(page.locator("#btnStop")).to_be_disabled()
    resolutions.pop().fulfill(
        json={"ok": True, "requestPending": False, "operationState": operation_snapshot(102)}
    )
    page.wait_for_function("window.launchFinished")
    expect(page.locator("#btnStop")).to_be_enabled()
    expect(page.locator("#opbar")).to_be_hidden()
    assert page.evaluate("S.op.snapshot.sequence") == 103
    assert page.evaluate("S.op.history[0].id") == newest["id"]


def test_cancel_request_stays_disabled_across_operation_refreshes(page, dashboard):
    page.goto(dashboard["url"] + "/#/maintenance")
    stop_stream(page)
    active = {**ACTIVE, "id": "cancel-operation"}
    snapshot = operation_snapshot(50, active)
    page.route("**/api/ops", lambda route: route.fulfill(json=snapshot))
    page.evaluate("refreshOps()")
    held = []
    page.route("**/api/action", lambda route: held.append(route))
    page.locator("#btnCancelMods").click()
    expect(page.locator("#btnCancelMods")).to_be_disabled()
    snapshot = operation_snapshot(51, active)
    page.evaluate("refreshOps()")
    expect(page.locator("#btnCancelMods")).to_be_disabled()
    assert len(held) == 1
    held.pop().fulfill(json={"ok": True, "cancelRequested": True})
    expect(page.locator("#btnCancelMods")).to_have_text("Отмена…")
    snapshot = operation_snapshot(52, active)
    page.evaluate("refreshOps()")
    expect(page.locator("#btnCancelMods")).to_be_disabled()
    expect(page.locator("#btnCancelMods")).to_have_text("Отмена…")
    expect(page.locator("#btnStop")).to_be_disabled()
    snapshot = operation_snapshot(53)
    page.evaluate("refreshOps()")
    expect(page.locator("#btnCancelMods")).to_be_hidden()
    expect(page.locator("#btnStop")).to_be_enabled()


@pytest.mark.parametrize("response", [{}, {"ok": False}, {"ok": True}])
def test_invalid_cancellation_response_does_not_claim_cancellation(page, dashboard, response):
    page.goto(dashboard["url"] + "/#/maintenance")
    stop_stream(page)
    snapshot = operation_snapshot(70, {**ACTIVE, "id": "uncancelled-operation"})
    page.route("**/api/ops", lambda route: route.fulfill(json=snapshot))
    page.evaluate("refreshOps()")
    sent = []

    def respond(route):
        sent.append(route.request.post_data_json)
        route.fulfill(json=response)

    page.route("**/api/action", respond)
    page.locator("#btnCancelMods").click()
    expect(page.locator("#btnCancelMods")).to_be_enabled()
    expect(page.locator("#btnCancelMods")).to_have_text("Отменить обновление модов")
    expect(page.locator("#btnStop")).to_be_disabled()
    assert sent == [{"op": "cancel-mods-update", "operationId": "uncancelled-operation"}]
    assert not page.evaluate("!!S.op.active.cancelRequested")


def test_late_cancellation_acknowledgement_does_not_mark_replacement_operation(page, dashboard):
    page.goto(dashboard["url"] + "/#/maintenance")
    stop_stream(page)
    active = {**ACTIVE, "id": "first-cancellation-target"}
    snapshot = operation_snapshot(80, active)
    page.route("**/api/ops", lambda route: route.fulfill(json=snapshot))
    page.evaluate("refreshOps()")
    held = []
    page.route("**/api/action", lambda route: held.append(route))
    page.locator("#btnCancelMods").click()
    expect(page.locator("#btnCancelMods")).to_be_disabled()
    assert len(held) == 1
    assert held[0].request.post_data_json == {
        "op": "cancel-mods-update",
        "operationId": active["id"],
    }
    replacement = {**ACTIVE, "id": "second-cancellation-target"}
    snapshot = operation_snapshot(81, replacement)
    page.evaluate("refreshOps()")
    expect(page.locator("#btnCancelMods")).to_be_enabled()
    held.pop().fulfill(json={"ok": True, "cancelRequested": True})
    page.wait_for_function("pendingCancellations.size === 0")
    expect(page.locator("#btnCancelMods")).to_be_enabled()
    expect(page.locator("#btnCancelMods")).to_have_text("Отменить обновление модов")
    assert page.evaluate("S.op.active.id") == replacement["id"]
    assert not page.evaluate("!!S.op.active.cancelRequested")


def test_overlapping_operation_refreshes_apply_newer_completion_after_active_response(
    page, dashboard
):
    page.goto(dashboard["url"])
    stop_stream(page)
    held = []
    page.expose_function("operationRequestsHeld", lambda: len(held))
    page.route("**/api/ops", lambda route: held.append(route))
    for _ in range(2):
        with page.expect_request("**/api/ops"):
            page.evaluate("() => {void refreshOps();}")
    page.wait_for_function("async () => await window.operationRequestsHeld() === 2")
    assert len(held) == 2
    active = {**ACTIVE, "id": "overlapping-refresh-operation"}
    held[0].fulfill(json=operation_snapshot(90, active))
    page.wait_for_function("S.op?.active?.id === 'overlapping-refresh-operation'")
    expect(page.locator("#btnStop")).to_be_disabled()
    held[1].fulfill(
        json=operation_snapshot(
            91,
            history=[
                {
                    "id": active["id"],
                    "op": active["op"],
                    "ok": True,
                    "finishedAt": "2026-10-20T12:01:00Z",
                }
            ],
        )
    )
    expect(page.locator("#btnStop")).to_be_enabled()
    expect(page.locator("#opbar")).to_be_hidden()
