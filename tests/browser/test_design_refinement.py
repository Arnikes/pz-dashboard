"""Approved editor layout retains real drafts and archive operation results."""

import pytest
from playwright.sync_api import expect
from test_editors import editing, env, navigate  # noqa: F401

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_draft_stays_reachable(page, dashboard, editing, width):  # noqa: F811
    data, _ = editing
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
    assert page.locator("#configSearch").bounding_box()["width"] <= 600
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
    page.locator("#draftMore").click()
    expect(page.locator("#configDiscard")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#draftMore")).to_be_focused()
    page.reload()
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Review season")
    expect(page.locator(".config-field[data-changed=true] .config-change")).to_have_text("Изменено")
    assert (data / "Server/world.ini").read_bytes() == original
    assert dashboard["actions"] == []


def test_archive_result_is_specific_and_schedule_distinguishes_time_zones(page, dashboard):
    page.goto(dashboard["url"] + "/#/backups")
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
