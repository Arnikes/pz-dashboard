"""Reproduce the ten UI/UX audit scenarios against the shipped interface."""
# ruff: noqa: F811 -- imported pytest fixtures are injected by name.

import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

from test_editors import editing, env  # noqa: F401

pytestmark = pytest.mark.browser
EVIDENCE = Path(__file__).resolve().parents[2] / ".tmp-uiux-fixes-20261007/evidence"


def start(page, dashboard, route, language="ru", width=390):
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 844})
    page.goto(dashboard["url"] + "/#/" + route)
    expect(page.locator("#btnStop")).to_be_enabled()
    page.evaluate("liveSource?.close(); liveSource=null; clearTimeout(sseStartupTimer)")


def screenshot(page, name, full_page=True):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(EVIDENCE / f"{name}.png"), full_page=full_page)


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_diagnostics_reflow_preserves_hash_url_path_and_list_errors(page, dashboard, width):
    start(page, dashboard, "events", width=width)
    tokens = [
        "a" * 64,
        "https://steamcommunity.com/sharedfiles/filedetails/?id=123456789",
        "C:/" + "long-path/" * 14,
    ]
    for token in tokens:
        item = {"ts": "2026-10-07T12:00:00Z", "type": "error", "text": "Diagnostic: " + token}
        page.evaluate("item=>renderEvents({ok:true,items:[item]})", item)
        expect(page.locator("#eventsBody")).to_contain_text(token)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.evaluate("location.hash='#/overview'")
        expect(page.locator("#view-overview")).to_be_visible()
        expect(page.locator("#recentBody")).to_contain_text(token)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.evaluate("location.hash='#/events'")
        expect(page.locator("#view-events")).to_be_visible()
    screenshot(page, f"diagnostics-{width}")
    for route in ["backups", "events"]:
        page.evaluate("route=>location.hash='#/'+route", route)
        expect(page.locator(f"#view-{route}")).to_be_visible()
        page.evaluate(
            "({route,error})=>{const data={ok:false,error};if(route==='backups')renderBackups(data);else renderEvents(data)}",
            {"route": route, "error": "a" * 64},
        )
        expect(page.locator(f"#{route}Body")).to_contain_text("a" * 64)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_log_coalescing_keeps_identifiers_and_timestamp_precision(page, dashboard):
    start(page, dashboard, "console")
    rows = [
        "2026-10-07T12:00:00.001Z WARN Failed to load Workshop item 123456789",
        "2026-10-07T12:00:00.001Z WARN Failed to load Workshop item 987654321",
        "2026-10-07T12:00:00.002Z WARN Failed to load Workshop item 987654321",
    ]
    page.evaluate("text=>renderLogs({ok:true,text})", "\n".join(rows + [rows[-1]]))
    expect(page.locator("#logsOut")).to_contain_text("123456789")
    expect(page.locator("#logsOut")).to_contain_text("987654321")
    expect(page.locator("#logsOut > span")).to_have_count(3)
    expect(page.locator("#logsOut .l-dup")).to_have_count(1)
    expect(page.locator("#logsCount")).to_have_text("Строк: 4 из 4")
    screenshot(page, "logs-identities")


