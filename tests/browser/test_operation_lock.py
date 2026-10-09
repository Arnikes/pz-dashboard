"""Operation locks cover every editing surface and survive asynchronous races."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from test_editors import editing, env  # noqa: F401

pytestmark = pytest.mark.browser

ACTIVE = {
    "op": "mods-restart",
    "phase": "Countdown",
    "message": "Waiting for players",
    "startedAt": "2026-10-07T12:00:00Z",
    "cancellable": True,
}


def stop_stream(page):
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
            page.screenshot(path=str(target / f"{view}-{language}-{width}.png"), full_page=True)

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
        page.route("**/api/action", lambda route: route.fulfill(json={"ok": True}))
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
