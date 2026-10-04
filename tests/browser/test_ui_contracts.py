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
