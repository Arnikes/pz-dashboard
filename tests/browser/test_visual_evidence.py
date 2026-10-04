"""Opt-in final render matrix: isolated config files and synthetic live data."""

import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_editors import editing, env  # noqa: F401

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.getenv("PZ_UI_EVIDENCE") != "1", reason="Opt-in render evidence: PZ_UI_EVIDENCE=1"
    ),
]
AXE = "async () => (await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21a','wcag21aa']}})).violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>n.target)}))"


def test_final_render_matrix(page, dashboard, editing):  # noqa: F811
    root = Path(__file__).resolve().parents[2]
    out = root / ".impeccable" / "review"
    out.mkdir(parents=True, exist_ok=True)
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    page.locator('[data-key="PublicName"]').fill("Северный берег — рабочий черновик")
    page.locator('[data-key="PublicName"]').press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    page.evaluate("""() => {liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer);
        renderPlayers({ok:true,names:['Администратор','Игрок с длинным именем'],count:2});
        renderBackups({ok:true,items:[{name:'world-long-backup.tar',mtime:new Date(Date.now()-72*3600000).toISOString(),size:60000000}],journal:[]});
        renderEvents({ok:true,items:[{ts:'2026-10-04T12:00:00Z',type:'backup',text:'Иллюстративное событие: архив создан'}]});
        renderOverview({...S.overview,serverName:'Северный берег · изолированная проверка',stats:{cpuPct:24,memPct:55,memUsed:4000000000,memLimit:8000000000},update:{error:'Иллюстративная ошибка проверки образа'}});
    }""")
    page.add_style_tag(content="* { animation:none!important;transition:none!important; }")
    page.add_script_tag(path=str(root / ".tmp-impeccable-audit" / "axe.min.js"))
    records = []
    routes = [
        "overview",
        "settings",
        "mods",
        "players",
        "maintenance",
        "backups",
        "events",
        "console",
    ]
    for width, height in [
        (320, 568),
        (390, 844),
        (768, 1024),
        (1024, 768),
        (1440, 900),
        (2048, 1152),
    ]:
        page.set_viewport_size({"width": width, "height": height})
        for route in routes:
            page.evaluate("route => location.hash = '#/' + route", route)
            expect(page.locator(f"#view-{route}")).to_be_visible()
            page.evaluate("window.scrollTo(0,0)")
            page.wait_for_timeout(80)
            records.append(
                {
                    "route": route,
                    "width": width,
                    "height": height,
                    "axe": page.evaluate(AXE),
                    "overflow": page.evaluate("document.documentElement.scrollWidth > innerWidth"),
                }
            )
            if width in (320, 390, 1440):
                page.screenshot(path=str(out / f"{route}-{width}.png"), full_page=True)
                if route == "settings" and width in (1440, 390):
                    page.screenshot(
                        path=str(out / ("desktop.png" if width == 1440 else "mobile.png")),
                        full_page=True,
                    )
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate(
        "location.hash='#/settings';renderOp({active:{op:'apply-config',phase:'Предупреждение',message:'Изолированная проверка операции',startedAt:new Date().toISOString()},history:[]})"
    )
    expect(page.locator("#opbar")).to_be_visible()
    page.evaluate("window.scrollTo(0,0)")
    page.screenshot(path=str(out / "busy-390.png"), full_page=True)
    page.evaluate("renderOp({active:null,history:[]})")
    page.set_viewport_size({"width": 844, "height": 390})
    page.evaluate("window.scrollTo(0,0)")
    page.wait_for_timeout(100)
    page.screenshot(path=str(out / "landscape-settings.png"), full_page=True)
    page.emulate_media(forced_colors="active", reduced_motion="reduce")
    page.get_by_role("tab", name="Исходники", exact=True).click()
    page.locator("#iniSource").focus()
    assert page.locator("#iniSource").evaluate(
        "el=>getComputedStyle(el).color !== 'rgba(0, 0, 0, 0)'"
    )
    page.evaluate("window.scrollTo(0,0)")
    page.screenshot(path=str(out / "forced-colors.png"), full_page=True)
    page.emulate_media(forced_colors="none")
    page.goto(dashboard["url"] + "/login.html")
    page.add_script_tag(path=str(root / ".tmp-impeccable-audit" / "axe.min.js"))
    for width in [1440, 390, 320]:
        page.set_viewport_size({"width": width, "height": 900 if width == 1440 else 844})
        records.append(
            {
                "route": "login",
                "width": width,
                "axe": page.evaluate(AXE),
                "overflow": page.evaluate("document.documentElement.scrollWidth > innerWidth"),
            }
        )
        page.screenshot(path=str(out / f"login-{width}.png"), full_page=True)
    (out / "matrix.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    assert all(not row["overflow"] and not row["axe"] for row in records), records
