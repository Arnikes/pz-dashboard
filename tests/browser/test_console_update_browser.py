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
            "image": "gitea.arnike.ru/arnike/pz-console:latest",
            "imageId": "sha256:" + "a" * 64,
            "local": "sha256:" + "c" * 64,
        },
        modsCheck={"state": "up-to-date", "at": "2026-10-08T17:24:00Z"},
    )
    page.set_viewport_size({"width": width, "height": 1000})
    page.add_init_script(f"localStorage.setItem('pz-language','{language}')")
    page.goto(dashboard["url"] + "/#/maintenance")
    expect(page.locator("#btnCheckDashboardUpd")).to_be_enabled()
    expect(page.locator("#dashboardUpdImage")).to_have_text(
        "gitea.arnike.ru/arnike/pz-console:latest"
    )
    expect(page.locator("#dashboardUpdLocalCopy")).to_have_text("c" * 12)
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
                    "started": request["op"],
                }
            )
        else:
            route.fulfill(json={"ok": True, "started": request["op"]})

    page.route("**/api/action", action)
    page.locator("#btnCheckDashboardUpd").click()
    expect(page.locator("#dashboardUpdPill")).to_have_text(
        "update available" if language == "en" else "есть обновление"
    )
    expect(page.locator("#sumDashboardPill")).to_have_text(
        "update available" if language == "en" else "есть обновление"
    )
    expect(page.locator("#sumDashboardPill")).to_have_attribute("data-state", "warn")
    page.locator("#btnApplyDashboardUpd").click()
    expect(page.locator("#modalRoot")).to_contain_text(
        "game server will continue running"
        if language == "en"
        else "Игровой сервер продолжит работу"
    )
    page.locator("#modalOk").click()
    expect(page.locator("#modalRoot")).to_be_hidden()
    # Let the operation-completion refresh settle before injecting a new snapshot.
    page.wait_for_load_state("networkidle")
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
@pytest.mark.parametrize("language", ["en", "ru"])
def test_console_update_availability(page, dashboard, mode, language):
    page.add_init_script(f"localStorage.setItem('pz-language','{language}')")
    dashboard["overview"]["dashboardUpdate"] = {"supported": True}
    if mode == "remote":
        dashboard["overview"]["mode"] = "remote"
    elif mode == "unsupported":
        dashboard["overview"]["dashboardUpdate"] = {
            "supported": False,
            "note": "Use a published image",
        }
    elif mode == "current":
        dashboard["overview"]["dashboardUpdate"].update(
            available=False,
            image="gitea.arnike.ru/arnike/pz-console:latest",
            imageId="sha256:" + "a" * 64,
            local="sha256:" + "c" * 64,
            remote="sha256:" + "c" * 64,
        )
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
    if mode == "current":
        expect(page.locator("#dashboardUpdPill")).to_have_text(
            "up to date" if language == "en" else "актуально"
        )
        expect(page.locator("#dashboardUpdRemoteCopy")).to_have_text(
            "matches" if language == "en" else "совпадает"
        )


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("width", [320, 1440])
def test_overview_console_update_summary(page, dashboard, language, width):
    dashboard["overview"].update(update={"available": False}, modsCheck={"state": "up-to-date"})
    page.add_init_script(f"localStorage.setItem('pz-language','{language}')")
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(dashboard["url"])
    page.evaluate("liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer)")
    summary = page.locator("#sumDashboardPill")
    expect(summary).to_be_visible()
    states = [
        ({}, "unknown", "not checked", "не проверялось"),
        ({"supported": False}, "unknown", "not checked", "не проверялось"),
        ({"available": False}, "ok", "up to date", "актуально"),
        ({"available": True}, "warn", "update available", "есть обновление"),
        ({"error": "Registry unavailable"}, "bad", "check error", "ошибка проверки"),
    ]
    for update, state, english, russian in states:
        page.evaluate("update => renderOverview({...S.overview, dashboardUpdate:update})", update)
        expect(summary).to_have_text(english if language == "en" else russian)
        expect(summary).to_have_attribute("data-state", state)
        expect(page.locator("#sumImagePill")).to_have_attribute("data-state", "ok")
        expect(page.locator("#sumModsPill")).to_have_attribute("data-state", "ok")

    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    target = Path(__file__).resolve().parents[2] / ".tmp-console-update-evidence"
    target.mkdir(exist_ok=True)
    page.screenshot(path=str(target / f"overview-{language}-{width}.png"), full_page=True)
    page.locator("#sec-sumupd").get_by_role(
        "link", name="Console image" if language == "en" else "Образ пульта"
    ).click()
    expect(page.locator("#view-maintenance")).to_be_visible()
    expect(page.locator("#sec-dashboard-update")).to_be_visible()