@pytest.mark.parametrize("language", ["ru", "en"])
def test_log_empty_states_reset_text_level_and_operation_period(page, dashboard, language):
    frame = {"ok": True, "text": "2026-10-07T12:00:00Z INFO ready\n2026-10-07T12:01:00Z WARN retry"}
    page.route("**/api/logs**", lambda route: route.fulfill(json=frame))
    start(page, dashboard, "console", language)
    page.evaluate("frame=>renderLogs(frame)", frame)
    page.locator('#logLevels [data-level="error"]').click()
    page.locator("#logsFilter").fill("missing")
    page.evaluate(
        "S.logsSince=Date.parse('2026-10-07T13:00:00Z');S.logsUntil=Date.parse('2026-10-07T14:00:00Z');S.logsProfile='world.ini';renderLogsFiltered()"
    )
    expect(page.locator("#logsOut")).to_have_text(
        "Нет строк по выбранным фильтрам."
        if language == "ru"
        else "No lines match the selected filters."
    )
    expect(page.locator("#logsCount")).to_have_text(
        "Строк: 0 из 2" if language == "ru" else "Lines: 0 of 2"
    )
    screenshot(page, f"logs-filter-{language}")
    page.locator("#logsResetFilters").click()
    expect(page.locator("#logsFilter")).to_have_value("")
    expect(page.locator("#logsFilter")).to_be_focused()
    assert page.evaluate("S.logsLevel==='all' && !S.logsSince && !S.logsUntil && !S.logsProfile")
    expect(page.locator('#logLevels [data-level="all"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#logsOut")).to_contain_text("ready")
    page.evaluate("renderLogs({ok:true,text:'\\n   \\n'})")
    expect(page.locator("#logsOut")).to_have_text(
        "Нет данных логов" if language == "ru" else "No log data"
    )


def test_console_status_tracks_disabled_available_and_busy_states(page, dashboard):
    start(page, dashboard, "console")
    original = dashboard["overview"]
    stopped = {**original, "containerInfo": {"running": False}, "rcon": {"state": "down"}}
    page.evaluate("data=>applyOverview(data)", stopped)
    expect(page.locator("#consoleInput")).to_be_disabled()
    expect(page.locator("#consoleOut")).to_contain_text("Ожидаем доступность RCON")
    expect(page.locator("#consoleOut")).not_to_contain_text("Консоль доступна")
    screenshot(page, "rcon-unavailable")
    page.evaluate("data=>applyOverview(data)", original)
    expect(page.locator("#consoleInput")).to_be_enabled()
    expect(page.locator("#consoleOut")).to_contain_text("Консоль доступна")
    page.evaluate("S.actionPending=true;updateButtons()")
    expect(page.locator("#consoleInput")).to_be_disabled()
    expect(page.locator("#consoleOut")).to_contain_text("Запрос отправляется")
    page.evaluate("S.actionPending=false;updateButtons()")
    page.evaluate("data=>applyOverview({...data,mode:'remote',rcon:{state:'down'}})", original)
    expect(page.locator("#consoleInput")).to_be_disabled()
    expect(page.locator("#consoleOut")).to_contain_text("Ожидаем доступность RCON")


@pytest.mark.parametrize("language", ["ru", "en"])
def test_watchdog_control_names_action_when_off_and_on(page, dashboard, language):
    start(page, dashboard, "maintenance", language)
    for enabled in [False, True]:
        frame = {**dashboard["overview"], "settings": {"watchdog": {"enabled": enabled}}}
        page.evaluate("data=>applyOverview(data)", frame)
        assert page.locator("#wdSwitch").is_checked() == enabled
        expect(page.locator("#wdSwitch").locator("..")).to_contain_text(
            "Следить за RCON" if language == "ru" else "Monitor RCON"
        )
        if not enabled:
            screenshot(page, f"watchdog-off-{language}")


@pytest.mark.parametrize("language", ["ru", "en"])
def test_setting_search_explains_scope_and_opens_matching_section(
    page, dashboard, editing, language
):  # noqa: F811
    start(page, dashboard, "settings", language)
    expect(page.locator('[data-key="PublicName"]')).to_be_enabled()
    page.locator("#configSearch").fill("Zombies")
    expect(page.locator("#configSearchScope")).to_have_text(
        "Поиск в настройках сервера" if language == "ru" else "Search server settings"
    )
    expect(page.locator('[data-search-tab="world"]')).to_be_visible()
    screenshot(page, f"settings-search-{language}")
    page.locator('[data-search-tab="world"]').click()
    expect(page.locator('[data-key="Zombies"]')).to_be_visible()
    expect(page.locator('#configTabs [data-tab="world"]')).to_have_attribute(
        "aria-selected", "true"
    )
    expect(page.locator("#configSearch")).to_have_value("Zombies")
    expect(page.locator("#configSearch")).to_have_attribute(
        "aria-label", "Поиск в настройках мира" if language == "ru" else "Search world settings"
    )


@pytest.mark.parametrize("width", [320, 390, 1440])
@pytest.mark.parametrize("language", ["ru", "en"])
def test_player_sanctions_have_visible_names_and_confirmation(page, dashboard, width, language):
    start(page, dashboard, "players", language, width)
    page.evaluate("renderPlayers({ok:true,count:1,names:['Maple very long name '.repeat(15)]})")
    kick = page.locator('[data-p="kick"]')
    ban = page.locator('[data-p="ban"]')
    expect(kick).to_have_text("Кикнуть" if language == "ru" else "Kick")
    expect(ban).to_have_text("Забанить" if language == "ru" else "Ban")
    assert kick.bounding_box()["height"] >= (44 if width <= 740 else 30)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    screenshot(page, f"players-{language}-{width}")
    kick.click()
    expect(page.get_by_role("alertdialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(kick).to_be_focused()
    assert dashboard["actions"] == []


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("failure", ["network", "timeout"])
def test_login_transport_retry_preserves_input_without_persisting_secret(
    page, static_url, language, failure
):
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    if failure == "timeout":
        page.clock.install()
        page.route("**/api/auth/login", lambda route: None)
    else:
        page.route("**/api/auth/login", lambda route: route.abort())
    page.goto(static_url + "/login.html")
    page.locator("#login").fill("audit-user")
    page.locator("#password").fill("fictional-audit-password")
    page.locator("#loginSubmit").click()
    if failure == "timeout":
        page.clock.run_for(11000)
    expect(page.locator("#loginError")).to_be_visible()
    expect(page.locator("#password")).to_have_value("fictional-audit-password")
    expect(page.locator("#login")).to_have_value("audit-user")
    assert "fictional-audit-password" not in page.evaluate(
        "JSON.stringify([localStorage,sessionStorage])"
    )
    requests = []
    page.route(
        "**/api/auth/login",
        lambda route: (
            requests.append(route.request.post_data_json),
            route.fulfill(status=401, json={"ok": False, "error": "Rejected"}),
        ),
    )
    page.locator("#loginSubmit").click()
    expect(page.locator("#loginError")).to_have_text("Rejected")
    assert requests[0]["password"] == "fictional-audit-password"
    expect(page.locator("#password")).to_have_value("")


@pytest.mark.parametrize("width", [320, 390, 1440])
@pytest.mark.parametrize("language", ["ru", "en"])
def test_profile_selector_has_no_adjacent_status_or_help(
    page, dashboard, editing, width, language
):  # noqa: F811
    start(page, dashboard, "settings", language, width)
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    expect(page.locator("#configActive")).to_be_hidden()
    expect(page.locator('[data-help="configProfileHelp"], #configProfileHelp')).to_have_count(0)
    expect(page.locator("#configProfile")).not_to_have_attribute("title")
    expect(page.locator("#configProfile")).to_have_accessible_description(
        "Активен на сервере" if language == "ru" else "Active on server"
    )
    assert dashboard["actions"] == []


@pytest.mark.parametrize("language", ["ru", "en"])
def test_variable_fonts_load_all_weights_without_duplicate_urls(page, dashboard, editing, language):  # noqa: F811
    start(page, dashboard, "settings", language)
    loaded = page.evaluate("""async()=>{
        for(const [family,weights] of [['Golos Text',[400,500,600]],['JetBrains Mono',[400,500,700]]])
          for(const weight of weights) {
            const faces=await document.fonts.load(`${weight} 16px "${family}"`,'PZ Пульт №123');
            if(faces.length!==2||faces.some(f=>f.status!=='loaded')) throw new Error('Font missing');
          }
        return performance.getEntriesByType('resource').filter(e=>e.name.endsWith('.woff2')).map(e=>({name:e.name,bytes:e.encodedBodySize}));
    }""")
    assert len(loaded) == 4
    assert all("-400-" in r["name"] for r in loaded)
    assert len({r["name"] for r in loaded}) == 4
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / f"font-downloads-{language}.json").write_text(
        json.dumps(loaded, indent=2), encoding="utf-8"
    )


def test_profile_selector_stays_compact_in_loading_other_and_unknown_context(page, dashboard, editing):
    data, context = editing
    (data / "Server/other.ini").write_text("PublicName=Other\n", encoding="utf-8")
    held = []
    page.route("**/api/config-draft?file=other.ini", lambda route: held.append(route))
    start(page, dashboard, "settings")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    expect(page.locator("#configProfile")).to_be_enabled()
    page.locator("#configProfile").select_option("other.ini")
    expect(page.locator("#configProfile")).to_be_disabled()
    expect(page.locator("#configActive")).to_be_hidden()
    expect(page.locator('[data-help="configProfileHelp"], #configProfileHelp')).to_have_count(0)
    assert len(held) == 1
    held[0].fallback()
    expect(page.locator("#configProfile")).to_be_enabled()
    expect(page.locator("#configProfile")).to_have_attribute("data-state", "other")
    expect(page.locator("#configProfile")).to_have_accessible_description("Другой профиль")
    expect(page.locator("#configActive")).to_be_hidden()
    context["activeFile"] = None
    page.locator("#configProfile").select_option("world.ini")
    expect(page.locator("#configProfile")).to_be_enabled()
    expect(page.locator("#configProfile")).to_have_attribute("data-state", "unknown")
    expect(page.locator("#configProfile")).to_have_accessible_description("Не подтверждён")
    expect(page.locator("#configActive")).to_be_hidden()
    assert dashboard["actions"] == []
