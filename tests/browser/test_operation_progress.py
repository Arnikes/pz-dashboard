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
    expect(page.locator("#opStages li")).to_have_count(3 if operation == "check-mods-update" else 2)
    expect(page.locator('#opStages [data-status="pending"]')).to_have_count(
        2 if operation == "check-mods-update" else 1
    )
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


@pytest.mark.parametrize("language,width", [("ru", 390), ("en", 1440)])
def test_planned_route_keeps_future_and_skipped_steps_distinct(page, dashboard, language, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(f"localStorage.setItem('pz-language','{language}')")
    page.goto(dashboard["url"] + "/#/maintenance")
    expect(page.locator("#startupLoader")).to_be_hidden()
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")
    plan = page.evaluate(
        "OperationPlan.preview('apply-update', {running:true,warnSeconds:300,backupBeforeUpdate:true})"
    )
    active = {"op": "apply-update", "phase": "Подготовка…", "stages": [], "plan": plan}
    page.evaluate("active=>renderOp({active,history:[]})", active)
    expect(page.locator("#opStages li")).to_have_count(len(plan))
    expect(page.locator('#opStages [data-status="pending"]')).to_have_count(len(plan) - 1)
    expect(page.locator('#opStages [aria-current="step"]')).to_have_attribute(
        "data-step-id", "prepare"
    )
    expect(page.locator('#opStages [data-step-id="ready"]')).to_contain_text(
        "Ожидается" if language == "ru" else "Pending"
    )
    expect(page.locator("#opStages button, #opStages a")).to_have_count(0)

    # A skipped conditional branch stays visible without claiming work was done.
    current_index = next(index for index, step in enumerate(plan) if step["id"] == "stop")
    for index, step in enumerate(plan):
        step["status"] = (
            "skipped"
            if step["id"] == "backup"
            else "completed"
            if index < current_index
            else "current"
            if index == current_index
            else "pending"
        )
    active.update(phase="Остановка", plan=plan, stages=["Остановка"])
    page.evaluate("active=>renderOp({active,history:[]})", active)
    skipped = page.locator('#opStages [data-step-id="backup"]')
    expect(skipped).to_have_attribute("data-status", "skipped")
    expect(skipped).not_to_have_attribute("data-complete", "true")
    expect(skipped).to_contain_text("Пропущено" if language == "ru" else "Skipped")
    expect(page.locator('#opStages [aria-current="step"]')).to_have_count(1)
    expect(page.locator('#opStages [data-status="pending"]')).to_have_count(2)
    expect(page.locator("#btnRestart")).to_be_disabled()

    # A nested Done does not finish the worker or erase its remaining route.
    active["phase"] = "Готово"
    page.evaluate("active=>renderOp({active,history:[]})", active)
    expect(page.locator('#opStages [aria-current="step"]')).to_have_attribute(
        "data-step-id", "stop"
    )
    expect(page.locator("#opStages li")).to_have_count(len(plan))
    page.evaluate("renderOp({active:null,history:[]})")
    expect(page.locator("#opbar")).to_be_hidden()
    expect(page.locator("#btnRestart")).to_be_enabled()


@pytest.mark.parametrize(
    "language,width", [("ru", 390), ("en", 541), ("ru", 768), ("en", 1440), ("ru", 2560)]
)
@pytest.mark.parametrize("count", [1, 2, 3, 5, 8, 12])
def test_planned_route_markers_fill_the_available_lane(page, dashboard, language, width, count):
    page.set_viewport_size({"width": width, "height": 900})
    page.add_init_script(f"localStorage.setItem('pz-language','{language}')")
    page.goto(dashboard["url"] + "/#/maintenance")
    expect(page.locator("#startupLoader")).to_be_hidden()
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")
    labels = [
        "Подготовка…",
        "Проверка актуального образа",
        "Скачивание нового образа",
        "Страховочный бэкап",
        "Предупреждение игроков",
        "Остановка сервера",
        "Пересоздание контейнера",
        "Запуск сервера",
        "Загрузка сервера",
        "Проверка конфигурации",
        "Проверка и распаковка в песочницу",
        "Проверка готовности пульта",
    ][:count]
    active = {
        "op": "apply-update",
        "phase": labels[0],
        "message": "Preparing the operation" if language == "en" else "Подготовка операции",
        "plan": [
            {"id": f"step-{index}", "label": label, "status": "pending" if index else "current"}
            for index, label in enumerate(labels)
        ],
    }
    page.evaluate("active=>renderOp({active,history:[]})", active)
    page.evaluate("document.fonts.ready")
    expect(page.locator("#opStages li")).to_have_count(count)
    metrics = page.locator("#opStages").evaluate("""flow=>{
        const box = el => {const r=el.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};};
        const style=getComputedStyle(flow), bounds=box(flow);
        return {layout:flow.dataset.layout,left:bounds.left+parseFloat(style.paddingLeft),right:bounds.right-parseFloat(style.paddingRight),
            steps:[...flow.children].map(step=>({marker:box(step.querySelector('.route-marker')),copy:box(step.querySelector('.route-copy')),
                labelLines:(()=>{const range=document.createRange();range.selectNodeContents(step.querySelector('.op-stage-label'));return [...range.getClientRects()].map(r=>({left:r.left,right:r.right,top:r.top,width:r.width}));})(),
                connector:step.querySelector('.route-connector')?box(step.querySelector('.route-connector')):null}))};
    }""")
    steps = metrics["steps"]
    assert abs(steps[0]["marker"]["left"] - metrics["left"]) <= 1
    if width >= 1440:
        assert metrics["layout"] == "horizontal", "Wide routes must also fit more than five steps"
    if metrics["layout"] == "horizontal" and count > 1:
        assert abs(steps[-1]["marker"]["right"] - metrics["right"]) <= 1
        intervals = [
            steps[index + 1]["marker"]["left"] - step["marker"]["left"]
            for index, step in enumerate(steps[:-1])
        ]
        assert max(intervals) - min(intervals) <= 1
        for index, step in enumerate(steps[:-1]):
            connector, following = step["connector"], steps[index + 1]["marker"]
            assert abs(connector["left"] - step["marker"]["right"] - 12) <= 1
            assert abs(following["left"] - connector["right"] - 12) <= 1
            assert connector["width"] > 0
        for index, step in enumerate(steps[:-1]):
            for line in step["labelLines"]:
                for following in steps[index + 1]["labelLines"]:
                    if abs(line["top"] - following["top"]) <= 1:
                        assert following["left"] - line["right"] >= 4
        for step in steps:
            if len(step["labelLines"]) > 1:
                assert step["labelLines"][-1]["width"] >= 14, "Avoid a lone-letter final line"
    elif metrics["layout"] == "vertical":
        for index, step in enumerate(steps):
            assert abs(step["marker"]["left"] - metrics["left"]) <= 1
            if step["connector"]:
                assert step["marker"]["bottom"] < step["connector"]["top"]
                assert step["connector"]["bottom"] < steps[index + 1]["marker"]["top"]
    assert steps[-1]["connector"] is None
    assert all(
        metrics["left"] - 1 <= step["copy"]["left"] <= step["copy"]["right"] <= metrics["right"] + 1
        for step in steps
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    target = Path(__file__).resolve().parents[2] / ".tmp-operation-progress"
    target.mkdir(exist_ok=True)
    capture_evidence(
        page.locator("#opbar"), path=str(target / f"plan-{count}-{language}-{width}.png")
    )
    if width <= 540 and count == 12:
        # Tall routes remain reachable above the fixed footer and mobile navigation.
        for element in [
            page.locator("#opStages li").last,
            page.locator("#opMsg"),
            page.locator('#opbar a[href="#/console"]'),
        ]:
            element.evaluate("el=>el.scrollIntoView({block:'center',behavior:'instant'})")
            expect(element).to_be_in_viewport()
            box, footer = element.bounding_box(), page.locator(".site-footer").bounding_box()
            assert box["y"] + box["height"] <= footer["y"]
        capture_evidence(page, path=str(target / "plan-12-ru-390-scrolled.png"))
    page.emulate_media(reduced_motion="reduce", forced_colors="active")
    expect(page.locator('#opStages [aria-current="step"]')).to_be_visible()
    expect(page.locator('#opStages [data-status="pending"]')).to_have_count(count - 1)
