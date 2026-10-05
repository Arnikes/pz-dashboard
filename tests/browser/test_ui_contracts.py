"""UI audit regressions: preserve context and safe, reachable actions."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def test_rejected_request_survives_unchanged_operation_history(page, dashboard):
    page.goto(dashboard["url"])
    expect(page.locator("#btnStart")).to_be_disabled()
    history = {
        "op": "backup",
        "ok": True,
        "finishedAt": "2026-10-04T12:00:00Z",
        "message": "Old backup",
    }
    page.evaluate("h=>renderOp({active:null,history:[h]})", history)
    page.evaluate("showActionError('start',new Error('Связь потеряна'))")
    for _ in range(3):
        page.evaluate("h=>renderOp({active:null,history:[h]})", history)
    expect(page.locator("#operationResult")).to_contain_text("Связь потеряна")
    page.locator("#operationResultDismiss").click()
    page.evaluate("h=>renderOp({active:null,history:[h]})", history)
    expect(page.locator("#operationResult")).to_be_hidden()
    history["finishedAt"] = "2026-10-04T13:00:00Z"
    page.evaluate("h=>renderOp({active:null,history:[h]})", history)
    expect(page.locator("#operationResult")).to_contain_text("Old backup")


def test_update_check_has_persistent_result_and_serializes_requests(page, dashboard):
    held = []
    page.route("**/api/action", lambda route: held.append(route))
    page.goto(dashboard["url"] + "/#/maintenance")
    expect(page.locator("#btnCheckUpd")).to_be_enabled()
    page.locator("#btnCheckUpd").click()
    expect(page.locator("#operationResult")).to_contain_text("выполняется")
    expect(page.locator("#btnCheckUpd")).to_be_disabled()
    page.evaluate("document.getElementById('btnCheckUpd').click()")
    assert len(held) == 1
    held[0].fulfill(json={"ok": True, "check": {"available": False}})
    expect(page.locator("#operationResult")).to_contain_text("образ актуален")
    page.evaluate("renderOp({active:null,history:[]});location.hash='#/settings'")
    expect(page.locator("#operationResult")).to_contain_text("образ актуален")


@pytest.mark.parametrize("name", ['Дмитрий "Север"', "Alice\nBob", "Игрок 'Север'"])
def test_player_command_preserves_exact_target_or_refuses_unsafe_encoding(page, dashboard, name):
    sent = []
    page.route(
        "**/api/rcon",
        lambda route: (
            sent.append(route.request.post_data_json),
            route.fulfill(json={"ok": True, "output": "done"}),
        ),
    )
    page.goto(dashboard["url"] + "/#/players")
    expect(page.locator("#btnStop")).to_be_enabled()
    page.evaluate("name=>confirmPlayerAction('kick',name)", name)
    page.locator("#modalOk").click()
    if '"' in name or "\n" in name:
        expect(page.locator("#modalError")).to_contain_text("имя не подменяется")
        assert sent == []
    else:
        expect(page.get_by_role("alertdialog")).to_be_hidden()
        assert sent == [{"command": f'kickuser "{name}"'}]


def test_navigation_keeps_one_stream_and_hidden_polling_does_not_overlap(page, dashboard):
    with page.expect_request("**/api/stream"):
        page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    page.evaluate("window.firstStream=liveSource")
    for route in ["settings", "mods", "players", "maintenance", "backups", "console", "events"]:
        page.evaluate("route=>location.hash='#/'+route", route)
        expect(page.locator(f"#view-{route}")).to_be_visible()
        assert page.evaluate("liveSource===firstStream && !pollingStarted")
    page.clock.install()
    held = []
    hidden_requests = []
    page.on(
        "request",
        lambda request: hidden_requests.append(request.url) if "/api/" in request.url else None,
    )
    page.route("**/api/overview", lambda route: held.append(route))
    page.evaluate("""() => {
        Object.defineProperty(document,'hidden',{value:true,configurable:true});
        document.dispatchEvent(new Event('visibilitychange'));startPolling();
    }""")
    page.clock.run_for(30000)
    assert held == []
    assert hidden_requests == []
    assert page.evaluate("liveSource===null")
    page.evaluate("""() => {
        Object.defineProperty(document,'hidden',{value:false,configurable:true});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    # Stay below the API timeout: a timed-out request may correctly be retried.
    page.clock.run_for(6000)
    assert len(held) == 1
    held[0].fulfill(json={"ok": True, "serverName": "Resumed polling", "settings": {}})
    page.wait_for_function("S.overview?.serverName === 'Resumed polling'")
    page.unroute("**/api/overview")
    with page.expect_request("**/api/overview"):
        page.clock.run_for(3000)


def test_unchanged_events_and_journal_retain_nodes_but_changes_refresh(page, dashboard):
    page.goto(dashboard["url"] + "/#/events")
    expect(page.locator("#btnStop")).to_be_enabled()
    page.evaluate(r"""() => {
        liveSource?.close(); liveSource=null; clearTimeout(sseStartupTimer);
        window.eventSnapshot={ok:true,items:[{ts:'2026-09-01T00:00:00Z',type:'backup',text:'Архив "Север" — администратор\'s'}]};
        window.journalSnapshot=[{ts:'2026-09-01T00:00:00Z',status:'ok',name:'Archive "Север"',size:1024}];
        renderEvents(eventSnapshot); renderBkJournal(journalSnapshot);
        window.oldEvent=document.querySelector('#eventsBody .event-row');
        window.oldJournal=document.querySelector('#bkJournalBody .journal-row');
        window.eventChanges=0;
        const observer=new MutationObserver(records=>eventChanges+=records.length);
        for(const id of ['eventsBody','recentBody','bkJournalBody']) observer.observe(document.getElementById(id),{subtree:true,childList:true,attributes:true,characterData:true});
        for(let i=0;i<60;i++){renderEvents(eventSnapshot);renderBkJournal(journalSnapshot);}
    }""")
    assert page.evaluate(
        "eventChanges===0 && oldEvent===document.querySelector('#eventsBody .event-row') && oldJournal===document.querySelector('#bkJournalBody .journal-row')"
    )
    page.evaluate("eventSnapshot.items[0].text='Новое событие';renderEvents(eventSnapshot)")
    expect(page.locator("#eventsBody")).to_contain_text("Новое событие")
    page.locator('#eventFilters [data-ef="ops"]').click()
    expect(page.locator("#eventsBody")).to_contain_text("Нет событий в этой категории")
    page.locator('#eventFilters [data-ef="all"]').click()
    expect(page.locator("#eventsBody")).to_contain_text("Новое событие")


def test_action_restrictions_show_recovery_in_context(page, dashboard):
    page.goto(dashboard["url"])
    page.wait_for_function("typeof S !== 'undefined' && !!S.overview")
    expect(page.locator("#btnStop")).to_be_enabled()
    page.evaluate(
        "() => { liveSource?.close(); liveSource = null; clearTimeout(sseStartupTimer); }"
    )
    page.evaluate("applyOverview({...S.overview,mode:'remote'})")
    expect(page.locator("#operationAvailability")).to_contain_text("на хосте сервера")
    expect(page.locator("#btnStop")).to_be_disabled()
    page.evaluate("location.hash='#/backups'")
    expect(page.locator("#backupAvailability")).to_contain_text("Remote")
    page.evaluate("location.hash='#/maintenance'")
    expect(page.locator("#maintenanceAvailability")).to_contain_text("RCON и игроки")
    page.evaluate("applyOverview({...S.overview,mode:'host',compose:false})")
    expect(page.locator("#maintenanceAvailability")).to_contain_text("docker compose")
    page.evaluate("renderOp({active:{op:'backup',phase:'Архив',message:'Создание'},history:[]})")
    expect(page.locator("#maintenanceAvailability")).to_contain_text("Дождитесь результата")
    page.evaluate("renderOp({active:null,history:[]});applyOverview({...S.overview,compose:true})")
    expect(page.locator("#maintenanceAvailability")).to_be_hidden()


@pytest.mark.parametrize("width", [320, 1440])
def test_local_help_is_available_without_external_assets(page, dashboard, width):
    page.set_viewport_size({"width": width, "height": 844})
    response = page.goto(dashboard["url"] + "/static/config-help.html#mods")
    assert response.status == 200
    expect(page.get_by_role("heading", name="Изменить состав модов")).to_be_visible()
    expect(page.locator("main")).to_contain_text("Записать файлы")
    expect(page.locator("main")).to_contain_text("Отменить черновик")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("link", name="Настройки", exact=True).click()
    expect(page.locator("#view-settings")).to_be_visible()


@pytest.mark.parametrize("width", [320, 1440])
def test_commands_keyboard_navigation_and_escape_restore_focus(page, dashboard, width):
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    trigger = page.locator("#btnCommands")
    trigger.focus()
    page.keyboard.press("Control+k")
    expect(page.locator("#commandSearch")).to_be_focused()
    page.keyboard.press("Escape")
    expect(trigger).to_be_focused()
    expect(page.locator("#commandDialog")).not_to_be_visible()
    trigger.click()
    page.locator("#commandSearch").fill("открыть: события")
    page.keyboard.press("ArrowDown")
    expect(page.get_by_role("button", name="Открыть: События")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#view-events")).to_be_visible()
    expect(page.locator("#commandDialog")).not_to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_commands_ignore_shortcut_in_rcon_and_open_safe_confirmation(page, dashboard):
    page.goto(dashboard["url"] + "/#/console")
    expect(page.locator("#btnStop")).to_be_enabled()
    field = page.locator("#consoleInput")
    field.fill("servermsg сохранённый ввод")
    field.press("Control+k")
    expect(page.locator("#commandDialog")).not_to_be_visible()
    expect(field).to_have_value("servermsg сохранённый ввод")
    page.locator("#btnCommands").click()
    page.locator("#commandSearch").fill("остановить")
    page.locator("#commandSearch").press("Enter")
    expect(page.locator("#modalRoot")).to_be_visible()
    expect(page.locator("#modalCancel")).to_be_focused()
    assert dashboard["actions"] == []
    page.keyboard.press("Escape")
    assert dashboard["actions"] == []


def test_commands_unavailable_reason_empty_results_and_log_filter(page, dashboard):
    page.goto(dashboard["url"])
    page.locator("#btnCommands").click()
    page.locator("#commandSearch").fill("текущую операцию")
    unavailable = page.locator("#commandResults button")
    expect(unavailable).to_be_disabled()
    expect(unavailable).to_contain_text("Сейчас нет активной операции")
    page.locator("#commandSearch").fill("нет-такой-команды")
    expect(page.locator("#commandResults")).to_contain_text("Команда не найдена")
    page.locator("#commandSearch").fill("строку в логах")
    page.locator("#commandSearch").press("Enter")
    expect(page.locator("#logsFilter")).to_be_focused()
    expect(page.locator("#view-console")).to_be_visible()


@pytest.mark.parametrize("operation", ["backup", "apply-update", "restore", "restart"])
def test_operation_visible_on_all_routes_and_result_survives_navigation(page, dashboard, operation):
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    page.evaluate(
        """op => renderOp({active:{op, phase:'Ожидание', message:'Предупреждение игроков',
        startedAt:new Date(Date.now()-65000).toISOString()},history:[]})""",
        operation,
    )
    for route in [
        "players",
        "mods",
        "settings",
        "maintenance",
        "backups",
        "events",
        "console",
        "overview",
    ]:
        page.evaluate("route => {location.hash = '#/' + route}", route)
        expect(page.locator(f"#view-{route}")).to_be_visible()
        expect(page.locator("#opbar")).to_be_visible()
        expect(page.locator("#opPhase")).to_contain_text("Ожидание")
        expect(page.locator("#opElapsed")).to_contain_text("мин")
        expect(page.locator("#btnCancelMods")).to_be_hidden()
    result = {
        "op": operation,
        "ok": False,
        "message": "Проверьте диск",
        "finishedAt": "2026-10-04T21:00:00Z",
    }
    page.evaluate("h => renderOp({active:null,history:[h]})", result)
    expect(page.locator("#operationResult")).to_contain_text("Проверьте диск")
    page.locator('#operationResult a[href="#/console"]').click()
    expect(page.locator("#view-console")).to_be_visible()
    expect(page.locator("#operationResult")).to_be_visible()
    page.locator("#operationResultDismiss").click()
    page.evaluate("h => renderOp({active:null,history:[h]})", result)
    expect(page.locator("#operationResult")).to_be_hidden()
    result.update(ok=True, message="Архив проверен", finishedAt="2026-10-04T21:01:00Z")
    page.evaluate("h => renderOp({active:null,history:[h]})", result)
    expect(page.locator("#operationResult")).to_contain_text("Архив проверен")


@pytest.mark.parametrize(
    "kind,action",
    [("players", "kick"), ("players", "ban"), ("backups", "dl"), ("backups", "restore")],
)
def test_live_list_preserves_focus_identity_and_recovers_removed_row(page, dashboard, kind, action):
    page.goto(dashboard["url"] + f"/#/{kind}")
    expect(page.locator("#btnStop")).to_be_enabled()
    page.clock.install()
    name = 'Дмитрий "Север" ' + "очень-длинное-имя-" * 12
    page.evaluate(
        """({kind,name}) => {
        window.listFrame = kind === 'players' ? {ok:true,count:2,names:[name,'Alice']} :
          {ok:true,items:[{name,mtime:'2026-10-04T20:00:00Z',size:100},{name:'second.tar',mtime:'2026-10-04T20:00:00Z',size:200}]};
        window.paintList = () => kind === 'players' ? renderPlayers(listFrame) : renderBackups(listFrame);
        // This regression isolates row reconciliation from the fixture's empty live snapshots.
        liveSource?.close(); liveSource = null; clearTimeout(sseStartupTimer);
        paintList();
        window.rowChanges = 0;
        new MutationObserver(records => {rowChanges += records.length}).observe(
          document.getElementById(kind+'Body'), {subtree:true,childList:true,attributes:true});
        window.listTimer = setInterval(paintList,1000);
        }""",
        {"kind": kind, "name": name},
    )
    selector = f'#{kind}Body button[data-{"p" if kind == "players" else "b"}="{action}"]'
    focused = page.locator(selector).first
    focused.focus()
    page.evaluate("window.originalButton = document.activeElement")
    page.clock.run_for(60000)
    assert page.evaluate("document.activeElement === originalButton && rowChanges === 0")
    # Reorder plus change the metadata: retain the same interactive node and exact recipient.
    page.evaluate("""() => {clearInterval(listTimer);
      if(listFrame.names) listFrame.names.reverse();
      else {listFrame.items.reverse();listFrame.items[1].size = 999;}
      paintList();}""")
    assert page.evaluate("document.activeElement === originalButton")
    expect(page.locator(selector).last).to_have_attribute("data-name", name)
    page.evaluate("""() => {if(listFrame.names) {listFrame.names.pop();listFrame.count=1;}
      else listFrame.items.pop();paintList();}""")
    expect(page.locator(selector)).to_be_focused()
    page.evaluate("""() => {if(listFrame.names) {listFrame.names=[];listFrame.count=0;}
      else listFrame.items=[];paintList();}""")
    expect(page.locator(f"#{kind}Body")).to_be_focused()


@pytest.mark.parametrize("failure", ["409", "503", "timeout"])
def test_rejected_confirmation_preserves_parameters_and_retries_once(page, dashboard, failure):
    requests = []
    pending = []

    def respond(route):
        requests.append(route.request.post_data_json)
        if len(requests) == 1:
            if failure == "timeout":
                pending.append(route)
            else:
                route.fulfill(status=int(failure), json={"error": "Запрос отклонён"})
        else:
            route.fulfill(json={"ok": True})

    page.route("**/api/action", respond)
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    page.clock.install()
    page.locator("#btnStop").click()
    page.locator("#warnSel").select_option("60")
    page.locator("#modalOk").click()
    if failure == "timeout":
        page.wait_for_function("S.actionPending")
        page.clock.run_for(9500)
    expect(page.locator("#modalError")).to_be_visible()
    expect(page.get_by_role("alertdialog")).to_be_visible()
    expect(page.locator("#warnSel")).to_have_value("60")
    page.locator("#modalOk").click()
    expect(page.get_by_role("alertdialog")).to_be_hidden()
    assert requests == [{"op": "stop", "warnSeconds": 60}] * 2


def test_pending_confirmation_cannot_be_submitted_twice_or_dismissed(page, dashboard):
    requests = []
    page.route("**/api/action", lambda route: requests.append(route))
    page.goto(dashboard["url"])
    page.locator("#btnStop").click()
    page.locator("#modalOk").click()
    expect(page.locator("#modalOk")).to_be_disabled()
    page.evaluate("document.getElementById('modalOk').click()")
    page.keyboard.press("Escape")
    expect(page.get_by_role("alertdialog")).to_be_visible()
    expect(page.locator("#modalCancel")).to_be_disabled()
    assert len(requests) == 1
    requests[0].fulfill(json={"ok": True})
    expect(page.get_by_role("alertdialog")).to_be_hidden()


def test_failed_telegram_save_keeps_secret_and_stale_snapshots_do_not_erase_edits(page, dashboard):
    requests = []
    pending = []

    def settings(route):
        requests.append(route.request.post_data_json)
        pending.append(route)

    page.route("**/api/settings", settings)
    page.goto(dashboard["url"] + "/#/maintenance")
    expect(page.locator("#btnStop")).to_be_enabled()
    token = "123456:test-only-secret"
    page.locator("#tgToken").fill(token)
    page.locator("#tgToken").press("Tab")
    expect(page.locator(".settings-feedback")).to_contain_text("сохраняется")
    pending.pop(0).fulfill(status=503, json={"error": "Сервис недоступен"})
    expect(page.locator('.settings-feedback[data-state="error"]')).to_contain_text("Ввод сохранён")
    expect(page.locator("#tgToken")).to_have_value(token)
    page.evaluate(
        "renderOverview({...S.overview,settings:{telegram:{chatId:'старое',enabled:false}}})"
    )
    expect(page.locator("#tgToken")).to_have_value(token)
    assert (
        page.evaluate("Object.values(localStorage).join('').includes('test-only-secret')") is False
    )
    assert (
        page.evaluate("Object.values(sessionStorage).join('').includes('test-only-secret')")
        is False
    )
    page.get_by_role("button", name="Повторить сохранение").click()
    expect(page.locator('.settings-feedback[data-state="pending"]')).to_be_visible()
    pending.pop(0).fulfill(
        json={
            "ok": True,
            "settings": {
                "version": {"epoch": "server", "revision": 5},
                "telegram": {"chatId": "", "enabled": False, "botTokenMasked": "•••cret"},
            },
        }
    )
    expect(page.locator("#tgToken")).to_have_value("")
    expect(page.locator("#toasts")).to_contain_text(
        "Токен сохранён. Введите новый, чтобы заменить."
    )
    expect(page.locator("#sec-notify .settings-feedback")).to_have_count(0)
    assert list(requests[0]) == ["telegram"]
    assert requests[0] == requests[1]


def test_settings_serializes_local_changes_and_ignores_stale_server_revision(page, dashboard):
    requests = []
    pending = []

    def settings(route):
        requests.append(route.request.post_data_json)
        pending.append(route)

    page.route("**/api/settings", settings)
    page.goto(dashboard["url"] + "/#/maintenance")
    page.locator("#autoInterval").select_option("12")
    expect(page.locator('.settings-feedback[data-state="pending"]')).to_be_visible()
    page.locator("#autoInterval").select_option("24")
    page.locator("#hUpdates").click()
    page.evaluate(
        "renderOverview({...S.overview,settings:{version:{epoch:'server',revision:1},autoUpdate:{intervalHours:6}}})"
    )
    expect(page.locator("#autoInterval")).to_have_value("24")
    assert len(requests) == 1
    pending.pop(0).fulfill(
        json={
            "ok": True,
            "settings": {
                "version": {"epoch": "server", "revision": 2},
                "autoUpdate": {"intervalHours": 12},
            },
        }
    )
    page.wait_for_function("settingState('autoUpdate').pending === 1")
    expect(page.locator("#autoInterval")).to_have_value("24")
    page.wait_for_timeout(100)
    assert len(requests) == 2
    pending.pop(0).fulfill(
        json={
            "ok": True,
            "settings": {
                "version": {"epoch": "server", "revision": 3},
                "autoUpdate": {"intervalHours": 24},
            },
        }
    )
    expect(page.locator('.settings-feedback[data-state="ok"]')).to_be_visible()
    page.evaluate(
        "renderOverview({...S.overview,settings:{version:{epoch:'server',revision:2},autoUpdate:{intervalHours:12}}})"
    )
    expect(page.locator("#autoInterval")).to_have_value("24")
    assert requests[0]["autoUpdate"]["intervalHours"] == 12
    assert requests[1]["autoUpdate"]["intervalHours"] == 24


def test_destructive_dialog_starts_with_safe_focus_and_restore_ack_stays_required(page, dashboard):
    page.goto(dashboard["url"])
    page.locator("#btnStop").click()
    expect(page.locator("#modalCancel")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.get_by_role("alertdialog")).to_be_hidden()
    expect(page.locator("#btnStop")).to_be_focused()
    assert dashboard["actions"] == []
    page.evaluate("confirmRestore('world.tar')")
    expect(page.locator("#modalCancel")).to_be_focused()
    expect(page.locator("#modalOk")).to_be_disabled()
    page.locator("#restoreAck").check()
    expect(page.locator("#modalOk")).to_be_enabled()
    page.locator("#restoreAck").uncheck()
    expect(page.locator("#modalOk")).to_be_disabled()
    page.keyboard.press("Escape")
    assert dashboard["actions"] == []


@pytest.mark.parametrize("key", ["Enter", "Space"])
def test_explicit_copy_button_works_from_keyboard_with_full_digest(page, dashboard, key):
    value = "sha256:" + "0123456789abcdef" * 4
    dashboard["overview"]["update"] = {"local": value}
    page.goto(dashboard["url"])
    expect(page.locator("#btnStop")).to_be_enabled()
    page.evaluate("value => renderOverview({...S.overview,update:{local:value}})", value)
    page.locator("#serverDetails summary").click()
    button = page.get_by_role("button", name="Скопировать локальный digest").first
    button.focus()
    button.press(key)
    expect(page.locator("#mDigestCopy")).to_have_attribute("data-copied", "true")
    expect(page.locator("#mDigestCopy .copy-icon-check")).to_be_visible()
    page.evaluate("""() => { const target=document.createElement('textarea');
      target.id='pasteTarget';document.body.appendChild(target); }""")
    target = page.locator("#pasteTarget")
    target.focus()
    target.press("ControlOrMeta+V")
    expect(target).to_have_value(value)


def test_backup_schedule_switch_has_name_and_update_links_are_reachable_at_320(page, dashboard):
    page.set_viewport_size({"width": 320, "height": 568})
    page.goto(dashboard["url"] + "/#/backups")
    expect(page.get_by_role("switch", name="Бэкап по расписанию")).to_be_visible()
    page.evaluate("location.hash='#/overview'")
    page.evaluate("""() => {renderOverview({...S.overview,update:{at:'2026-10-04T21:00:00Z'},
      modsCheck:{at:'2026-10-04T21:00:00Z'}});
      document.getElementById('sumImageMeta').textContent='Очень длинные метаданные проверки образа';
      document.getElementById('sumModsMeta').textContent='Очень длинные метаданные проверки модов';}""")
    for name in ["Образ Docker", "Моды Workshop"]:
        link = page.get_by_role("link", name=name, exact=True)
        expect(link).to_be_visible()
        assert link.bounding_box()["width"] > 80
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
