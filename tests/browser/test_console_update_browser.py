"""Console updates share maintenance conventions in both languages and sizes."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("language,width", [("en", 1440), ("ru", 390)])
def test_console_update_flow_and_mod_timestamp(page, dashboard, language, width):
    dashboard["overview"].update(
        dashboardUpdate={
            "supported": True,
            "image": "ghcr.io/example/console:latest",
            "local": "sha256:" + "a" * 64,
        },
        modsCheck={"state": "up-to-date", "at": "2026-10-08T17:24:00Z"},
    )
    page.set_viewport_size({"width": width, "height": 1000})
    page.add_init_script(f"localStorage.setItem('pz-language','{language}')")
    page.goto(dashboard["url"] + "/#/maintenance")
    expect(page.locator("#btnCheckDashboardUpd")).to_be_enabled()
    expect(page.locator("#modsCheckNote")).to_be_hidden()
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")
    requests = []

    def action(route):
        request = route.request.post_data_json
        requests.append(request)
        if request["op"] == "check-dashboard-update":
            dashboard["overview"]["dashboardUpdate"].update(
                available=True, remote="sha256:" + "b" * 64
            )
            route.fulfill(
                json={
                    "ok": True,
                    "check": dashboard["overview"]["dashboardUpdate"],
                }
            )
        else:
            route.fulfill(json={"ok": True, "started": request["op"]})

    page.route("**/api/action", action)
    page.locator("#btnCheckDashboardUpd").click()
    expect(page.locator("#dashboardUpdPill")).to_have_text(
        "update available" if language == "en" else "есть обновление"
    )
    page.locator("#btnApplyDashboardUpd").click()
    expect(page.locator("#modalRoot")).to_contain_text(
        "game server will continue running"
        if language == "en"
        else "Игровой сервер продолжит работу"
    )
    page.locator("#modalOk").click()
    expect(page.locator("#modalRoot")).to_be_hidden()
    assert requests == [{"op": "check-dashboard-update"}, {"op": "apply-dashboard-update"}]
    page.evaluate(
        "renderOverview({...S.overview,modsCheck:{state:'needs-update',at:'2026-10-08T17:24:00Z'}})"
    )
    expect(page.locator("#modsCheckNote")).to_have_text(
        "Restart required to download updates."
        if language == "en"
        else "Для загрузки обновлений нужен рестарт."
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    target = Path(__file__).resolve().parents[2] / ".tmp-console-update-evidence"
    target.mkdir(exist_ok=True)
    page.screenshot(path=str(target / f"maintenance-{language}-{width}.png"), full_page=True)
    page.evaluate("renderOp({active:{op:'apply-dashboard-update',phase:'Updating'},history:[]})")
    expect(page.locator("#btnCheckDashboardUpd")).to_be_disabled()
    expect(page.locator("#btnApplyDashboardUpd")).to_be_disabled()


@pytest.mark.parametrize("mode", ["remote", "demo", "unsupported", "current", "compose"])
def test_console_update_availability(page, dashboard, mode):
    dashboard["overview"]["dashboardUpdate"] = {"supported": True}
    if mode == "remote":
        dashboard["overview"]["mode"] = "remote"
    elif mode == "unsupported":
        dashboard["overview"]["dashboardUpdate"] = {
            "supported": False,
            "note": "Use a published image",
        }
    elif mode == "current":
        dashboard["overview"]["dashboardUpdate"]["available"] = False
    elif mode == "compose":
        dashboard["overview"]["compose"] = False
    page.goto(dashboard["url"] + "/#/maintenance" + ("?demo=1" if mode == "demo" else ""))
    if mode == "demo":
        page.evaluate("S.demo=true;updateButtons()")
    expect(page.locator("#btnApplyDashboardUpd")).to_be_disabled()
    if mode in ("remote", "demo", "unsupported"):
        expect(page.locator("#btnCheckDashboardUpd")).to_be_disabled()
    else:
        expect(page.locator("#btnCheckDashboardUpd")).to_be_enabled()
