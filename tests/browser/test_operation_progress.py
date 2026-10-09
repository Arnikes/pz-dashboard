"""Countdown ending transitions into startup while widgets stay unavailable."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("language,width", [("ru", 390), ("en", 1440)])
@pytest.mark.parametrize("operation", ["restart", "apply-mods-update", "mods-restart"])
def test_countdown_finishing_keeps_lock_until_game_ready(
    page, dashboard, language, width, operation
):
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(f"localStorage.setItem('pz-language','{language}')")
    page.goto(dashboard["url"] + "/#/maintenance")
    expect(page.locator("#btnStop")).to_be_enabled()
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")
    page.clock.install()
    warning = "Предупреждение игроков" if language == "ru" else "Warning players"
    stopping = "Остановка" if language == "ru" else "Stopping"
    loading = "Загрузка сервера" if language == "ru" else "Loading server"
    page.evaluate(
        """({warning,operation})=>renderOp({active:{op:operation,phase:warning,stages:['Подготовка…',warning],
        countdownEndsAt:Date.now()/1000+300},history:[]})""",
        {"warning": warning, "operation": operation},
    )
    expect(page.locator("#opCountdown")).to_have_text("5:00")
    expect(page.locator("#opElapsed")).to_have_count(0)
    expect(page.locator("#maintenanceAvailability")).to_be_hidden()
    target = Path(__file__).resolve().parents[2] / ".tmp-operation-lock-evidence"
    target.mkdir(exist_ok=True)
    page.screenshot(
        path=str(target / f"countdown-{operation}-{language}-{width}.png"), full_page=True
    )
    page.clock.run_for(300000)
    expect(page.locator("#opCountdown")).to_have_text("0:00")
    expect(page.locator("#btnRestart")).to_be_disabled()
    expect(page.locator("#sec-watchdog .operation-loader")).to_be_visible()
    page.evaluate(
        """({stages,operation})=>renderOp({active:{op:operation,phase:stages[2],stages,
        countdownEndsAt:null},history:[]})""",
        {"stages": [warning, stopping, loading], "operation": operation},
    )
    expect(page.locator("#opCountdown")).to_be_hidden()
    expect(page.locator('#opStages [data-complete="true"]')).to_have_count(2)
    expect(page.locator('#opStages [aria-current="step"]')).to_have_text(loading)
    expect(page.locator("#opbar")).to_be_visible()
    expect(page.locator("#btnRestart")).to_be_disabled()
    expect(page.locator("#sec-logs .operation-loader")).to_have_count(0)
    expect(page.locator("#logsFilter")).to_be_enabled()
    page.screenshot(
        path=str(target / f"startup-{operation}-{language}-{width}.png"), full_page=True
    )
    page.emulate_media(reduced_motion="reduce")
    assert (
        page.locator("#sec-watchdog .op-spin").evaluate("el=>getComputedStyle(el).animationName")
        == "none"
    )
    page.evaluate("renderOp({active:null,history:[]})")
    expect(page.locator("#btnRestart")).to_be_enabled()
    expect(page.locator(".operation-loading")).to_have_count(0)


@pytest.mark.parametrize("language,width", [("ru", 390), ("en", 1440)])
@pytest.mark.parametrize(
    "operation,button",
    [
        ("check-update", "btnCheckUpd"),
        ("check-dashboard-update", "btnCheckDashboardUpd"),
        ("check-mods-update", "btnCheckMods"),
    ],
)
def test_checks_share_pending_progress_and_one_failure_result(
    page, dashboard, language, width, operation, button
):
    dashboard["overview"]["dashboardUpdate"] = {"supported": True}
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(f"localStorage.setItem('pz-language','{language}')")
    page.goto(dashboard["url"] + "/#/maintenance")
    expect(page.locator(f"#{button}")).to_be_enabled()
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")
    held = []
    page.route("**/api/action", lambda route: held.append(route))
    phase = page.evaluate(
        "source=>I18n.t(source)",
        {
            "check-update": "Проверка актуального образа",
            "check-dashboard-update": "Проверка обновлений пульта",
            "check-mods-update": "Проверка модов",
        }[operation],
    )
    active = {"op": operation, "phase": phase, "stages": [phase]}
    snapshot = {"active": None, "history": []}
    page.route("**/api/ops", lambda route: route.fulfill(json=snapshot))
    page.locator(f"#{button}").click()
    # Polling may run before POST acceptance; it must not release the pending lock.
    page.evaluate("refreshOps()")
    expect(page.locator("#opbar")).to_be_visible()
    expect(page.locator("#opPhase")).to_contain_text(
        "Подготовка…" if language == "ru" else "Preparing…"
    )
    expect(page.locator("#btnRestart")).to_be_disabled()
    assert len(held) == 1
    assert held[0].request.post_data_json == {"op": operation}
    snapshot["active"] = active
    held[0].fulfill(json={"ok": True, "started": operation})
    expect(page.locator("#opPhase")).to_contain_text(phase)
    expect(page.locator("#sec-updates .operation-label")).to_have_text(
        {
            "ru": {
                "check-update": "Идёт операция: Проверка обновлений",
                "check-dashboard-update": "Идёт операция: Проверка обновлений пульта",
                "check-mods-update": "Идёт операция: Проверка модов",
            },
            "en": {
                "check-update": "Operation in progress: Checking updates",
                "check-dashboard-update": "Operation in progress: Check console updates",
                "check-mods-update": "Operation in progress: Checking mods",
            },
        }[language][operation]
    )
    snapshot.update(
        active=None,
        history=[
            {
                "id": "failed-check",
                "op": operation,
                "ok": False,
                "finishedAt": "2026-10-09T12:00:00Z",
                "message": "Registry unavailable",
            }
        ],
    )
    page.evaluate("refreshOps()")
    expect(page.locator("#opbar")).to_be_hidden()
    expect(page.locator(f"#{button}")).to_be_enabled()
    expect(page.locator(".operation-loading")).to_have_count(0)
    page.evaluate("refreshOps()")
    page.locator("#btnNotifications").click()
    expect(page.locator('#notificationList [data-id="operation:failed-check"]')).to_have_count(1)
    expect(page.locator("#notificationList")).to_contain_text("Registry unavailable")


def test_accepted_operation_keeps_lock_when_first_state_refresh_fails(page, dashboard):
    page.goto(dashboard["url"])
    expect(page.locator("#btnRestart")).to_be_enabled()
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")
    page.route(
        "**/api/action", lambda route: route.fulfill(json={"ok": True, "started": "restart"})
    )
    page.route("**/api/ops", lambda route: route.abort())
    page.evaluate("requestOperation({op:'restart',warnSeconds:300})")
    expect(page.locator("#btnRestart")).to_be_disabled()
    expect(page.locator("#sec-status .operation-loader")).to_be_visible()
    expect(page.locator("#opbar")).to_be_visible()
    assert page.evaluate("S.actionPending") is False
    page.route(
        "**/api/ops",
        lambda route: route.fulfill(
            json={"active": {"op": "restart", "phase": "Загрузка сервера"}, "history": []}
        ),
    )
    page.evaluate("refreshOps()")
    expect(page.locator("#opPhase")).to_contain_text("Загрузка сервера")
    expect(page.locator("#btnRestart")).to_be_disabled()
    page.evaluate("renderOp({active:null,history:[]})")
    expect(page.locator("#btnRestart")).to_be_enabled()
