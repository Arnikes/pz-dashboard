"""Browser editors use the real draft/metadata service with isolated file fixtures."""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import i18n
from playwright.sync_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_configeditor import env, editor, INI, saved_verification  # noqa: E402,F401


@pytest.fixture
def editing(page, dashboard, env):  # noqa: F811 (imported pytest fixture)
    def api(route):
        url = urlsplit(route.request.url)
        query = parse_qs(url.query)
        file = query.get("file", ["world.ini"])[0]
        body = route.request.post_data_json if route.request.method == "POST" else {}
        locale_token = i18n.LANGUAGE.set(i18n.resolve(route.request.headers))
        try:
            if url.path == "/api/server-configs":
                result = editor.profiles()
            elif url.path == "/api/config-draft":
                result = editor.patch(body) if body else editor.draft(file)
            elif url.path == "/api/config-validate":
                result = editor.validate(
                    body["file"],
                    prepare=body.get("prepare", False),
                    draft_revision=body.get("draftRevision"),
                )
            elif url.path == "/api/config-verify":
                result = editor.verify_running(body)
            elif url.path == "/api/config-history":
                result = editor.restore_history(body) if body else editor.history(file)
            elif url.path == "/api/mods":
                result = editor.mod_response(file, draft_mode=True)
            elif url.path == "/api/modpack":
                result = editor.modpack(body)
            else:
                route.fallback()
                return
            route.fulfill(json=i18n.present(result))
        except editor.EditorError as error:
            route.fulfill(
                status=error.status, json=i18n.present({"ok": False, "error": str(error)})
            )
        finally:
            i18n.LANGUAGE.reset(locale_token)

    page.route("**/api/**", api)
    return env


def navigate(page, route, mobile=False):
    if mobile and route not in ("overview", "settings", "mods"):
        page.locator("#navMore").click()
        page.locator(f'#moreMenu a[href="#/{route}"]').click()
    else:
        page.locator(f'.nav [data-route="{route}"]').click()
    # Hash navigation finishes before the hashchange handler updates the views.
    expect(page.locator(f"#view-{route}")).to_be_visible()


@pytest.mark.parametrize("width", [320, 1440])
def test_field_help_supports_hover_focus_and_tap_without_changing_draft(
    page, dashboard, editing, width
):
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    page.locator("#configSearch").fill("Unknown")
    field = page.locator('.config-field:has([data-key="Unknown"])')
    trigger = field.get_by_role("button", name="О настройке «Unknown»")
    tip = field.locator("[role=tooltip]")
    expect(tip).to_be_hidden()
    original = field.locator("[data-key]").input_value()
    trigger.hover()
    expect(tip).to_be_visible()
    tip.hover()
    expect(tip).to_be_visible()
    page.locator("#configSearch").hover()
    expect(tip).to_be_hidden()
    trigger.focus()
    expect(tip).to_be_visible()
    expect(tip).to_contain_text("ограничения не определены")
    assert (
        tip.get_attribute("id")
        in field.locator("[data-key]").get_attribute("aria-describedby").split()
    )
    page.keyboard.press("Escape")
    expect(tip).to_be_hidden()
    expect(trigger).to_be_focused()
    trigger.click()
    expect(tip).to_be_visible()
    bounds = tip.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 844
    trigger.click()
    expect(tip).to_be_hidden()
    trigger.click()
    page.locator("#configSearch").click()
    expect(tip).to_be_hidden()
    expect(field.locator("[data-key]")).to_have_value(original)
    assert dashboard["actions"] == []
    trigger.click()
    page.screenshot(path=str(editing[0].parent / f"field-help-{width}.png"))


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_field_help_keeps_viewport_coordinates_during_page_animation(
    page, dashboard, editing, width
):
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator('[data-key="PublicName"]')).to_be_enabled()
    # Hold the route's transform so the test does not depend on CI rendering speed.
    page.locator("#view-settings").evaluate("""el => {
        const animation = el.animate(
            [{transform:'translateY(4px)'}, {transform:'translateY(0)'}],
            {duration:1000, fill:'both'});
        animation.pause();
        animation.currentTime = 0;
    }""")
    page.locator("#configSearch").fill("Unknown")
    field = page.locator('.config-field:has([data-key="Unknown"])')
    trigger = field.get_by_role("button", name="О настройке «Unknown»")
    tip = field.locator("[role=tooltip]")
    trigger.focus()
    expect(tip).to_be_visible()
    bounds = tip.bounding_box()
    assert bounds["x"] >= 12 and bounds["x"] + bounds["width"] <= width - 12
    assert bounds["y"] >= 12 and bounds["y"] + bounds["height"] <= 844 - 12
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(editing[0].parent / f"animated-field-help-{width}.png"))
    page.keyboard.press("Escape")
    expect(tip).to_be_hidden()
    expect(trigger).to_be_focused()
    assert dashboard["actions"] == []


@pytest.mark.parametrize("width", [320, 1440])
def test_field_help_survives_scroll_after_hover_and_inside_tooltip(page, dashboard, editing, width):
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator('[data-key="PublicName"]')).to_be_enabled()
    page.locator("#configSearch").fill("Unknown")
    field = page.locator('.config-field:has([data-key="Unknown"])')
    trigger = field.get_by_role("button", name="О настройке «Unknown»")
    tip = field.locator("[role=tooltip]")
    tip.evaluate("el => el.textContent = 'PublicName\\n'.repeat(200)")
    trigger.hover()
    expect(tip).to_be_visible()
    # A scroll notification can arrive after pointerover during browser auto-scroll.
    page.evaluate("window.dispatchEvent(new Event('scroll'))")
    expect(tip).to_be_visible()
    tip.hover()
    tip.evaluate("el => el.scrollTop = 100")
    expect(tip).to_have_js_property("scrollTop", 100)
    expect(tip).to_be_visible()
    page.mouse.click(2, 2)
    expect(tip).to_be_hidden()
    assert dashboard["actions"] == []


def test_attention_routes_each_problem_and_preserves_focused_action(page, dashboard, editing):
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    page.locator('[data-key="PublicName"]').fill("Attention draft")
    page.locator('[data-key="PublicName"]').press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    navigate(page, "overview")
    draft = page.locator('[data-attention-key="draft"]')
    backup = page.locator('[data-attention-key="backup-missing"]')
    expect(draft).to_have_attribute("href", "#/settings")
    expect(backup).to_have_attribute("href", "#/backups")
    backup.focus()
    page.evaluate(
        "renderOverview({...S.overview,update:{error:'Проверка образа недоступна'}});ConfigEditor.operationChanged()"
    )
    expect(backup).to_be_focused()
    expect(page.locator('[data-attention-key="image"]')).to_have_attribute("href", "#/maintenance")
    backup.click()
    expect(page.locator("#view-backups")).to_be_visible()
    assert dashboard["actions"] == []


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_configuration_stages_keep_draft_and_only_navigate(page, dashboard, editing, width):
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    expect(page.locator("#configFlow")).to_be_visible()
    expect(page.locator('[data-flow="launch"]')).to_be_disabled()
    field = page.locator('[data-key="PublicName"]')
    field.fill("Staged draft")
    field.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    expect(page.locator("#flowDraft")).to_have_text("Есть правки")
    expect(page.locator("#flowFiles")).to_have_text("Ожидают записи")
    expect(page.locator("#flowLaunch")).not_to_have_text("Подтверждён")
    page.locator('[data-flow="files"]').click()
    expect(page.get_by_role("alertdialog")).to_contain_text("Staged draft")
    page.locator("#modalCancel").click()
    page.locator('[data-flow="launch"]').click()
    expect(page.locator("#configApply")).to_be_focused()
    expect(page.get_by_role("alertdialog")).to_be_hidden()
    assert dashboard["actions"] == []
    if width <= 740:
        page.locator("#navMore").click()
        menu = page.locator("#moreMenu").bounding_box()
        nav = page.locator(".nav").bounding_box()
        assert menu["y"] + menu["height"] <= nav["y"]
        assert menu["y"] >= 0
        page.keyboard.press("Escape")
        expect(page.locator("#configProfile")).to_be_visible()
    expect(field).to_have_value("Staged draft")


def test_commands_find_settings_and_mods_without_losing_draft(page, dashboard, editing):
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    page.locator('[data-key="PublicName"]').fill("Quick access draft")
    page.locator("#btnCommands").click()
    page.locator("#commandSearch").fill("настройку мира")
    page.locator("#commandSearch").press("Enter")
    expect(page.locator("#configSearch")).to_be_focused()
    expect(page.locator('#configTabs [data-tab="world"]')).to_have_attribute(
        "aria-selected", "true"
    )
    expect(page.locator("#draftBar")).to_be_visible()
    page.locator("#btnCommands").click()
    page.locator("#commandSearch").fill("настройку сервера")
    page.locator("#commandSearch").press("Enter")
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Quick access draft")
    page.locator("#btnCommands").click()
    page.locator("#commandSearch").fill("мод в порядке")
    page.locator("#commandSearch").press("Enter")
    expect(page.locator("#orderQuery")).to_be_focused()
    expect(page.locator('#modTabs [data-tab="order"]')).to_have_attribute("aria-selected", "true")


def test_unchanged_editor_status_retains_draft_controls_and_busy_still_locks(
    page, dashboard, editing
):
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    page.locator('[data-key="PublicName"]').fill("Stable controls")
    page.locator('[data-key="PublicName"]').press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    page.locator("#configDiff").focus()
    page.evaluate("""() => {
        liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer);
        ConfigEditor.operationChanged();window.editorStatusChanges=0;
        const observer=new MutationObserver(records=>editorStatusChanges+=records.length);
        for(const id of ['draftBar','configFields','editorAttention'])observer.observe(document.getElementById(id),{subtree:true,childList:true,attributes:true,characterData:true});
        for(let i=0;i<60;i++)ConfigEditor.operationChanged();
    }""")
    assert page.evaluate("editorStatusChanges===0")
    expect(page.locator("#configDiff")).to_be_focused()
    page.evaluate("renderOp({active:{op:'backup',phase:'Архив'},history:[]})")
    expect(page.locator("#configApply")).to_be_disabled()
    expect(page.locator('[data-key="PublicName"]')).to_be_disabled()
    page.evaluate("renderOp({active:null,history:[]})")
    expect(page.locator('[data-key="PublicName"]')).to_be_enabled()
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Stable controls")


@pytest.mark.parametrize("width", [390, 768, 1440, 2048])
def test_editor_layout_controls_and_draft_do_not_cover_content(page, dashboard, editing, width):
    data, _ = editing
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings", width <= 740)
    expect(page.locator("#draftBar")).to_be_hidden()
    page.locator('[data-key="PublicName"]').fill("Layout draft")
    page.locator('[data-key="PublicName"]').press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    expect(page.locator("#draftBar")).to_be_visible()
    metrics = page.evaluate("""() => {
        const box = s => document.querySelector(s).getBoundingClientRect();
        const bar = box('#draftBar'), nav = box('.nav'), header = box('.topbar');
        const search = box('#configSearch'), card = box('#view-settings .card');
        const columns = getComputedStyle(document.querySelector('.config-grid')).gridTemplateColumns.split(' ').length;
        return {barTop:bar.top,barBottom:bar.bottom,navTop:nav.top,navBottom:nav.bottom,
            headerBottom:header.bottom,headerHeight:header.height,searchWidth:search.width,cardWidth:card.width,
            columns,overflow:document.documentElement.scrollWidth > innerWidth};
    }""")
    assert not metrics["overflow"] and metrics["columns"] <= 3, metrics
    assert min(500, metrics["cardWidth"] - 50) <= metrics["searchWidth"] <= 600
    if width <= 740:
        assert metrics["headerHeight"] < 170
        assert page.locator("#draftBar").evaluate("el=>getComputedStyle(el).position") == "fixed"
        assert metrics["barBottom"] <= metrics["navTop"]
        assert metrics["barBottom"] - metrics["barTop"] < 160
    else:
        assert metrics["navTop"] >= metrics["headerBottom"]
        assert metrics["cardWidth"] <= 1500
    # Scroll to the last field; the measured bottom space must clear the draft.
    page.evaluate("scrollTo(0, document.documentElement.scrollHeight)")
    last = page.locator("#configFields [data-key]:visible").last.bounding_box()
    assert last["y"] + last["height"] < page.locator("#draftBar").bounding_box()["y"]
    assert (data / "Server/world.ini").read_bytes() == INI.encode()
    page.evaluate("scrollTo(0, 0)")
    page.screenshot(path=str(data.parent / f"review-settings-{width}.png"), full_page=False)


