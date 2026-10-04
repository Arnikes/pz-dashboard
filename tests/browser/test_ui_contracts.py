"""UI audit regressions: preserve context and safe, reachable actions."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


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
    expect(page.locator('.settings-feedback[data-state="ok"]')).to_contain_text(
        "Telegram: сохранено"
    )
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
