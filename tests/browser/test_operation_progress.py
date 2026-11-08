"""Operation routes show honest stages and keep controls locked through completion."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from browser_evidence import capture_evidence

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("language,width", [("ru", 390), ("en", 1440)])
@pytest.mark.parametrize("operation", ["restart", "apply-mods-update", "mods-restart"])
def test_countdown_finishing_keeps_lock_until_game_ready(
    page, dashboard, language, width, operation
):
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(f"localStorage.setItem('pz-language','{language}')")
    page.goto(dashboard["url"] + "/#/maintenance")
    expect(page.locator("#startupLoader")).to_be_hidden()
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
    capture_evidence(
        page, path=str(target / f"countdown-{operation}-{language}-{width}.png"), full_page=True
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
    expect(page.locator('#opStages [aria-current="step"]')).to_contain_text(loading)
    expect(page.locator("#opbar")).to_be_visible()
    expect(page.locator("#btnRestart")).to_be_disabled()
    expect(page.locator("#sec-logs .operation-loader")).to_have_count(0)
    expect(page.locator("#logsFilter")).to_be_enabled()
    capture_evidence(
        page, path=str(target / f"startup-{operation}-{language}-{width}.png"), full_page=True
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
    expect(page.locator('#opStages [aria-current="step"]')).to_contain_text(
        "Подготовка…" if language == "ru" else "Preparing…"
    )
    expect(page.locator("#opStages")).to_be_visible()
    expect(page.locator("#opStages li")).to_have_count(1)
    expect(page.locator("#btnRestart")).to_be_disabled()
    assert len(held) == 1
    assert held[0].request.post_data_json == {"op": operation}
    snapshot["active"] = active
    held[0].fulfill(json={"ok": True, "started": operation})
    expect(page.locator('#opStages [aria-current="step"]')).to_contain_text(phase)
    expect(page.locator("#opStages li")).to_have_count(1)
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
    expect(page.locator('#opStages [aria-current="step"]')).to_contain_text("Загрузка сервера")
    expect(page.locator("#btnRestart")).to_be_disabled()
    page.evaluate("renderOp({active:null,history:[]})")
    expect(page.locator("#btnRestart")).to_be_enabled()


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("stage_payload", ["missing", "empty", "behind"])
def test_current_stage_and_details_survive_incomplete_stage_history(
    page, dashboard, language, stage_payload
):
    page.add_init_script(f"localStorage.setItem('pz-language','{language}')")
    page.goto(dashboard["url"] + "/#/maintenance")
    expect(page.locator("#startupLoader")).to_be_hidden()
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")
    stopping, loading = page.evaluate(
        "sources=>sources.map(source=>I18n.t(source))", ["Остановка", "Загрузка сервера"]
    )
    detail = (
        "Ожидаем готовность PZ и RCON; настройки пока недоступны."
        if language == "ru"
        else "Waiting for PZ and RCON readiness; settings are still unavailable."
    )
    active = {"op": "restart", "phase": loading, "message": detail}
    if stage_payload != "missing":
        active["stages"] = [stopping] if stage_payload == "behind" else []
    page.evaluate("active=>renderOp({active,history:[]})", active)
    expect(page.locator("#opStages")).to_be_visible()
    expect(page.locator("#opStages li")).to_have_count(2 if stage_payload == "behind" else 1)
    current = page.locator('#opStages [aria-current="step"]')
    expect(current).to_contain_text(loading)
    expect(current).to_have_attribute(
        "aria-label", f"{loading}: {'Выполняется' if language == 'ru' else 'In progress'}"
    )
    expect(page.locator("#opMsg")).to_be_visible()
    expect(page.locator("#opMsg")).to_have_text(detail)
    expect(page.locator("#btnRestart")).to_be_disabled()
    # Stages describe progress; only explicit controls can perform actions.
    expect(page.locator("#opStages button, #opStages a")).to_have_count(0)


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("width", [320, 390, 768, 1440])
def test_route_preserves_repeated_stages_and_fits_countdown_actions(
    page, dashboard, language, width
):
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(f"localStorage.setItem('pz-language','{language}')")
    page.goto(dashboard["url"] + "/#/maintenance")
    expect(page.locator("#startupLoader")).to_be_hidden()
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")
    page.evaluate("document.fonts.ready")
    stages = page.evaluate(
        "sources=>sources.map(source=>I18n.t(source))",
        [
            "Проверка актуального образа",
            "Скачивание нового образа",
            "Создание архива",
            "Предупреждение игроков",
            "Остановка сервера",
            "Пересоздание контейнера",
            "Запуск сервера",
            "Загрузка сервера",
            "Запуск сервера",
            "Загрузка сервера",
        ],
    )
    detail = (
        "Ждём ответа PZ и RCON. Сервер ещё запускается."
        if language == "ru"
        else "Waiting for PZ and RCON. The server is still starting."
    )
    active = {"op": "apply-update", "phase": stages[-1], "stages": stages, "message": detail}
    page.evaluate("active=>renderOp({active,history:[]})", active)
    items = page.locator("#opStages li")
    # Every displayed step comes from observed history, including return visits.
    expect(items).to_have_count(len(stages))
    for item, phase in zip(items.all(), stages, strict=True):
        expect(item).to_contain_text(phase)
        assert item.get_attribute("aria-label").startswith(phase + ": ")
    expect(page.locator('#opStages [data-complete="true"]')).to_have_count(len(stages) - 1)
    expect(page.locator('#opStages [aria-current="step"]')).to_contain_text(stages[-1])
    expect(page.locator("#opMsg")).to_be_visible()
    expect(page.locator("#opMsg")).to_have_text(detail)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert items.evaluate_all(
        "nodes=>nodes.every(node=>node.getBoundingClientRect().right <= innerWidth)"
    )

    warning = page.evaluate("I18n.t('Предупреждение игроков')")
    active.update(
        phase=warning,
        stages=[warning],
        cancellable=True,
        countdownEndsAt=page.evaluate("Date.now()/1000+300"),
        message=detail,
    )
    page.evaluate("active=>renderOp({active,history:[]})", active)
    expect(page.locator("#opStages li")).to_have_count(1)
    expect(page.locator("#opCountdown")).to_be_visible()
    expect(page.locator("#opElapsed")).to_have_count(0)
    cancel = page.locator("#btnCancelUpdate")
    expect(cancel).to_be_enabled()
    assert cancel.bounding_box()["height"] >= 44
    cancel.focus()
    expect(cancel).to_be_focused()
    logs = page.locator('#opbar a[href="#/console"]')
    logs.focus()
    expect(logs).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for element in [cancel, logs, page.locator("#opCountdown")]:
        box = element.bounding_box()
        assert box["x"] >= 0
        assert box["x"] + box["width"] <= width

    page.emulate_media(reduced_motion="reduce", forced_colors="active")
    current = page.locator('#opStages [aria-current="step"]')
    expect(current).to_be_visible()
    expect(current).to_contain_text(warning)
    assert current.evaluate(
        "node=>[node,...node.querySelectorAll('*')].every(el=>getComputedStyle(el).animationName==='none')"
    )
    expect(cancel).to_be_visible()


@pytest.mark.parametrize("language", ["ru", "en"])
def test_nested_completion_does_not_claim_the_active_operation_finished(page, dashboard, language):
    page.add_init_script(f"localStorage.setItem('pz-language','{language}')")
    page.goto(dashboard["url"] + "/#/maintenance")
    expect(page.locator("#startupLoader")).to_be_hidden()
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")
    download, archive, done, warning = page.evaluate(
        "sources=>sources.map(source=>I18n.t(source))",
        ["Скачивание нового образа", "Создание архива", "Готово", "Предупреждение игроков"],
    )
    active = {
        "op": "apply-update",
        "phase": done,
        "stages": [download, archive],
        "message": "Archive created",
    }
    page.evaluate("active=>renderOp({active,history:[]})", active)
    expect(page.locator("#opbar")).to_be_visible()
    expect(page.locator("#opStages li")).to_have_count(2)
    expect(page.locator('#opStages [aria-current="step"]')).to_contain_text(archive)
    expect(page.locator('#opStages [data-complete="true"]')).to_have_count(1)
    expect(page.locator("#btnRestart")).to_be_disabled()
    active.update(phase=warning, stages=[download, archive, warning])
    page.evaluate("active=>renderOp({active,history:[]})", active)
    expect(page.locator('#opStages [aria-current="step"]')).to_contain_text(warning)
    expect(page.locator('#opStages [data-complete="true"]')).to_have_count(2)