def test_search_restores_collapsed_groups_and_shows_empty_result(page, dashboard, editing):
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    group = page.locator('details[data-group-key="server:Доступ и игроки"]')
    group.locator("summary").click()
    expect(group).not_to_have_attribute("open", "")
    page.locator("#configSearch").fill("PublicName")
    expect(group).to_have_attribute("open", "")
    page.locator("#configSearch").fill("no-match-setting")
    expect(page.locator("#configFields")).to_contain_text("Нет настроек, соответствующих поиску")
    page.locator("#configSearch").fill("")
    expect(group).not_to_have_attribute("open", "")


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("width", [390, 1440])
def test_settings_help_keeps_only_decision_relevant_content(
    page, dashboard, editing, monkeypatch, language, width
):
    data, ctx = editing
    ini_path = data / "Server/world.ini"
    ini_path.write_bytes(
        (
            INI + "MaxPlayers=32\r\n# Min: 0\r\nSaveWorldEveryMinutes=15\r\nChatStreams=s,r,a\r\n"
        ).encode()
    )
    ctx["owners"]["MaxPlayers"] = "MAX_PLAYERS"
    (data / "Server/world_SandboxVars.lua").write_text(
        "SandboxVars={VERSION=5, StartYear=1993, FoodLootNew=0.6, DayLength=4, Mod={Count=2}}",
        encoding="utf-8",
    )
    mod_media = data / "steamapps/workshop/content/108600/111/mods/PluginFolder/42/media"
    mod_media.mkdir(parents=True, exist_ok=True)
    (mod_media / "sandbox-options.txt").write_text(
        "option Mod.Count { type=integer, max=100, default=2, page=ModPage, }\n"
        "option Mod.Absent { type=boolean, default=true, page=ModPage, }",
        encoding="utf-8",
    )
    editor.workshop.invalidate()
    monkeypatch.setattr(
        editor.workshop,
        "vanilla_translations",
        lambda *args, **kwargs: {
            "Sandbox_DayLength": "Длительность дня",
            "Sandbox_DayLength_tooltip": "Уникальное объяснение длительности дня",
        },
    )
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    assert page.locator("#view-settings .page-description").count() == 0
    plain = page.locator('.config-field:has([data-key="PublicName"])')
    expect(plain.locator(".config-meta")).to_have_text("PublicName")
    assert plain.locator("[data-help]").count() == 0
    players = page.locator('.config-field:has([data-key="MaxPlayers"])')
    expect(players.locator(".config-meta")).to_contain_text("1–100")
    expect(players.locator(".hint")).to_contain_text("MAX_PLAYERS")
    expect(players.locator("[data-key]")).to_be_disabled()
    minimum = "Минимум: 0" if language == "ru" else "Minimum: 0"
    save = page.locator('.config-field:has([data-key="SaveWorldEveryMinutes"])')
    expect(save.locator(".config-meta")).to_contain_text(minimum)
    assert save.locator("[data-help]").count() == 0
    page.locator("#configSearch").fill("ChatStreams")
    streams = page.locator('.config-field:has([data-key="ChatStreams"])')
    expect(streams.locator(".hint")).to_have_text(
        "По одной записи в строке." if language == "ru" else "One entry per line."
    )
    page.locator("#configSearch").fill("")
    page.locator('#configTabs [data-tab="world"]').click()
    start = page.locator('.config-field:has([data-key="StartYear"])')
    expect(start.locator(".config-scope")).to_be_visible()
    start.locator("[data-help]").click()
    expect(start.locator("[role=tooltip]")).to_be_visible()
    page.keyboard.press("Escape")
    expect(start.locator("[role=tooltip]")).to_be_hidden()
    expect(
        page.locator('.config-field:has([data-key="FoodLootNew"]) .config-meta')
    ).to_contain_text(minimum)
    day = page.locator('.config-field:has([data-key="DayLength"])')
    expect(day.locator(".config-meta")).to_have_text("DayLength")
    expect(day.locator("[role=tooltip]")).to_contain_text("Уникальное объяснение")
    # Every accessible description still resolves after tab/search rerenders,
    # including fields whose redundant help control was removed.
    missing = page.locator("#configFields [aria-describedby]").evaluate_all("""els =>
        els.flatMap(el => el.getAttribute('aria-describedby').split(/\\s+/))
           .filter(id => !document.getElementById(id))""")
    assert missing == []
    page.locator('#configTabs [data-tab="custom"]').click()
    count = page.locator('.config-field:has([data-key="Mod.Count"])')
    expect(count.locator(".config-meta")).to_contain_text(
        "Максимум: 100" if language == "ru" else "Maximum: 100"
    )
    assert count.locator("[data-help]").count() == 0
    absent = page.locator('.config-field:has([data-key="Mod.Absent"])')
    absent.locator("[data-help]").click()
    expect(absent.locator("[role=tooltip]")).to_contain_text(
        "Значение по умолчанию ещё не записано в файл."
        if language == "ru"
        else "Default value has not been written to the file yet."
    )
    page.keyboard.press("Escape")
    page.locator('#configTabs [data-tab="sources"]').click()
    expect(page.locator("#configSources > .hint")).to_have_text(
        "Оставьте __PZ_SECRET_UNCHANGED__, чтобы сохранить текущий пароль."
        if language == "ru"
        else "Keep __PZ_SECRET_UNCHANGED__ to preserve the current password."
    )
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    page.locator('#configTabs [data-tab="server"]').click()
    page.screenshot(path=str(data.parent / f"settings-hints-{language}-{width}.png"))
    assert not editor.draft("world.ini")["changed"]
    assert dashboard["actions"] == []


@pytest.mark.parametrize("width", [390, 1440])
def test_game_tooltip_markup_is_readable_and_never_executed(page, dashboard, editing, width):
    data, _ = editing
    base = data / "steamapps/workshop/content/108600/111/mods/PluginFolder/42/media"
    (base / "lua/shared/Translate/RU").mkdir(parents=True)
    (base / "sandbox-options.txt").write_text(
        "option Mod.Count { type=integer, min=1, max=100, default=2, page=ModPage, translation=Count, }",
        encoding="utf-8",
    )
    (base / "lua/shared/Translate/RU/Sandbox.json").write_text(
        json.dumps(
            {
                "Sandbox_Count_tooltip": "/AAAAFFStatic/FFFFFF: описание.<br><br><RGB:1,0,0>Вторая строка<LINE><script>window.executed=1</script>"
            }
        ),
        encoding="utf-8",
    )
    editor.workshop.invalidate()
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings", width <= 740)
    page.locator('#configTabs [data-tab="custom"]').click()
    field = page.locator('.config-field:has([data-key="Mod.Count"])')
    field.locator("[data-help]").click()
    hint = field.locator("[role=tooltip]")
    expect(hint).to_be_visible()
    expect(hint).to_contain_text("Static: описание.")
    expect(hint).to_contain_text("Вторая строка")
    assert "<br>" not in hint.inner_text() and "/AAAAFF" not in hint.inner_text()
    assert page.evaluate("window.executed") is None
    assert hint.evaluate("el => getComputedStyle(el).whiteSpace") == "pre-line"
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_sandbox_applicability_is_separate_accessible_and_keeps_fields_aligned(
    page, dashboard, editing, monkeypatch, width
):
    data, _ = editing
    path = data / "Server/world_SandboxVars.lua"
    path.write_text(
        "SandboxVars={VERSION=5, StartYear=1, StarterKit=true, CarSpawnRate=3, InitialGas=2, DayLength=4}",
        encoding="utf-8",
    )
    original = path.read_bytes()
    monkeypatch.setattr(
        editor.workshop,
        "vanilla_translations",
        lambda *args, **kwargs: {
            "Sandbox_StarterKit": "Стартовый набор",
            "Sandbox_StarterKit_tooltip": "Описание набора из установленной игры",
        },
    )
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings", width <= 740)
    page.get_by_role("tab", name="Мир", exact=True).click()
    for key, kind in (
        ("StartYear", "world"),
        ("StarterKit", "character"),
        ("CarSpawnRate", "areas"),
        ("InitialGas", "vehicles"),
    ):
        field = page.locator(f'.config-field:has([data-key="{key}"])')
        scope = field.locator(".config-scope")
        expect(scope).to_be_visible()
        expect(scope).to_have_attribute("data-scope", kind)
        described = field.locator("[data-key]").get_attribute("aria-describedby").split()
        assert scope.get_attribute("id") in described
        assert field.locator("[role=tooltip]").get_attribute("id") in described
        metrics = field.evaluate("""el => {
            const field = el.getBoundingClientRect(), scope = el.querySelector('.config-scope').getBoundingClientRect();
            const control = el.querySelector('.config-control').getBoundingClientRect();
            return {left:scope.left >= field.left,right:scope.right <= field.right,
                below:scope.top >= control.bottom};
        }""")
        assert all(metrics.values())
    starter = page.locator('.config-field:has([data-key="StarterKit"])')
    expect(starter.locator("[role=tooltip]")).to_contain_text(
        "Описание набора из установленной игры"
    )
    expect(starter.locator(".config-scope")).to_contain_text("Новые персонажи")
    assert page.locator('.config-field:has([data-key="DayLength"]) .config-scope').count() == 0
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    page.screenshot(path=str(data.parent / f"sandbox-applicability-{width}.png"), full_page=True)
    assert not editor.draft("world.ini")["changed"] and path.read_bytes() == original
    assert dashboard["actions"] == []


def test_short_phone_diff_keeps_confirmation_buttons_reachable(page, dashboard, editing):
    page.set_viewport_size({"width": 390, "height": 568})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings", True)
    page.locator('[data-key="PublicName"]').fill("Diff draft")
    page.locator('[data-key="PublicName"]').press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    page.locator("#configDiff").click()
    expect(page.get_by_role("alertdialog")).to_be_visible()
    page.evaluate(
        "document.querySelector('.modal-body').prepend(Object.assign(document.createElement('p'),{textContent:'Long diff '.repeat(800)}))"
    )
    dialog = page.get_by_role("alertdialog").bounding_box()
    button = page.locator("#modalCancel").bounding_box()
    assert dialog["y"] >= 0 and dialog["y"] + dialog["height"] <= 568
    assert button["y"] + button["height"] <= 568
    page.locator("#modalCancel").click()
    assert dashboard["actions"] == []


@pytest.mark.parametrize("width", [390, 1440])
def test_explicit_rebase_keeps_settings_and_fresh_reset_id_without_server_write(
    page, dashboard, editing, width
):
    data, _ = editing
    path = data / "Server/world.ini"
    original = INI + "ResetID=4742151\r\n"
    path.write_bytes(original.encode())
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings", width <= 740)
    page.locator('[data-key="PublicName"]').fill("Keep my draft")
    page.locator('[data-key="PublicName"]').press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    external = original.replace("ResetID=4742151", "ResetID=1701740")
    path.write_bytes(external.encode())
    page.reload()
    expect(page.locator("#configError")).to_contain_text("Рабочие файлы изменились")
    page.locator("#draftMore").click()
    page.locator("#configRebase").click()
    expect(page.get_by_role("alertdialog")).to_contain_text("Keep my draft")
    page.locator("#modalOk").click()
    expect(page.locator("#configError")).to_be_hidden()
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Keep my draft")
    assert not editor.draft("world.ini")["conflict"]
    assert "ResetID=1701740" in editor.draft("world.ini")["texts"]["ini"]
    assert path.read_bytes() == external.encode() and dashboard["actions"] == []
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    page.screenshot(path=str(data.parent / f"rebase-{width}.png"))


