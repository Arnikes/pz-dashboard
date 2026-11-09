"""Approved editor layout retains real drafts and persistent archive check results."""

import pytest
from playwright.sync_api import expect
from test_editors import editing, env, navigate  # noqa: F401

pytestmark = pytest.mark.browser


def assert_fills_content_lane(controls, lane):
    edges = lane.evaluate("""el => {
        const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
        return {left: rect.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft),
          right: rect.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight)};
    }""")
    boxes = controls.evaluate_all("""els => els.map(el => {
        const rect = el.getBoundingClientRect(); return {left:rect.left,right:rect.right};
    })""")
    assert abs(min(box["left"] for box in boxes) - edges["left"]) <= 1
    assert abs(max(box["right"] for box in boxes) - edges["right"]) <= 1


@pytest.mark.parametrize("width", [320, 390, 1440, 2560])
def test_draft_stays_reachable(page, dashboard, editing, width):  # noqa: F811
    data, _ = editing
    ini = data / "Server/world.ini"
    ini.write_bytes(ini.read_bytes() + b"MaxPlayers=16\r\n")
    original = (data / "Server/world.ini").read_bytes()
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configActive")).to_be_hidden()
    expect(page.locator("#configActive")).to_have_text("Активен на сервере")
    navigate(page, "settings", width <= 740)
    page.locator('[data-key="PublicName"]').fill("Review season")
    page.locator('[data-key="PublicName"]').press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    expect(page.locator("#draftContext")).to_have_text("Сервер пока использует прежние значения")
    expect(page.locator(".config-field[data-changed=true] .config-change")).to_have_text("Изменено")
    expect(page.locator("#navDraftCount")).to_be_visible()
    field = page.locator('.config-field:has([data-key="PublicName"])')
    expect(field.locator(".config-control .config-key")).to_have_text("PublicName")
    assert (
        field.locator(".config-meta").bounding_box()["y"]
        >= field.locator("input").bounding_box()["y"]
        + field.locator("input").bounding_box()["height"]
    )
    assert_fills_content_lane(page.locator("#configSearch"), page.locator("#configSharedFields"))
    assert_fills_content_lane(
        page.locator("#configFlow"), page.locator("#view-settings .editor-card")
    )
    players = page.locator('.config-field:has([data-key="MaxPlayers"])')
    assert_fills_content_lane(players.locator("input"), players.locator(".config-control"))
    control = players.locator(".config-control").bounding_box()
    row = players.bounding_box()
    assert abs(control["x"] + control["width"] - row["x"] - row["width"]) <= 1
    if width >= 1440:
        assert control["width"] > 560
    assert (
        page.locator("#configFlow").bounding_box()["y"]
        < page.locator("#configTabs").bounding_box()["y"]
    )
    if width <= 740:
        bar = page.locator("#draftBar").bounding_box()
        nav = page.locator(".nav").bounding_box()
        assert 0 < bar["y"] and bar["y"] + bar["height"] <= nav["y"]
        assert bar["height"] < 170
    else:
        assert (
            page.locator("#configFlow").bounding_box()["y"]
            > page.locator(".page-heading:visible").bounding_box()["y"]
        )
    page.locator("#configDiff").click()
    expect(page.locator("#modalCancel")).to_have_text("Сбросить изменения…")
    page.keyboard.press("Escape")
    expect(page.locator("#configDiff")).to_be_focused()
    page.reload()
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Review season")
    expect(page.locator(".config-field[data-changed=true] .config-change")).to_have_text("Изменено")
    assert (data / "Server/world.ini").read_bytes() == original
    assert dashboard["actions"] == []


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("width", [320, 834, 2560])
def test_mod_tools_fill_the_available_lane(page, dashboard, editing, language, width):  # noqa: F811
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"] + "/#/mods")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    expect(page.locator("#workshopInput")).to_be_enabled()
    assert_fills_content_lane(page.locator("#configFlow"), page.locator("#view-mods .editor-card"))
    composition = page.locator("#modComposition")
    assert_fills_content_lane(page.locator("#modQuery, #modFilter, #modSortNew"), composition)
    assert_fills_content_lane(page.locator("#workshopInput, #workshopAdd button"), composition)
    assert round(page.locator("#workshopInput").bounding_box()["height"], 2) >= 44
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert dashboard["actions"] == []


def test_archive_result_is_specific_and_schedule_distinguishes_time_zones(page, dashboard):
    page.goto(dashboard["url"] + "/#/backups")
    expect(page.locator("#startupLoader")).to_be_hidden()
    page.evaluate("""() => renderBackups({ok:true,autoBackup:{enabled:true,time:'03:00',
        nextRun:'2026-10-07T03:00:00+03:00'},items:[
        {name:'first.tar.gz',size:10,mtime:'2026-10-06T00:00:00Z'},
        {name:'second.tar.gz',size:20,mtime:'2026-10-05T00:00:00Z'}]})""")
    expect(page.locator("#bkScheduleContext")).to_contain_text("ваше время")
    expect(page.locator("#bkScheduleContext")).to_contain_text("UTC+03:00")
    expect(page.locator("[data-b=verify]").first).to_contain_text("Проверить")
    expect(page.locator(".backup-result:visible")).to_have_count(0)
    page.evaluate("""() => renderOp({active:null,history:[{op:'verify-backup',ok:true,
        finishedAt:'2026-10-06T01:00:00Z',message:'OK',archive:{name:'first.tar.gz',
        files:3,hasServerIni:true,hasMapData:true}}]})""")
    # Operation history alone must not certify an archive that may have changed.
    expect(page.locator(".backup-result:visible")).to_have_count(0)
    page.evaluate("""() => renderBackups({ok:true,items:[
        {name:'first.tar.gz',size:10,mtime:'2026-10-06T00:00:00Z',verification:{
         verifiedAt:'2026-10-06T01:00:00Z',files:3,hasServerIni:true,hasMapData:true}},
        {name:'second.tar.gz',size:20,mtime:'2026-10-05T00:00:00Z',verification:null}]})""")
    expect(page.locator(".backup-row").first.locator(".backup-result")).to_contain_text("файлов: 3")
    expect(page.locator(".backup-row").nth(1).locator(".backup-result")).to_be_hidden()
    assert dashboard["actions"] == []


def test_demo_archive_dates_agree_across_sections(page, dashboard):
    page.goto(dashboard["url"])
    scenario = page.evaluate(
        """() => ({backups:DEMO.backups(),events:DEMO.events(),logs:DEMO.logs(),now:demoScenarioAt})"""
    )
    backups = scenario["backups"]
    latest = backups["items"][0]
    assert latest["mtime"][11:16] == backups["autoBackup"]["time"]
    assert backups["journal"][0]["name"] == latest["name"]
    backup_event = next(event for event in scenario["events"]["items"] if event["type"] == "backup")
    assert backup_event["ts"] == latest["mtime"] and latest["name"] in backup_event["text"]
    assert "v42." in scenario["logs"]["text"]
    assert backups["journal"][-1]["status"] == "error"


def test_mod_settings_shortcut_preserves_draft(page, dashboard, editing):  # noqa: F811
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    expect(page.locator("#configProfile")).to_be_enabled()
    page.locator('[data-key="PublicName"]').fill("Retained through shortcut")
    page.locator('[data-key="PublicName"]').press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    navigate(page, "mods")
    page.locator("#view-mods .page-heading").get_by_role("link", name="Настройки модов").click()
    expect(page.get_by_role("tab", name="Настройки модов", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    page.get_by_role("tab", name="Сервер", exact=True).click()
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Retained through shortcut")
    assert dashboard["actions"] == []
