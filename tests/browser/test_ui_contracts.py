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