def test_rebase_overlap_keeps_draft_and_reports_manual_resolution(page, dashboard, editing):
    data, _ = editing
    path = data / "Server/world.ini"
    current = editor.draft("world.ini")
    editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "ini": {"PublicName": "Draft"},
        }
    )
    external = INI.replace("Сервер", "External")
    path.write_bytes(external.encode())
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configError")).to_contain_text("Рабочие файлы изменились")
    page.locator("#draftMore").click()
    page.locator("#configRebase").click()
    expect(page.locator("#configError")).to_contain_text("Обе версии изменяют строки")
    assert editor.draft("world.ini")["conflict"]
    assert "PublicName=Draft" in editor.draft("world.ini")["texts"]["ini"]
    assert path.read_bytes() == external.encode() and dashboard["actions"] == []


@pytest.mark.parametrize("width", [390, 1440])
def test_conflict_review_action_updates_draft_and_then_allows_apply_review(
    page, dashboard, editing, width
):
    data, _ = editing
    path = data / "Server/world.ini"
    original = INI + "ResetID=4742151\r\n"
    path.write_bytes(original.encode())
    current = editor.draft("world.ini")
    editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "ini": {"PublicName": "Keep draft"},
        }
    )
    external = original.replace("ResetID=4742151", "ResetID=1701740")
    path.write_bytes(external.encode())
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings", width <= 740)
    expect(page.locator("#configSaveHint")).to_have_count(0)
    expect(page.locator("#configApply")).to_be_disabled()
    page.locator("#configDiff").click()
    expect(page.get_by_role("alertdialog")).to_contain_text("Сейчас на диске")
    expect(page.locator("#modalOk")).to_have_text("Обновить основу черновика")
    page.locator("#modalOk").click()
    expect(page.locator("#modalRoot")).to_be_hidden()
    expect(page.locator("#configError")).to_be_hidden()
    assert editor.validate("world.ini")["valid"]
    assert "ResetID=1701740" in editor.draft("world.ini")["texts"]["ini"]
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Keep draft")
    page.locator("#configApply").click()
    expect(page.get_by_role("alertdialog")).to_contain_text("Применить конфигурацию?")
    page.locator("#modalCancel").click()
    assert path.read_bytes() == external.encode() and dashboard["actions"] == []
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")


def test_diff_without_conflict_has_close_action(page, dashboard, editing):
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    page.locator('[data-key="PublicName"]').fill("New name")
    page.locator('[data-key="PublicName"]').press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    page.locator("#configDiff").click()
    expect(page.locator("#modalOk")).to_have_text("Закрыть")
    page.locator("#modalOk").click()
    expect(page.locator("#modalRoot")).to_be_hidden()
    assert editor.draft("world.ini")["changed"] and dashboard["actions"] == []


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("entry", ["save", "apply", "prepare"])
def test_apply_review_can_skip_world_backup(page, dashboard, editing, width, entry):
    data, _ = editing
    original = (data / "Server/world.ini").read_bytes()
    current = editor.draft("world.ini")
    editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "mods": {"items": ["111", "222"]} if entry == "prepare" else {"selected": ["library"]},
        }
    )
    page.set_viewport_size({"width": width, "height": 568})
    page.goto(dashboard["url"] + "/#/mods")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    if entry == "save":
        page.evaluate(
            "renderOverview({...S.overview,containerInfo:{running:false,status:'exited'}})"
        )
        page.locator("#draftMore").click()
        page.locator("#configSave").click()
    elif entry == "apply":
        page.locator("#configApply").click()
    else:
        page.locator("#prepareWorkshop").click()
    expect(page.locator("#editorBackup")).to_be_checked()
    page.locator("#editorBackup").uncheck()
    expect(page.locator("#editorBackupHint")).to_contain_text("История конфигурации")
    expect(page.get_by_role("alertdialog")).not_to_contain_text("обязателен бэкап")
    dialog = page.get_by_role("alertdialog").bounding_box()
    confirm = page.locator("#modalOk").bounding_box()
    assert dialog["y"] >= 0 and confirm["y"] + confirm["height"] <= 568
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    page.screenshot(path=str(data.parent / f"apply-options-{entry}-{width}.png"))
    page.locator("#modalOk").click()
    expect(page.locator(".toast").last).to_contain_text("сервер занят")
    assert len(dashboard["actions"]) == 1
    request = dashboard["actions"][0]
    assert request["backupBeforeApply"] is False
    assert request["restart"] is (entry != "save")
    assert request["op"] == ("prepare-workshop" if entry == "prepare" else "apply-config")
    assert (data / "Server/world.ini").read_bytes() == original


def test_regular_settings_review_offers_optional_world_backup(page, dashboard, editing):
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    page.locator('[data-key="PublicName"]').fill("Optional backup")
    page.locator("#configApply").click()
    expect(page.locator("#editorBackup")).not_to_be_checked()
    page.locator("#editorBackup").check()
    page.locator("#modalOk").click()
    expect(page.locator(".toast").last).to_contain_text("сервер занят")
    assert dashboard["actions"][0]["backupBeforeApply"] is True


@pytest.mark.parametrize("width", [320, 390, 768, 1440, 2048])
def test_pages_share_edges_and_form_controls_share_size(page, dashboard, editing, width):
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    edges = []
    for route in (
        "overview",
        "players",
        "mods",
        "settings",
        "maintenance",
        "backups",
        "events",
        "console",
    ):
        navigate(page, route, width <= 740)
        metrics = page.evaluate("""() => {
            const card = document.querySelector('.view:not([hidden]) .card').getBoundingClientRect();
            const view = document.querySelector('.view:not([hidden])').getBoundingClientRect();
            const fields = [...document.querySelectorAll('.view:not([hidden]) input:not([type=checkbox]):not([type=file]), .view:not([hidden]) select')]
                .filter(el => el.getBoundingClientRect().width && !el.closest('[hidden]'));
            return {left:card.left,right:view.right,heights:fields.map(el => el.getBoundingClientRect().height),
                overflow:document.documentElement.scrollWidth > innerWidth};
        }""")
        assert not metrics["overflow"], route
        assert all(abs(h - 44) < 1 for h in metrics["heights"]), (route, metrics["heights"])
        edges.append((metrics["left"], metrics["right"]))
        if route == "console" and width > 1180:
            panels = page.evaluate("""() => {
                const box = selector => document.querySelector(selector).getBoundingClientRect();
                const consolePanel = box('#consoleOut'), logsPanel = box('#logsOut');
                return {consoleTop:consolePanel.top, logsTop:logsPanel.top,
                    consoleWidth:consolePanel.width, logsWidth:logsPanel.width};
            }""")
            assert abs(panels["consoleTop"] - panels["logsTop"]) < 1
            assert abs(panels["consoleWidth"] - panels["logsWidth"]) < 1
        if width in (390, 1440):
            page.screenshot(path=str(editing[0].parent / f"unified-{route}-{width}.png"))
    assert all(
        abs(left - edges[0][0]) < 1 and abs(right - edges[0][1]) < 1 for left, right in edges
    )
    navigate(page, "settings", width <= 740)
    page.locator('[data-key="PublicName"]').fill("Aligned draft")
    page.locator('[data-key="PublicName"]').press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    if width > 740:
        bar = page.locator("#draftBar").bounding_box()
        assert abs(bar["x"] - edges[0][0]) < 1
        assert abs(bar["x"] + bar["width"] - edges[0][1]) < 1
    page.screenshot(path=str(editing[0].parent / f"unified-ui-{width}.png"))


def test_more_menu_escape_restores_focus_and_marks_extra_page(page, dashboard, editing):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "players", True)
    expect(page.locator("#navMore")).to_have_attribute("aria-current", "page")
    page.locator("#navMore").click()
    page.locator('#moreMenu a[href="#/players"]').focus()
    page.keyboard.press("Escape")
    expect(page.locator("#moreMenu")).to_be_hidden()
    expect(page.locator("#navMore")).to_be_focused()


@pytest.mark.parametrize("width,height", [(320, 844), (390, 844), (390, 500)])
def test_scrolling_draft_does_not_block_phone_navigation(page, dashboard, editing, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator('[data-key="PublicName"]')).to_be_enabled()
    page.locator('[data-key="PublicName"]').fill("Navigation draft")
    page.locator('[data-key="PublicName"]').press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    navigate(page, "mods")
    # Put the scrolling draft actions beneath the fixed navigation's hit target.
    page.evaluate("""() => {
        const bar = document.querySelector('#draftBar').getBoundingClientRect();
        const link = document.querySelector('.nav [data-route=settings]').getBoundingClientRect();
        window.scrollBy(0, bar.top + bar.height / 2 - link.top - link.height / 2);
    }""")
    assert page.evaluate("""() => {
        const bar = document.querySelector('#draftBar').getBoundingClientRect();
        const link = document.querySelector('.nav [data-route=settings]').getBoundingClientRect();
        const x = link.left + link.width / 2, y = link.top + link.height / 2;
        const fixed = getComputedStyle(document.querySelector('#draftBar')).position === 'fixed';
        return (fixed ? bar.bottom <= link.top : bar.top < y && bar.bottom > y)
            && !!document.elementFromPoint(x, y)?.closest('.nav [data-route=settings]');
    }""")
    page.screenshot(path=str(editing[0].parent / f"draft-navigation-{width}-{height}.png"))
    navigate(page, "settings", True)
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Navigation draft")


def test_phone_draft_secondary_actions_stay_available(page, dashboard, editing):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings", True)
    page.locator('[data-key="PublicName"]').fill("Compact draft")
    page.locator('[data-key="PublicName"]').press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    expect(page.locator("#editorLogs")).to_be_hidden()
    page.locator("#draftMore").click()
    expect(page.locator("#editorLogs")).to_be_visible()
    expect(page.locator("#configDiscard")).to_be_visible()
    page.locator("#configDiscard").click()
    expect(page.get_by_role("alertdialog")).to_contain_text("Отменить черновик")
    page.locator("#modalCancel").click()
    page.locator("#draftMore").click()
    page.locator("#editorLogs").focus()
    page.keyboard.press("Escape")
    expect(page.locator("#editorLogs")).to_be_hidden()
    expect(page.locator("#draftMore")).to_be_focused()


@pytest.mark.parametrize("width", [390, 1440])
def test_control_text_fits_and_settings_has_navigation_icon(page, dashboard, editing, width):
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    settings = page.locator('.nav [data-route="settings"]')
    expect(settings).to_have_text("Настройки")
    expect(settings.locator("svg")).to_have_count(1)
    for route in ("settings", "mods", "maintenance", "backups", "console"):
        navigate(page, route, width <= 740)
        clipped = page.evaluate("""() => [...document.querySelectorAll('input:not([type=checkbox]):not([type=file]), select, button.btn, label.btn')].filter(el => {
            const r = el.getBoundingClientRect();
            if (!r.width || !r.height) return false;
            const s = getComputedStyle(el);
            if (el.matches('input,select')) return el.clientHeight - parseFloat(s.paddingTop) - parseFloat(s.paddingBottom) + 1 < parseFloat(s.lineHeight);
            const range = document.createRange(); range.selectNodeContents(el);
            const text = range.getBoundingClientRect();
            return text.height > 0 && (text.top < r.top || text.bottom > r.bottom);
        }).map(el => el.id || el.textContent.trim())""")
        assert not clipped, f"Text is vertically clipped on {route}: {clipped}"
    assert dashboard["actions"] == []


