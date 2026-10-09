"""Countdown ending transitions into startup while widgets stay unavailable."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("language,width", [("ru", 390), ("en", 1440)])
def test_countdown_finishing_keeps_lock_until_game_ready(page, dashboard, language, width):
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
        """warning=>renderOp({active:{op:'restart',phase:warning,stages:['Подготовка…',warning],
        countdownEndsAt:Date.now()/1000+300},history:[]})""",
        warning,
    )
    expect(page.locator("#opCountdown")).to_have_text("5:00")
    expect(page.locator("#opElapsed")).to_have_count(0)
    expect(page.locator("#maintenanceAvailability")).to_be_hidden()
    target = Path(__file__).resolve().parents[2] / ".tmp-operation-lock-evidence"
    target.mkdir(exist_ok=True)
    page.screenshot(path=str(target / f"countdown-{language}-{width}.png"), full_page=True)
    page.clock.run_for(300000)
    expect(page.locator("#opCountdown")).to_have_text("0:00")
    expect(page.locator("#btnRestart")).to_be_disabled()
    expect(page.locator("#sec-watchdog .operation-loader")).to_be_visible()
    page.evaluate(
        """stages=>renderOp({active:{op:'restart',phase:stages[2],stages,
        countdownEndsAt:null},history:[]})""",
        [warning, stopping, loading],
    )
    expect(page.locator("#opCountdown")).to_be_hidden()
    expect(page.locator('#opStages [data-complete="true"]')).to_have_count(2)
    expect(page.locator('#opStages [aria-current="step"]')).to_have_text(loading)
    expect(page.locator("#opbar")).to_be_visible()
    expect(page.locator("#btnRestart")).to_be_disabled()
    expect(page.locator("#sec-logs .operation-loader")).to_have_count(0)
    expect(page.locator("#logsFilter")).to_be_enabled()
    page.screenshot(path=str(target / f"startup-{language}-{width}.png"), full_page=True)
    page.emulate_media(reduced_motion="reduce")
    assert (
        page.locator("#sec-watchdog .op-spin").evaluate("el=>getComputedStyle(el).animationName")
        == "none"
    )
    page.evaluate("renderOp({active:null,history:[]})")
    expect(page.locator("#btnRestart")).to_be_enabled()
    expect(page.locator(".operation-loading")).to_have_count(0)


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