@pytest.mark.parametrize("width", [320, 390, 768, 1440])
def test_header_profile_context_is_clear_and_aligned(page, dashboard, editing, width):
    data, _ = editing
    other = "Life Before - альтернативный профиль.ini"
    (data / "Server" / other).write_bytes(INI.encode())
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    expect(page.locator("#configActive")).to_have_text("Активен на сервере")
    expect(page.locator("#configProfile")).to_have_attribute("data-state", "active")
    expect(page.locator("#configProfile")).to_have_accessible_description("Активен на сервере")
    active_color = page.locator("#configProfile").evaluate("el => getComputedStyle(el).color")
    expect(page.locator("#configProfile")).to_have_css("border-top-color", active_color)
    expect(page.locator("#configVersion")).to_have_text("PZ 42.15.1")
    expect(page.locator("#configProfile option:checked")).to_have_text("world.ini")
    expect(page.locator("#clock, #topLamp")).to_have_count(0)
    expect(page.locator("#serverName")).to_have_count(0)
    expect(page.locator("#freshness, .header-meta")).to_have_count(0)
    expect(page.locator("#configActive")).to_be_hidden()
    page.locator("#configProfile").select_option(other)
    expect(page.locator("#configActive")).to_have_text("Другой профиль")
    expect(page.locator("#configProfile")).to_have_attribute("data-state", "other")
    expect(page.locator("#configProfile")).not_to_have_attribute("title")
    expect(page.locator("#configProfile")).to_have_accessible_description("Другой профиль")
    expect(page.locator("#configActive")).to_be_hidden()
    expect(page.locator('[data-help="configProfileHelp"], #configProfileHelp')).to_have_count(0)
    other_color = page.locator("#configProfile").evaluate("el => getComputedStyle(el).color")
    assert active_color != other_color
    expect(page.locator("#configProfile")).to_have_css("border-top-color", other_color)
    expect(page.locator("#configApply")).to_be_disabled()
    geometry = page.evaluate("""() => {
        const box = s => document.querySelector(s).getBoundingClientRect();
        const select = box('#configProfile'), commands = box('#btnCommands'), header = box('.topbar');
        const logout = box('#btnLogout');
        return {
            selectTop:select.top,selectBottom:select.bottom,selectHeight:select.height,
            commandsTop:commands.top,commandsHeight:commands.height,
            logoutTop:logout.top,logoutHeight:logout.height,
            height:header.height,overflow:document.documentElement.scrollWidth > innerWidth};
    }""")
    assert geometry["selectHeight"] == geometry["commandsHeight"] == 44
    assert geometry["selectTop"] == geometry["commandsTop"]
    assert not geometry["overflow"]
    if width <= 740:
        assert geometry["height"] < 150
    else:
        assert geometry["selectTop"] == geometry["logoutTop"]
        assert geometry["logoutHeight"] == 44
        assert geometry["height"] <= 110
    expect(page.locator(".health-details, #healthbar, #pillDocker, #pillRcon")).to_have_count(0)
    expect(page.locator(".brand-text #configVersion")).to_be_visible()
    expect(page.locator("#configVersion")).to_have_attribute("title", "PZ 42.15.1")
    expect(page.locator(".topbar #btnLogout")).to_be_visible()
    brand = page.locator(".brand-text")
    original_width = brand.bounding_box()["width"]
    original_header_height = page.locator(".topbar").bounding_box()["height"]
    name_box = page.locator(".brand-name").bounding_box()
    version_box = page.locator("#configVersion").bounding_box()
    assert version_box["y"] >= name_box["y"] + name_box["height"]
    for version in ["B42 · версия неизвестна", "PZ 42.15.1 — очень длинная версия сервера"]:
        page.locator("#configVersion").evaluate("(el, text) => el.textContent = text", version)
        assert brand.bounding_box()["width"] == original_width
        assert page.locator(".topbar").bounding_box()["height"] == original_header_height
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#configVersion").evaluate("el => el.textContent = 'PZ 42.15.1'")
    page.locator("#configProfile").select_option("world.ini")
    expect(page.locator("#configActive")).to_have_text("Активен на сервере")
    expect(page.locator("#configProfile")).to_have_attribute("data-state", "active")
    page.locator(".topbar").screenshot(path=str(data.parent / f"review-header-{width}.png"))
    page.screenshot(path=str(data.parent / f"review-page-{width}.png"))
    assert dashboard["actions"] == []


def test_header_does_not_claim_unknown_profile_is_active(page, dashboard, editing):
    _, context = editing
    context["activeFile"] = None
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    expect(page.locator("#configActive")).to_have_text("Не подтверждён")
    expect(page.locator("#configProfile")).to_have_attribute("data-state", "unknown")
    expect(page.locator("#configProfile")).to_have_accessible_description("Не подтверждён")
    expect(page.locator("#configActive")).to_be_hidden()
    expect(page.locator("#configProfile")).not_to_have_attribute("title")
    page.evaluate("location.hash='#/settings'")
    expect(page.locator("#configActionHelp")).to_contain_text("Профиль запуска не подтверждён")
    expect(page.locator(".context-help, #configSaveHint")).to_have_count(0)
    expect(page.locator("#configApply")).to_be_disabled()


@pytest.mark.parametrize("width", [1440, 390])
def test_real_form_draft_mod_selection_and_sources(page, dashboard, editing, width):
    data, _ = editing
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings", width == 390)
    name = page.locator('[data-key="PublicName"]')
    expect(name).to_have_value("Сервер")
    name.fill("UI draft")
    name.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    assert (data / "Server/world.ini").read_bytes() == INI.encode()
    navigate(page, "mods")
    packet = page.locator("#modPackages details")
    packet.locator("summary").click()
    expect(page.locator('[data-modid="library"]')).to_be_checked()
    page.locator('[data-modid="plugin"]').uncheck()
    expect(page.locator("#modOrderList [data-order-id]")).to_have_count(1)
    expect(page.locator("#modPackages details")).to_have_count(1)
    navigate(page, "settings", width == 390)
    expect(page.locator('[data-key="PublicName"]')).to_have_value("UI draft")
    page.get_by_role("tab", name="Исходники", exact=True).click()
    expect(page.locator("#iniSource")).to_contain_text("")
    assert "UI draft" in page.locator("#iniSource").input_value()
    assert "topsecret" not in page.locator("#iniSource").input_value()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(data.parent / f"editor-{width}.png"), full_page=True)


def test_background_refresh_preserves_profile_focus_and_draft(page, dashboard, editing):
    data, _ = editing
    (data / "Server/another.ini").write_bytes(INI.encode())
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    page.locator("#configProfile").select_option("another.ini")
    expect(page.locator("#configActive")).to_have_text("Другой профиль")
    navigate(page, "settings")
    field = page.locator('[data-key="PublicName"]')
    field.fill("Other draft")
    field.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    field.focus()
    page.evaluate(
        "renderMods({ok:true,file:'world.ini',files:['world.ini','another.ini'],mods:[],workshop:[]})"
    )
    expect(field).to_be_focused()
    expect(field).to_have_value("Other draft")
    expect(page.locator("#configProfile")).to_have_value("another.ini")
    expect(page.locator("#configApply")).to_be_disabled()


def test_conflict_and_invalid_field_are_visible(page, dashboard, editing):
    data, _ = editing
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    (data / "Server/world.ini").write_bytes(INI.replace("Сервер", "External").encode())
    page.locator('[data-key="PublicName"]').fill("Should not overwrite")
    page.locator('[data-key="PublicName"]').press("Tab")
    expect(page.locator("#configError")).to_contain_text("изменились")
    assert "External" in (data / "Server/world.ini").read_text(encoding="utf-8")
    page.locator("#configDiff").click()
    expect(page.get_by_role("alertdialog")).to_contain_text("Сейчас на диске")
    expect(page.get_by_role("alertdialog")).not_to_contain_text("topsecret")


def test_export_pack_download(page, dashboard, editing):
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    with page.expect_download() as received:
        page.locator("#modExport").click()
    download = received.value
    pack = json.loads(Path(download.path()).read_text(encoding="utf-8"))
    assert pack["items"] == ["111"] and pack["selected"] == ["library", "plugin"]
    assert "hidden-token" not in json.dumps(pack)


@pytest.mark.parametrize("width", [1440, 390])
def test_keyboard_tabs_and_mod_order(page, dashboard, editing, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    tab = page.get_by_role("tab", name="Состав", exact=True)
    tab.focus()
    tab.press("ArrowRight")
    expect(page.get_by_role("tab", name="Порядок", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    page.get_by_role("button", name="Опустить library", exact=True).click()
    expect(page.locator("#modOrder .order-row").first.locator("code")).to_have_text("plugin")
    page.get_by_role("button", name="Поднять library", exact=True).click()
    expect(page.locator("#modOrder .order-row").first.locator("code")).to_have_text("library")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.fixture
def long_order(editing):
    data, _ = editing
    ids = ["library", "plugin"] + [f"mod{i:03d}" for i in range(1, 209)]
    base = data / "steamapps/workshop/content/108600/111/mods"
    for mid in ids[2:]:
        path = base / f"Folder_{mid}" / "42"
        path.mkdir(parents=True)
        (path / "mod.info").write_text(f"id={mid}\nname=Название {mid}\n", encoding="utf-8")
    original = INI.replace("Mods=\\library;\\plugin", "Mods=" + ";".join("\\" + mid for mid in ids))
    (data / "Server/world.ini").write_bytes(original.encode())
    editor.workshop.invalidate()
    return data, ids, original


@pytest.mark.parametrize("width", [320, 390, 768, 1024, 1440])
def test_long_order_moves_to_edges_and_position_without_server_write(
    page, dashboard, long_order, width
):
    data, ids, original = long_order
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.get_by_role("tab", name="Порядок", exact=True).click()
    expect(page.locator("#modOrderList .order-row")).to_have_count(210)
    query = page.locator("#orderQuery")
    query.fill("Название mod199")
    row = page.locator('#modOrderList [data-order-id="mod199"]')
    expect(row.locator(".order-position")).to_have_text("201")
    expect(page.locator("#orderCount")).to_contain_text("Найдено 1 из 210")
    row.locator("[data-position-id]").click()
    expect(page.locator("#orderDestination")).to_be_focused()
    page.get_by_role("button", name="В начало", exact=True).click()
    expect(page.get_by_role("alertdialog")).to_be_hidden()
    expect(row.locator(".order-position")).to_have_text("1")
    expect(query).to_have_value("Название mod199")
    expect(row.locator("[data-position-id]")).to_be_focused()
    assert editor.mod_response("world.ini", draft_mode=True)["mods"] == [
        "mod199",
        *[mid for mid in ids if mid != "mod199"],
    ]
    row.locator("[data-position-id]").click()
    page.get_by_role("button", name="В конец", exact=True).click()
    expect(row.locator(".order-position")).to_have_text("210")
    row.locator("[data-position-id]").click()
    page.locator("#orderDestination").fill("105")
    page.locator("#orderDestination").press("Enter")
    expect(page.get_by_role("alertdialog")).to_be_hidden()
    expect(row.locator(".order-position")).to_have_text("105")
    expected = [mid for mid in ids if mid != "mod199"]
    expected.insert(104, "mod199")
    result = editor.mod_response("world.ini", draft_mode=True)
    assert result["mods"] == expected and result["maps"] == ["Muldraugh, KY"]
    assert [w["workshopId"] for w in result["workshop"]] == ["111"]
    assert (data / "Server/world.ini").read_bytes() == original.encode()
    assert dashboard["actions"] == []
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    query.fill("")
    page.locator("#modOrderList .order-row").first.scroll_into_view_if_needed()
    page.screenshot(path=str(data.parent / f"review-mod-order-{width}.png"), full_page=False)
    page.get_by_role("button", name="Переместить library", exact=True).click()
    page.screenshot(path=str(data.parent / f"review-mod-position-{width}.png"), full_page=False)
    buttons = page.locator(".order-actions .btn").first
    assert buttons.evaluate("el => el.getBoundingClientRect().height >= 44")


def test_order_position_invalid_cancel_and_single_item(page, dashboard, editing):
    data, _ = editing
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.get_by_role("tab", name="Порядок", exact=True).click()
    page.get_by_role("button", name="Переместить library", exact=True).click()
    revision = editor.draft("world.ini")["draftRevision"]
    for invalid in ("0", "3", "1.5", ""):
        page.locator("#orderDestination").fill(invalid)
        page.locator("#modalOk").click()
        expect(page.locator("#orderPositionError")).to_have_text("Укажите целую позицию от 1 до 2")
        expect(page.locator("#orderDestination")).to_have_attribute("aria-invalid", "true")
        assert editor.draft("world.ini")["draftRevision"] == revision
    page.locator("#orderDestination").press("Escape")
    expect(page.get_by_role("button", name="Переместить library", exact=True)).to_be_focused()
    assert editor.draft("world.ini")["draftRevision"] == revision
    # Editing after cancellation remains usable; single-item boundaries are disabled.
    page.get_by_role("tab", name="Состав", exact=True).click()
    page.locator("#modPackages summary").click()
    page.locator('[data-modid="plugin"]').uncheck()
    expect(page.locator("#modOrderList .order-row")).to_have_count(1)
    page.get_by_role("tab", name="Порядок", exact=True).click()
    expect(page.get_by_role("button", name="Поднять library", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Опустить library", exact=True)).to_be_disabled()
    page.get_by_role("button", name="Переместить library", exact=True).click()
    expect(page.get_by_role("button", name="В начало", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="В конец", exact=True)).to_be_disabled()
    assert (data / "Server/world.ini").read_bytes() == INI.encode()


def test_queued_order_steps_use_current_order(page, dashboard, long_order):
    _, ids, _ = long_order
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.get_by_role("tab", name="Порядок", exact=True).click()
    page.get_by_role("button", name="Опустить library", exact=True).evaluate(
        "button => { button.click(); button.click(); }"
    )
    row = page.locator('[data-order-id="library"]')
    expect(row.locator(".order-position")).to_have_text("3")
    assert editor.mod_response("world.ini", draft_mode=True)["mods"] == [
        "plugin",
        "mod001",
        "library",
        *ids[3:],
    ]


def test_drag_handle_inserts_before_and_after_without_off_by_one(page, dashboard, long_order):
    _, ids, _ = long_order
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.get_by_role("tab", name="Порядок", exact=True).click()
    row = page.locator('[data-order-id="mod001"]')
    # Settle the target's scroll margins before holding the source handle.
    row.scroll_into_view_if_needed()
    height = row.bounding_box()["height"]
    page.get_by_role("button", name="Перетащить library", exact=True).drag_to(
        row, target_position={"x": 160, "y": height - 4}
    )
    expect(page.locator('[data-order-id="library"] .order-position')).to_have_text("3")
    assert editor.mod_response("world.ini", draft_mode=True)["mods"] == [
        "plugin",
        "mod001",
        "library",
        *ids[3:],
    ]
    page.get_by_role("button", name="Перетащить library", exact=True).drag_to(
        page.locator('[data-order-id="plugin"]'), target_position={"x": 160, "y": 4}
    )
    expect(page.locator('[data-order-id="library"] .order-position')).to_have_text("1")
    assert editor.mod_response("world.ini", draft_mode=True)["mods"] == ids
    expect(page.locator("[data-drop], .order-row.dragging")).to_have_count(0)


@pytest.mark.parametrize("width", [390, 1440])
def test_navigation_waits_for_delayed_route(page, dashboard, editing, width):
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    # Make the gap between a link click and the route update deterministic.
    page.evaluate("""() => {
        window.removeEventListener('hashchange', applyRoute);
        window.addEventListener('hashchange', () => setTimeout(applyRoute, 250));
    }""")
    for route in ("settings", "mods", "maintenance", "console"):
        navigate(page, route, width <= 740)
        assert page.locator(f"#view-{route}").is_visible(), route
        assert page.locator(".view:visible").count() == 1


@pytest.mark.parametrize("width", [390, 1440])
def test_select_chevron_inset_and_text_space(page, dashboard, editing, width):
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    for route in ("settings", "mods", "maintenance", "console"):
        navigate(page, route, width <= 740)
        styles = page.locator("select:visible").evaluate_all("""els => els.map(el => {
            const s=getComputedStyle(el); return {id:el.id,appearance:s.appearance,padding:parseFloat(s.paddingRight),
                position:s.backgroundPosition,image:s.backgroundImage,height:el.clientHeight,
                textHeight:parseFloat(s.lineHeight)+parseFloat(s.paddingTop)+parseFloat(s.paddingBottom)};
        })""")
        assert styles, route
        for style in styles:
            assert style["appearance"] == "none" and style["padding"] >= 42, (route, style)
            assert "14px" in style["position"] and "data:image/svg+xml" in style["image"], (
                route,
                style,
            )
            assert style["height"] + 1 >= style["textHeight"], (route, style)
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")


def test_late_background_response_cannot_mark_own_change_as_conflict(page, dashboard, editing):
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    old = editor.draft("world.ini")
    pending = []

    def hold(route):
        if route.request.method == "GET":
            pending.append(route)
        else:
            route.fallback()

    page.route("**/api/config-draft?file=world.ini", hold)
    with page.expect_request("**/api/config-draft?file=world.ini"):
        page.evaluate("ConfigEditor.background()")
    page.wait_for_timeout(100)  # Request events precede delivery to the route handler.
    assert pending
    page.locator('[data-key="PublicName"]').fill("Own fresh revision")
    page.locator('[data-key="PublicName"]').press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    for route in pending:
        route.fulfill(json=old)
    page.wait_for_timeout(100)  # Let the stale response's promise callback run.
    expect(page.locator("#configError")).to_be_hidden()
    expect(page.locator("#configRebase")).to_be_hidden()
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Own fresh revision")


def test_steam_response_after_profile_switch_does_not_change_other_profile(
    page, dashboard, editing
):
    data, _ = editing
    (data / "Server/another.ini").write_bytes(INI.encode())
    pending = []
    page.route("**/api/workshop-resolve", lambda route: pending.append(route))
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    expect(page.locator("#workshopInput")).to_be_visible()
    page.locator("#workshopInput").fill("222")
    page.locator("#workshopAdd button").click()
    page.locator("#configProfile").select_option("another.ini")
    expect(page.locator("#configActive")).to_have_text("Другой профиль")
    expect(page.locator("#draftSaved")).not_to_have_text("Сохраняется черновик…")
    page.wait_for_function("ConfigEditor.file === 'another.ini'")
    assert pending
    pending[0].fulfill(json={"ok": True, "items": [{"workshopId": "222"}]})
    expect(page.locator("#configError")).to_contain_text(
        "Профиль изменился во время проверки Steam"
    )
    assert not editor.draft("world.ini")["changed"]
    assert not editor.draft("another.ini")["changed"]
    assert dashboard["actions"] == []


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_collection_candidates_filter_and_add_only_selected_packages(
    page, dashboard, editing, width
):
    data, _ = editing
    original_lua = (data / "Server/world_SandboxVars.lua").read_bytes()
    original_draft_lua = editor.draft("world.ini")["texts"]["sandbox"]
    items = [
        {"workshopId": "111", "title": "Already installed"},
        {"workshopId": "222", "title": "Choose this package"},
        {"workshopId": "333", "title": "<script>window.executed=1</script> Another package"},
    ]
    page.route(
        "**/api/workshop-resolve",
        lambda route: route.fulfill(
            json={
                "ok": True,
                "source": {"kind": "collection", "title": "Collection candidates"},
                "items": items,
            }
        ),
    )
    page.set_viewport_size({"width": width, "height": 568})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods", width <= 740)
    expect(page.locator("#workshopInput")).to_be_visible()
    page.locator("#workshopInput").fill("999")
    page.locator("#workshopAdd button").click()
    expect(page.locator("#collectionQuery")).to_be_focused()
    expect(page.locator('#collectionCandidates input[value="111"]')).to_be_disabled()
    expect(page.locator("#modalOk")).to_be_disabled()
    assert not editor.draft("world.ini")["changed"]
    page.locator("#collectionQuery").fill("222")
    page.locator("#collectionSelect").click()
    expect(page.locator("#collectionCount")).to_contain_text("Выбрано пакетов: 1")
    page.locator("#collectionQuery").fill("not-found")
    expect(page.locator("#collectionEmpty")).to_be_visible()
    # Hidden selections persist and invisible controls are excluded from the focus trap.
    page.locator("#modalOk").focus()
    page.keyboard.press("Tab")
    expect(page.locator("#collectionQuery")).to_be_focused()
    page.locator("#collectionQuery").fill("333")
    page.locator("#collectionSelect").click()
    expect(page.locator("#collectionCount")).to_contain_text("Выбрано пакетов: 2")
    page.locator("#collectionClear").click()
    expect(page.locator("#collectionCount")).to_contain_text("Выбрано пакетов: 1")
    assert page.evaluate("window.executed") is None
    dialog = page.get_by_role("alertdialog").bounding_box()
    button = page.locator("#modalOk").bounding_box()
    assert dialog["y"] >= 0 and dialog["y"] + dialog["height"] <= 568
    assert button["y"] + button["height"] <= 568
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    page.locator("#modalOk").click()
    expect(page.locator("#modalRoot")).to_be_hidden()
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    saved = editor.draft("world.ini")
    assert "WorkshopItems=111;222\r\n" in saved["texts"]["ini"]
    assert "Mods=\\library;\\plugin\r\n" in saved["texts"]["ini"]
    assert saved["texts"]["sandbox"] == original_draft_lua
    assert (data / "Server/world_SandboxVars.lua").read_bytes() == original_lua
    assert (data / "Server/world.ini").read_bytes() == INI.encode()
    assert dashboard["actions"] == []


@pytest.mark.parametrize("count", [0, 1, 210])
def test_collection_cancel_keeps_draft_and_next_modal_usable(page, dashboard, editing, count):
    page.route(
        "**/api/workshop-resolve",
        lambda route: route.fulfill(
            json={
                "ok": True,
                "source": {"kind": "collection", "title": "Collection"},
                "items": [
                    {"workshopId": str(1000 + i), "title": "Длинное название пакета " * 8}
                    for i in range(count)
                ],
            }
        ),
    )
    page.set_viewport_size({"width": 390, "height": 568})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods", True)
    expect(page.locator("#workshopInput")).to_be_visible()
    page.locator("#workshopInput").fill("999")
    page.locator("#workshopAdd button").click()
    expect(page.locator("#collectionQuery")).to_be_visible()
    expect(page.locator("#modalOk")).to_be_disabled()
    if count:
        page.locator("#collectionQuery").fill(str(999 + count))
        expect(page.locator(".collection-candidate:visible")).to_have_count(1)
        assert page.locator("#modalCancel").bounding_box()["y"] < 568
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    page.screenshot(path=str(editing[0].parent / f"collection-candidates-{count}.png"))
    page.keyboard.press("Escape")
    expect(page.locator("#modalRoot")).to_be_hidden()
    assert not editor.draft("world.ini")["changed"]
    # Collection's disabled confirmation must not leak into another modal.
    page.locator('.workshop-package[data-item="111"] summary').click()
    page.locator('[data-remove-item="111"]').click()
    expect(page.locator("#modalOk")).to_be_enabled()
    page.locator("#modalCancel").click()
    assert not editor.draft("world.ini")["changed"]
    assert dashboard["actions"] == []


@pytest.mark.parametrize("width", [390, 1440])
def test_verify_external_restart_clears_install_notice_and_old_error_without_action(
    page, dashboard, editing, monkeypatch, width
):
    data, _ = saved_verification(editing, monkeypatch, installed=True)
    original = (data / "Server/world.ini").read_bytes()
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    expect(page.locator("#installNotice")).to_contain_text("Загрузка требует проверки")
    expect(page.locator('[data-flow="launch"]')).to_have_attribute("data-state", "bad")
    expect(page.locator('[data-flow="launch"] .flow-error')).to_be_visible()
    page.locator("#installNotice [data-verify-running]").click()
    expect(page.locator("#installNotice")).to_be_hidden()
    expect(page.locator("#configOperationResult")).to_have_count(0)
    expect(page.locator("#flowLaunch")).to_have_text("Подтверждён")
    expect(page.locator('#configFlow [data-state="ok"]')).to_have_count(3)
    navigate(page, "settings", width <= 740)
    expect(page.locator("#configStatus")).to_have_text("Применено")
    expect(page.locator("#configError")).to_be_hidden()
    expect(page.locator("#configOperationResult")).to_have_count(0)
    assert (data / "Server/world.ini").read_bytes() == original
    assert dashboard["actions"] == []
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")


def test_startup_status_does_not_claim_previous_verification_after_another_restart(
    page, dashboard, editing, monkeypatch
):
    _, request = saved_verification(editing, monkeypatch)
    editor.verify_running(request)
    from test_configeditor import ops

    monkeypatch.setattr(
        ops, "container_state", lambda: {"running": True, "startedAt": "another-start"}
    )
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    expect(page.locator("#configStatus")).to_have_text("Применение не подтверждено")
    expect(page.locator("#flowLaunch")).to_have_text("Нужна проверка")
    expect(page.locator("#configVerifyHelp")).to_be_visible()
    expect(page.locator("#configOperationResult")).to_have_count(0)


@pytest.mark.parametrize("width", [390, 1440])
def test_auto_verification_updates_open_settings_and_mods_without_click(
    page, dashboard, editing, monkeypatch, width
):
    data, _ = saved_verification(editing, monkeypatch, installed=True)
    original = editor.read_profile("world.ini")
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    expect(page.locator("#installNotice")).to_contain_text("Загрузка требует проверки")
    editor.auto_verify_running()
    # Move the refresh throttle forward without waiting for the real timer.
    page.evaluate(
        "const now = Date.now; Date.now = () => now() + 16000; ConfigEditor.background();"
    )
    expect(page.locator("#installNotice")).to_be_hidden()
    expect(page.locator("#flowLaunch")).to_have_text("Подтверждён")
    expect(page.locator("#configOperationResult")).to_have_count(0)
    navigate(page, "settings", width <= 740)
    expect(page.locator("#configStatus")).to_have_text("Применено")
    expect(page.locator("#configError")).to_be_hidden()
    expect(page.locator("#configRebase")).to_be_hidden()
    expect(page.locator("#configOperationResult")).to_have_count(0)
    assert editor.read_profile("world.ini") == original
    assert (data / "Server/world.ini").exists()
    assert dashboard["actions"] == []
    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
    evidence = Path(__file__).resolve().parents[2] / ".tmp-auto-verify-browser"
    evidence.mkdir(exist_ok=True)
    page.screenshot(path=str(evidence / f"settings-{width}.png"), full_page=True)


def test_auto_verification_refresh_keeps_saved_draft_mod_selection(
    page, dashboard, editing, monkeypatch
):
    saved_verification(editing, monkeypatch)
    current = editor.draft("world.ini")
    editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "mods": {"selected": ["library"]},
        }
    )
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    expect(page.locator("#modOrderList [data-order-id]")).to_have_count(1)
    editor.auto_verify_running()
    with page.expect_response("**/api/config-draft?file=world.ini"):
        page.evaluate(
            "const now = Date.now; Date.now = () => now() + 16000; ConfigEditor.background();"
        )
    expect(page.locator("#configOperationResult")).to_have_count(0)
    expect(page.locator("#modOrderList [data-order-id]")).to_have_count(1)
    page.locator("#modPackages summary").click()
    expect(page.locator('[data-modid="plugin"]')).not_to_be_checked()
    expect(page.locator("#configRebase")).to_be_hidden()
    assert editor.draft("world.ini")["changed"]


def test_auto_verification_background_does_not_overwrite_unsaved_source(
    page, dashboard, editing, monkeypatch
):
    saved_verification(editing, monkeypatch)
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    page.get_by_role("tab", name="Исходники", exact=True).click()
    source = page.locator("#iniSource")
    pending = source.input_value().replace("PublicName=Сервер", "PublicName=Local unsaved edit")
    source.fill(pending)
    editor.auto_verify_running()
    page.evaluate(
        "const now = Date.now; Date.now = () => now() + 16000; ConfigEditor.background();"
    )
    expect(source).to_have_value(pending)
    assert "Local unsaved edit" not in editor.read_profile("world.ini")["ini"]


def test_auto_verification_marker_cannot_accept_later_edit_from_another_tab(
    page, dashboard, editing, monkeypatch
):
    saved_verification(editing, monkeypatch)
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    editor.auto_verify_running()
    verified = editor.draft("world.ini")
    editor.patch(
        {
            "file": "world.ini",
            "draftRevision": verified["draftRevision"],
            "ini": {"PublicName": "Another tab"},
        }
    )
    page.evaluate(
        "const now = Date.now; Date.now = () => now() + 16000; ConfigEditor.background();"
    )
    expect(page.locator("#draftSaved")).to_contain_text("Черновик изменён другой вкладкой")
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Сервер")


@pytest.mark.parametrize("width", [1440, 390])
def test_discovered_map_requires_explicit_edit_and_preserves_existing_order(
    page, dashboard, editing, width
):
    data, _ = editing
    path = data / "steamapps/workshop/content/108600/111/mods/PluginFolder/42/media/maps/TestTown"
    path.mkdir(parents=True)
    (path / "map.info").write_text("title=Display title\n", encoding="utf-8")
    ini_path = data / "Server/world.ini"
    original = INI.replace("Map=Muldraugh, KY", "Map=OldMap;Muldraugh, KY")
    ini_path.write_bytes(original.encode())
    editor.workshop.invalidate()
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.locator("#modPackages summary").click()
    page.locator('[data-modid="plugin"]').uncheck()
    page.locator('[data-modid="plugin"]').check()
    expect(page.locator("#modOrderList [data-order-id]")).to_have_count(2)
    page.get_by_role("tab", name="Порядок", exact=True).click()
    expect(page.locator("#modOrder")).to_contain_text("Найденные карты: TestTown")
    expect(page.locator("#mapList")).to_have_value("OldMap;Muldraugh, KY")
    assert "Map=OldMap;Muldraugh, KY\r\n" in editor.draft("world.ini")["texts"]["ini"]
    page.locator("#mapList").fill("TestTown;OldMap;Muldraugh, KY")
    page.locator("#mapEdit button").click()
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    assert "Map=TestTown;OldMap;Muldraugh, KY\r\n" in editor.draft("world.ini")["texts"]["ini"]
    assert ini_path.read_bytes() == original.encode()
    assert dashboard["actions"] == []


@pytest.mark.parametrize("completed", [True, False])
def test_operation_logs_preserve_filters_and_allow_return_to_all_logs(
    page, dashboard, editing, monkeypatch, completed
):
    data, _ = editing
    running = [not completed]
    monkeypatch.setattr(editor.ops, "op_busy", lambda: running[0])
    state_path = editor.state_dir("world.ini") / "state.json"
    state = {
        "status": "error" if completed else "applying",
        "operationStartedAt": "2026-10-01T12:00:00Z",
        "error": "Test operation failed",
    }
    if completed:
        state["operationCompletedAt"] = "2026-10-01T12:01:00Z"
    editor.save_json(state_path, state)
    requests = []

    def logs(route):
        requests.append(parse_qs(urlsplit(route.request.url).query))
        route.fulfill(
            json={
                "ok": True,
                "text": "2026-10-01T11:59:00Z ERROR database before\n"
                "2026-10-01T12:00:30Z ERROR database operation\n"
                "2026-10-01T12:02:00Z ERROR database after\n",
            }
        )

    page.route("**/api/logs**", logs)
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "console")
    page.locator("#logsFilter").fill("database")
    page.locator('#logLevels [data-level="error"]').click()
    navigate(page, "settings")
    page.locator("#draftMore").click()
    page.locator("#editorLogs").click()
    expect(page.locator("#logsScope")).to_be_visible()
    expect(page.locator("#logsPeriod")).to_contain_text("world.ini")
    if not completed:
        expect(page.locator("#logsPeriod")).to_contain_text("продолжается")
        state.update(status="error", operationCompletedAt="2026-10-01T12:01:00Z")
        editor.save_json(state_path, state)
        running[0] = False
        expect(page.locator("#logsPeriod")).not_to_contain_text("продолжается", timeout=20000)
    expect(page.locator("#logsOut")).to_contain_text("database operation")
    expect(page.locator("#logsOut")).not_to_contain_text("database before")
    expect(page.locator("#logsOut")).not_to_contain_text("database after")
    expect(page.locator("#logsFilter")).to_have_value("database")
    expect(page.locator('#logLevels [data-level="error"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    assert requests[-1] == {
        "since": ["2026-10-01T12:00:00.000Z"],
        "until": ["2026-10-01T12:01:00.000Z"],
        "tail": ["10000"],
    }
    page.locator("#logsClearPeriod").click()
    expect(page.locator("#logsScope")).to_be_hidden()
    expect(page.locator("#logsOut")).to_contain_text("database before")
    expect(page.locator("#logsOut")).to_contain_text("database after")
    assert requests[-1] == {}
    assert (data / "Server/world.ini").read_bytes() == INI.encode()
    assert dashboard["actions"] == []


def test_long_mod_list_on_phone(page, dashboard, editing):
    data, _ = editing
    for i in range(100):
        folder = data / f"steamapps/workshop/content/108600/111/mods/Additional{i}/42"
        folder.mkdir(parents=True)
        (folder / "mod.info").write_text(
            f"id=additional-{i}\nname=Additional mod {i}\n", encoding="utf-8"
        )
    editor.workshop.invalidate()
    page.set_viewport_size({"width": 390, "height": 900})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.locator("#modPackages summary").click()
    expect(page.locator("#modPackages [data-modid]")).to_have_count(102)
    page.locator('[data-modid="additional-99"]').scroll_into_view_if_needed()
    page.locator('[data-modid="additional-99"]').check()
    expect(page.locator("#modOrderList [data-order-id]")).to_have_count(3)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_disconnected_form_keeps_edit_until_explicit_retry(page, dashboard, editing):
    disconnected = [False]

    def drop_write(route):
        if disconnected[0] and route.request.method == "POST":
            route.abort()
        else:
            route.fallback()

    page.route("**/api/config-draft", drop_write)
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    disconnected[0] = True
    field = page.locator('[data-key="PublicName"]')
    field.fill("Offline draft")
    field.press("Tab")
    expect(page.locator("#draftRetry")).to_be_visible()
    page.get_by_role("tab", name="Мир", exact=True).click()
    expect(field).to_have_value("Offline draft")
    expect(page.get_by_role("tab", name="Сервер", exact=True)).to_have_attribute(
        "aria-selected", "true"
    )
    disconnected[0] = False
    page.locator("#draftRetry").click()
    expect(page.locator("#draftRetry")).to_be_hidden()
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Offline draft")
    assert "Offline draft" in editor.draft("world.ini")["texts"]["ini"]


@pytest.mark.parametrize("width", [1440, 390])
def test_multiline_and_list_fields_round_trip_into_same_draft(page, dashboard, editing, width):
    data, _ = editing
    original = (
        INI
        + "PublicDescription=First\\nSecond\r\nChatStreams=s,r,a\r\nServerWelcomeMessage=Welcome<LINE>Survivor\r\n"
    )
    (data / "Server/world.ini").write_bytes(original.encode())
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings", width == 390)
    description = page.locator('[data-key="PublicDescription"]')
    expect(description).to_have_value("First\nSecond")
    description.fill("Первая строка\nВторая строка")
    description.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    streams = page.locator('[data-key="ChatStreams"]')
    expect(streams).to_have_value("s\nr\na")
    streams.fill("s\nr\ny")
    streams.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    page.get_by_role("tab", name="Исходники", exact=True).click()
    source = page.locator("#iniSource").input_value()
    assert "PublicDescription=Первая строка\\nВторая строка" in source
    assert "ChatStreams=s,r,y" in source
    assert "ServerWelcomeMessage=Welcome<LINE>Survivor" in source
    assert (data / "Server/world.ini").read_bytes() == original.encode()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_inactive_mod_settings_are_preserved_separately_from_stock(page, dashboard, editing):
    data, _ = editing
    path = data / "steamapps/workshop/content/108600/111/mods/PluginFolder/42/media"
    path.mkdir()
    (path / "sandbox-options.txt").write_text(
        "option Mod.Count { type=integer, min=0, max=10, default=2, page=PluginPage, }",
        encoding="utf-8",
    )
    lua_path = data / "Server/world_SandboxVars.lua"
    lua_path.write_text(
        lua_path.read_text(encoding="utf-8").replace(
            "Zombies = 4,", "Zombies = 4, AnimalMetaPredator = true,"
        ),
        encoding="utf-8",
    )
    editor.workshop.invalidate()
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.locator("#modPackages summary").click()
    page.locator('[data-modid="plugin"]').uncheck()
    expect(page.locator("#modOrderList [data-order-id]")).to_have_count(1)
    navigate(page, "settings")
    page.get_by_role("tab", name="Мир", exact=True).click()
    expect(page.locator('[data-key="AnimalMetaPredator"]')).to_be_checked()
    expect(page.locator('[data-key="Mod.Count"]')).to_have_count(0)
    page.get_by_role("tab", name="Настройки модов", exact=True).click()
    expect(page.locator('[data-key="Mod.Count"]')).to_have_value("2")
    expect(page.locator("#configFields")).to_contain_text("Выключенные / отсутствующие моды")
    assert "Count = 2" in editor.draft("world.ini")["texts"]["sandbox"]


def test_future_version_can_be_disabled_but_cannot_be_selected_again(page, dashboard, editing):
    data, _ = editing
    path = data / "steamapps/workshop/content/108600/111/mods/Future/42.99"
    path.mkdir(parents=True)
    (path / "mod.info").write_text("id=future\nname=Future only", encoding="utf-8")
    ini = data / "Server/world.ini"
    ini.write_bytes(
        INI.replace("Mods=\\library;\\plugin", "Mods=\\library;\\plugin;\\future").encode()
    )
    editor.workshop.invalidate()
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.locator("#modPackages summary").click()
    future = page.locator('[data-modid="future"]')
    expect(future).to_be_checked()
    expect(future).to_be_enabled()
    future.uncheck()
    expect(future).not_to_be_checked()
    expect(future).to_be_disabled()
    expect(page.locator("#modPackages")).to_contain_text("Нет подходящего каталога B42")


def test_source_then_mod_edit_use_one_draft_without_restoring_old_selection(
    page, dashboard, editing
):
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    page.get_by_role("tab", name="Исходники", exact=True).click()
    source = page.locator("#iniSource")
    source.fill(source.input_value().replace("PublicName=Сервер", "PublicName=Source draft"))
    navigate(page, "mods")
    page.locator("#modPackages summary").click()
    page.locator('[data-modid="plugin"]').uncheck()
    expect(page.locator("#modOrderList [data-order-id]")).to_have_count(1)
    navigate(page, "settings")
    assert "PublicName=Source draft" in source.input_value()
    assert "Mods=\\library\n" in source.input_value()
    page.locator("#sourceSave").click()
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    draft = editor.draft("world.ini")
    assert "PublicName=Source draft" in draft["texts"]["ini"]
    assert "Mods=\\library\r\n" in draft["texts"]["ini"]


@pytest.mark.parametrize("width", [1440, 390])
def test_failed_start_history_restores_configuration_into_shared_draft(
    page, dashboard, editing, monkeypatch, width
):
    page.emulate_media(reduced_motion="reduce")
    current = editor.draft("world.ini")
    current = editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "ini": {"PublicName": "Failed start"},
        }
    )
    monkeypatch.setattr(editor.dockerlib, "container_start", lambda name: (1, "", "failed"))
    with pytest.raises(editor.EditorError):
        editor.run(
            {"file": "world.ini", "draftRevision": current["draftRevision"], "restart": True}
        )
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    expect(page.locator("#configOperationResult")).to_have_count(0)
    navigate(page, "settings", width == 390)
    expect(page.locator("#configStatus")).to_have_text("Ошибка")
    page.get_by_role("tab", name="История изменений", exact=True).click()
    history_id = editor.draft("world.ini")["state"]["historyId"]
    page.locator(f'[data-restore-config="{history_id}"]').click()
    page.locator("#modalOk").click()
    expect(page.get_by_role("alertdialog")).to_contain_text("Изменения при восстановлении")
    page.locator("#modalOk").click()
    navigate(page, "settings", width == 390)
    page.get_by_role("tab", name="Сервер", exact=True).click()
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Сервер")
    assert "PublicName=Failed start" in editor.read_profile("world.ini")["ini"]
    assert editor.draft("world.ini")["changed"]


@pytest.mark.parametrize("width", [1440, 390])
def test_individual_toggle_restores_original_order(page, dashboard, editing, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.locator("#modPackages summary").click()
    page.locator('[data-modid="library"]').uncheck()
    expect(page.locator("#modOrderList [data-order-id]")).to_have_count(1)
    page.locator('[data-modid="library"]').check()
    expect(page.locator("#modOrderList [data-order-id]")).to_have_count(2)
    page.get_by_role("tab", name="Порядок", exact=True).click()
    expect(page.locator("#modOrder [data-order-id]")).to_have_count(2)
    assert page.locator("#modOrder [data-order-id]").evaluate_all(
        "rows => rows.map(row => row.dataset.orderId)"
    ) == ["library", "plugin"]
    assert "Mods=\\library;\\plugin\r\n" in editor.draft("world.ini")["texts"]["ini"]


def test_prepare_modal_previews_only_download_stage(page, dashboard, editing):
    current = editor.draft("world.ini")
    editor.patch(
        {
            "file": "world.ini",
            "draftRevision": current["draftRevision"],
            "ini": {"PublicName": "Later name"},
            "sandbox": {"Zombies": 99},
            "mods": {"items": ["111", "222"], "selected": ["library", "unknown-new"]},
        }
    )
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.locator("#prepareWorkshop").click()
    expect(page.get_by_role("alertdialog")).to_contain_text("Загрузить Workshop items?")
    expect(page.get_by_role("alertdialog")).to_contain_text("+WorkshopItems=111;222")
    expect(page.get_by_role("alertdialog")).not_to_contain_text("Later name")
    expect(page.get_by_role("alertdialog")).not_to_contain_text("+ Zombies = 99")


def test_new_mod_password_is_masked_before_first_save(page, dashboard, editing):
    data, _ = editing
    path = data / "steamapps/workshop/content/108600/111/mods/PluginFolder/42/media"
    path.mkdir()
    (path / "sandbox-options.txt").write_text(
        "option Mod.NewPassword { type=string, default=public-default, }", encoding="utf-8"
    )
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    page.get_by_role("tab", name="Настройки модов", exact=True).click()
    field = page.locator('[data-key="Mod.NewPassword"]')
    expect(field).to_have_attribute("type", "password")
    expect(field).to_have_value("")
    field.fill("private-editor-token")
    field.press("Tab")
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    page.get_by_role("tab", name="Исходники", exact=True).click()
    assert "private-editor-token" not in page.locator("#sandboxSource").input_value()
    assert "Mod.NewPassword" not in editor.modpack({"file": "world.ini"})["pack"]["sandbox"]


@pytest.mark.parametrize("width", [390, 1440])
def test_operation_locks_mod_changes_but_keeps_inspection_and_restores_constraints(
    page, dashboard, editing, width
):
    data, _ = editing
    path = data / "steamapps/workshop/content/108600/111/mods/Future/42.99"
    path.mkdir(parents=True)
    (path / "mod.info").write_text("id=future\nname=Future only", encoding="utf-8")
    editor.workshop.invalidate()
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.locator("#modPackages summary").click()
    expect(page.locator('[data-modid="plugin"]')).to_be_enabled()
    original = editor.draft("world.ini")
    page.evaluate(
        "renderOp({active:{op:'apply-config',phase:'Запуск',message:'Приёмка блокировки'},history:[]})"
    )
    expect(page.locator('[data-modid="plugin"]')).to_be_disabled(timeout=3000)
    for selector in (
        "#workshopInput",
        "#workshopAdd button",
        '[data-remove-item="111"]',
        "#modImport",
        "#modExport",
        "#modRescan",
        "#mapList",
        "#mapEdit button",
    ):
        expect(page.locator(selector)).to_be_disabled()
    expect(page.locator(".import-button")).to_have_attribute("aria-disabled", "true")
    # Read controls remain useful; a redraw must not recreate enabled edit buttons.
    page.locator("#modQuery").fill("library")
    page.locator("#modSortNew").select_option("title")
    expect(page.locator('[data-modid="plugin"]')).to_be_disabled()
    expect(page.locator("#mapEdit button")).to_be_disabled()
    page.get_by_role("tab", name="Порядок", exact=True).click()
    page.locator("#orderQuery").fill("library")
    expect(page.locator('[data-position-id="library"]')).to_be_disabled()
    navigate(page, "settings", width <= 740)
    expect(page.locator('[data-key="PublicName"]')).to_be_disabled()
    page.get_by_role("tab", name="Исходники", exact=True).click()
    expect(page.locator("#iniSource")).to_have_attribute("readonly", "")
    expect(page.locator("#sourceSave")).to_be_disabled()
    page.evaluate("renderOp({active:null,history:[]})")
    expect(page.locator("#sourceSave")).to_be_enabled()
    navigate(page, "mods")
    page.get_by_role("tab", name="Состав", exact=True).click()
    expect(page.locator('[data-modid="plugin"]')).to_be_enabled()
    expect(page.locator('[data-modid="future"]')).to_be_disabled()
    expect(page.locator("#modImport")).to_be_enabled()
    expect(page.locator(".import-button")).to_have_attribute("aria-disabled", "false")
    page.get_by_role("tab", name="Порядок", exact=True).click()
    expect(page.locator('[aria-label="Поднять library"]')).to_be_disabled()
    expect(page.locator('[data-position-id="library"]')).to_be_enabled()
    assert editor.draft("world.ini")["draftRevision"] == original["draftRevision"]
    assert dashboard["actions"] == []


@pytest.mark.parametrize("entry", ["configDiff", "configApply"])
@pytest.mark.parametrize("width", [390, 1280, 1440])
@pytest.mark.parametrize("save_during_click", [False, True])
def test_first_click_flushes_focused_field_before_review(
    page, dashboard, editing, entry, width, save_during_click
):
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings", width <= 740)
    field = page.locator('[data-key="PublicName"]')
    field.fill("First click draft")
    expect(page.locator("#draftLabel")).to_contain_text("Есть несохранённые поля")
    assert page.evaluate("""() => {
        const event = new Event('beforeunload', {cancelable:true});
        window.dispatchEvent(event); return event.defaultPrevented;
    }""")
    button = page.locator(f"#{entry}")
    if save_during_click:
        # A fast blur save can complete between pointer press and release.
        button.scroll_into_view_if_needed()
        expect(field).to_be_focused()
        box = button.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
        page.mouse.up()
    else:
        button.click()
    expect(page.get_by_role("alertdialog")).to_contain_text("+PublicName=First click draft")
    assert "PublicName=First click draft" in editor.draft("world.ini")["texts"]["ini"]
    assert dashboard["actions"] == []


def test_metadata_refresh_keeps_unblurred_mod_setting_and_focus(page, dashboard, editing):
    data, _ = editing
    media = data / "steamapps/workshop/content/108600/111/mods/PluginFolder/42/media"
    media.mkdir()
    (media / "sandbox-options.txt").write_text(
        "option Mod.Count { type=integer, min=1, max=100, default=2, }", encoding="utf-8"
    )
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    page.get_by_role("tab", name="Настройки модов", exact=True).click()
    field = page.locator('[data-key="Mod.Count"]')
    field.fill("37")
    field.evaluate("el => el.protectedNode = true")
    with page.expect_response("**/api/mods?*refresh=1"):
        page.evaluate("document.querySelector('#modRescan').click()")
    page.wait_for_timeout(100)  # Complete metadata rendering without blurring the field.
    expect(field).to_have_value("37")
    expect(field).to_be_focused()
    assert field.evaluate("el => el.protectedNode")
    assert not editor.draft("world.ini")["changed"]
    page.evaluate("document.querySelector('#configDiff').click()")
    expect(page.get_by_role("alertdialog")).to_contain_text("Count = 37")
    assert "Count = 37" in editor.draft("world.ini")["texts"]["sandbox"]


@pytest.mark.parametrize("switch_profile", [False, True])
def test_late_mod_metadata_cannot_replace_profile_or_new_selection(
    page, dashboard, editing, switch_profile
):
    data, _ = editing
    (data / "Server/another.ini").write_bytes(INI.replace("\\library;\\plugin", "").encode())
    pending = []
    page.route("**/api/mods?*refresh=1", lambda route: pending.append(route))
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    old = editor.mod_response("world.ini", draft_mode=True)
    page.locator("#modRescan").click()
    if switch_profile:
        page.locator("#configProfile").select_option("another.ini")
        count = 0
    else:
        page.locator("#modPackages summary").click()
        page.locator('[data-modid="plugin"]').uncheck()
        count = 1
    expect(page.locator("#modOrderList [data-order-id]")).to_have_count(count)
    assert pending
    pending[0].fulfill(json=old)
    page.wait_for_timeout(100)  # Run the delayed response's promise callback.
    expect(page.locator("#modOrderList [data-order-id]")).to_have_count(count)
    expect(page.locator("#configError")).to_be_hidden()
    assert dashboard["actions"] == []


def test_failed_field_save_preserves_input_and_blocks_review(page, dashboard, editing):
    def reject_post(route):
        if route.request.method == "POST":
            route.fulfill(status=500, json={"ok": False, "error": "Ошибка сохранения черновика"})
        else:
            route.fallback()

    page.route("**/api/config-draft", reject_post)
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    field = page.locator('[data-key="PublicName"]')
    field.fill("Unsaved field survives")
    page.locator("#configApply").click()
    expect(page.locator("#configError")).to_contain_text("Ошибка сохранения черновика")
    expect(page.get_by_role("alertdialog")).to_be_hidden()
    expect(field).to_have_value("Unsaved field survives")
    expect(page.locator("#draftRetry")).to_be_visible()
    page.unroute("**/api/config-draft", reject_post)
    page.locator("#draftRetry").click()
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    assert "PublicName=Unsaved field survives" in editor.draft("world.ini")["texts"]["ini"]
    assert dashboard["actions"] == []


def test_missing_server_state_does_not_offer_stopped_server_save(page, dashboard, editing):
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    page.evaluate("S.overview = null; ConfigEditor.route('settings')")
    expect(page.locator("#configSave")).to_be_disabled()
    expect(page.locator("#configSaveHint")).to_have_count(0)
    page.evaluate("S.overview = {containerInfo:{running:false}}; ConfigEditor.route('settings')")
    expect(page.locator("#configSave")).to_be_enabled()
    page.evaluate("S.overview = {containerInfo:{running:true}}; ConfigEditor.route('settings')")
    expect(page.locator("#configSave")).to_be_disabled()
    assert not editor.draft("world.ini")["changed"]
    assert dashboard["actions"] == []


@pytest.fixture
def map_editing(editing):
    data, _ = editing
    path = data / "steamapps/workshop/content/108600/111/mods/PluginFolder/42/media/maps/TestTown"
    path.mkdir(parents=True)
    (path / "map.info").write_text("title=Test town\n", encoding="utf-8")
    return editing


@pytest.mark.parametrize("width", [390, 1440])
def test_unsaved_map_input_survives_mod_controls_before_explicit_save(
    page, dashboard, map_editing, width
):
    data, _ = map_editing
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.get_by_role("tab", name="Порядок", exact=True).click()
    maps = page.locator("#mapList")
    maps.fill("TestTown;Muldraugh, KY")
    page.get_by_role("tab", name="Состав", exact=True).click()
    page.locator("#modQuery").fill("library")
    expect(maps).to_have_value("TestTown;Muldraugh, KY")
    page.locator("#modSortNew").select_option("title")
    expect(maps).to_have_value("TestTown;Muldraugh, KY")
    # Saving a ModID must not recreate the separate, still pending map field.
    page.locator("#modPackages summary").click()
    page.locator('[data-modid="plugin"]').uncheck()
    expect(page.locator("#modOrderList [data-order-id]")).to_have_count(1)
    expect(maps).to_have_value("TestTown;Muldraugh, KY")
    page.locator('[data-modid="plugin"]').check()
    expect(page.locator("#modOrderList [data-order-id]")).to_have_count(2)
    page.get_by_role("tab", name="Порядок", exact=True).click()
    maps.focus()
    maps.evaluate("el => el.protectedNode = true")
    with page.expect_response("**/api/mods?*refresh=1"):
        page.evaluate("document.querySelector('#modRescan').click()")
    page.wait_for_timeout(100)
    expect(maps).to_be_focused()
    assert maps.evaluate("el => el.protectedNode")
    maps.scroll_into_view_if_needed()
    field_box = maps.bounding_box()
    assert field_box["y"] + field_box["height"] < page.locator("#draftBar").bounding_box()["y"]
    form_box = page.locator("#mapEdit").bounding_box()
    assert form_box["y"] + form_box["height"] < page.locator("#draftBar").bounding_box()["y"]
    assert "Map=Muldraugh, KY\r\n" in editor.draft("world.ini")["texts"]["ini"]
    page.locator("#mapEdit button").click()
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    assert "Map=TestTown;Muldraugh, KY\r\n" in editor.draft("world.ini")["texts"]["ini"]
    assert (data / "Server/world.ini").read_bytes() == INI.encode()
    page.wait_for_function("""() => {
        const event = new Event('beforeunload', {cancelable:true});
        window.dispatchEvent(event); return !event.defaultPrevented;
    }""")
    assert dashboard["actions"] == []


def test_map_and_server_field_share_one_review_draft(page, dashboard, map_editing):
    data, _ = map_editing
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.get_by_role("tab", name="Порядок", exact=True).click()
    page.locator("#mapList").fill("TestTown;Muldraugh, KY")
    navigate(page, "settings")
    page.locator('[data-key="PublicName"]').fill("Shared map draft")
    page.locator("#configDiff").click()
    expect(page.get_by_role("alertdialog")).to_contain_text("+Map=TestTown;Muldraugh, KY")
    expect(page.get_by_role("alertdialog")).to_contain_text("+PublicName=Shared map draft")
    page.locator("#modalCancel").click()
    navigate(page, "mods")
    expect(page.locator("#modMapEditor ol")).to_contain_text("TestTown")
    assert "Mods=\\library;\\plugin\r\n" in editor.draft("world.ini")["texts"]["ini"]
    assert (data / "Server/world.ini").read_bytes() == INI.encode()
    assert dashboard["actions"] == []


def test_discard_removes_pending_map_input_and_unload_warning(page, dashboard, map_editing):
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.get_by_role("tab", name="Порядок", exact=True).click()
    page.locator("#mapList").fill("TestTown;Muldraugh, KY")
    page.locator("#draftMore").click()
    page.locator("#configDiscard").click()
    page.locator("#modalOk").click()
    expect(page.get_by_role("alertdialog")).to_be_hidden()
    expect(page.locator("#mapList")).to_have_value("Muldraugh, KY")
    expect(page.locator("#draftBar")).to_be_hidden()
    assert not editor.draft("world.ini")["changed"]
    assert not page.evaluate("""() => {
        const event = new Event('beforeunload', {cancelable:true});
        window.dispatchEvent(event); return event.defaultPrevented;
    }""")
    assert dashboard["actions"] == []


def test_map_save_failure_keeps_input_for_retry(page, dashboard, map_editing):
    def reject_post(route):
        if route.request.method == "POST":
            route.fulfill(status=500, json={"ok": False, "error": "Ошибка сохранения карт"})
        else:
            route.fallback()

    page.route("**/api/config-draft", reject_post)
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "mods")
    page.get_by_role("tab", name="Порядок", exact=True).click()
    page.locator("#mapList").fill("TestTown;Muldraugh, KY")
    page.locator("#mapEdit button").click()
    expect(page.locator("#configError")).to_contain_text("Ошибка сохранения карт")
    page.evaluate(
        "document.querySelector('#modQuery').value='library'; document.querySelector('#modQuery').dispatchEvent(new Event('input'))"
    )
    expect(page.locator("#mapList")).to_have_value("TestTown;Muldraugh, KY")
    assert "Map=Muldraugh, KY\r\n" in editor.draft("world.ini")["texts"]["ini"]
    page.unroute("**/api/config-draft", reject_post)
    page.locator("#draftRetry").click()
    expect(page.locator("#draftSaved")).to_have_text("Черновик сохранён")
    expect(page.locator("#modMapEditor ol")).to_contain_text("TestTown")
    assert "Map=TestTown;Muldraugh, KY\r\n" in editor.draft("world.ini")["texts"]["ini"]
    assert dashboard["actions"] == []


@pytest.fixture
def profile_history(editing):
    data, _ = editing
    (data / "Server/another.ini").write_bytes(INI.replace("Сервер", "Другой сервер").encode())
    ids = {}
    for file in ("world.ini", "another.ini"):
        ids[file] = editor.commit(file, editor.read_profile(file), "История " + file)
    return data, ids


@pytest.mark.parametrize("width", [390, 1440])
def test_history_switches_with_profile_and_restores_its_own_version(
    page, dashboard, profile_history, width
):
    data, ids = profile_history
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings", width <= 740)
    page.get_by_role("tab", name="История изменений", exact=True).click()
    expect(page.locator("#configHistory")).to_contain_text("История world.ini")
    page.locator("#configProfile").select_option("another.ini")
    expect(page.locator("#configHistory")).to_contain_text("История another.ini")
    expect(page.locator(f'[data-restore-config="{ids["world.ini"]}"]')).to_have_count(0)
    page.locator(f'[data-restore-config="{ids["another.ini"]}"]').click()
    expect(page.get_by_role("alertdialog")).to_contain_text("another.ini")
    page.locator("#modalOk").click()
    expect(page.get_by_role("alertdialog")).to_contain_text("Изменения при восстановлении")
    page.locator("#modalCancel").click()
    page.get_by_role("tab", name="Сервер", exact=True).click()
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Другой сервер")
    assert (data / "Server/world.ini").read_bytes() == INI.encode()
    assert (data / "Server/another.ini").read_bytes() == INI.replace(
        "Сервер", "Другой сервер"
    ).encode()
    assert dashboard["actions"] == []


@pytest.mark.parametrize("failure", [False, True])
def test_late_history_response_does_not_replace_new_profile(
    page, dashboard, profile_history, failure
):
    held = []
    old = editor.history("world.ini")
    page.route("**/api/config-history?file=world.ini", lambda route: held.append(route))
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    page.get_by_role("tab", name="История изменений", exact=True).click()
    page.locator("#configProfile").select_option("another.ini")
    expect(page.locator("#configHistory")).to_contain_text("История another.ini")
    assert held
    held[0].fulfill(
        status=500 if failure else 200,
        json={"ok": False, "error": "Ошибка истории прежнего профиля"} if failure else old,
    )
    page.wait_for_timeout(100)
    expect(page.locator("#configHistory")).to_contain_text("История another.ini")
    expect(page.locator("#configHistory")).not_to_contain_text("История world.ini")
    expect(page.locator("#configError")).to_be_hidden()
    assert dashboard["actions"] == []


def test_history_failure_can_be_retried_without_switching_profile(page, dashboard, profile_history):
    def reject(route):
        route.fulfill(status=500, json={"ok": False, "error": "Ошибка загрузки истории"})

    page.route("**/api/config-history?file=world.ini", reject)
    page.goto(dashboard["url"])
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    navigate(page, "settings")
    page.get_by_role("tab", name="История изменений", exact=True).click()
    expect(page.locator("#configHistory")).to_contain_text("Ошибка загрузки истории")
    expect(page.locator("#configHistory button")).to_have_count(0)
    page.unroute("**/api/config-history?file=world.ini", reject)
    page.get_by_role("tab", name="История изменений", exact=True).click()
    expect(page.locator("#configHistory")).to_contain_text("История world.ini")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    expect(page.locator("#configError")).to_be_hidden()
    assert not editor.draft("world.ini")["changed"]
    assert dashboard["actions"] == []


@pytest.mark.parametrize("tablist,route", [("configTabs", "settings"), ("modTabs", "mods")])
def test_editor_tabs_have_roving_focus_and_linked_named_panels(
    page, dashboard, editing, tablist, route
):
    page.goto(dashboard["url"] + "/#/" + route)
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    tabs = page.locator(f"#{tablist} [role=tab]")
    first = tabs.first
    first.focus()
    expect(first).to_have_attribute("tabindex", "0")
    first.press("End")
    expect(tabs.last).to_be_focused()
    expect(tabs.last).to_have_attribute("aria-selected", "true")
    expect(tabs.last).to_have_attribute("tabindex", "0")
    expect(first).to_have_attribute("tabindex", "-1")
    panel = page.locator("#" + tabs.last.get_attribute("aria-controls"))
    expect(panel).to_be_visible()
    expect(panel).to_have_attribute("role", "tabpanel")
    expect(panel).to_have_attribute("aria-labelledby", tabs.last.get_attribute("id"))
    tabs.last.press("Home")
    expect(first).to_be_focused()
    expect(first).to_have_attribute("aria-selected", "true")
    expect(page.locator("#" + first.get_attribute("aria-controls"))).to_be_visible()
