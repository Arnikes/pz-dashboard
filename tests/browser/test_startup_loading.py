"""First opening stays bounded and keeps incomplete editors safely locked."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from browser_evidence import capture_evidence
from test_editors import editing, env  # noqa: F401


def hold_startup_requests(page, pattern):
    """A browser request event can precede its Python interception callback."""
    held = []
    marker = f"startupRequestReceipt{id(held)}"

    def hold(request):
        held.append(request)
        page.evaluate(
            "({marker, count}) => { window[marker] = count; }",
            {"marker": marker, "count": len(held)},
        )

    def wait_received(count=1):
        page.wait_for_function(
            "({marker, count}) => window[marker] >= count",
            arg={"marker": marker, "count": count},
            polling=20,
        )

    page.route(pattern, hold)
    return held, wait_received


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("width", [390, 1440])
def test_startup_waits_for_profile_and_visible_data(page, dashboard, language, width):
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    profiles, wait_profiles = hold_startup_requests(page, "**/api/server-configs")
    backups, wait_backups = hold_startup_requests(page, "**/api/backups")
    page.goto(dashboard["url"])
    loader = page.locator("#startupLoader")
    expect(loader).to_be_visible()
    expect(loader).to_contain_text(
        "Loading server data…" if language == "en" else "Загружаем данные сервера…"
    )
    expect(page.locator("#dashboardShell")).to_have_attribute("inert", "")
    expect(page.locator("#view-overview")).to_be_hidden()
    expect(page.locator("#btnStop")).to_be_disabled()
    page.keyboard.press("Control+k")
    expect(page.locator("#commandDialog")).not_to_be_visible()
    page.keyboard.press("Tab")
    assert not page.evaluate("dashboardShell.contains(document.activeElement)")
    assert (
        page.evaluate(
            "getComputedStyle(document.querySelector('.startup-track span')).animationName"
        )
        == "none"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    out = Path(".tmp-startup-loading")
    out.mkdir(exist_ok=True)
    capture_evidence(page, path=str(out / f"startup-{language}-{width}.png"), full_page=True)
    wait_profiles()
    wait_backups()
    assert len(profiles) == len(backups) == 1
    profiles.pop().fallback()
    expect(page.locator("#configProfile option")).to_have_count(1)
    expect(loader).to_be_visible()
    backups.pop().fallback()
    expect(loader).to_be_hidden()
    expect(page.locator("#view-overview")).to_be_visible()
    expect(page.locator("#view-overview")).to_be_focused()
    expect(page.locator("#btnStop")).to_be_enabled()
    assert not page.locator("#dashboardShell").evaluate("el => el.inert")
    capture_evidence(page, path=str(out / f"ready-{language}-{width}.png"), full_page=True)
    page.locator('.nav [data-route="settings"]').click()
    expect(page.locator("#view-settings")).to_be_visible()
    expect(loader).to_be_hidden()


def test_startup_slow_request_offers_recovery_and_failure_releases_shell(page, dashboard):
    page.clock.install()
    page.emulate_media(reduced_motion="reduce")
    profiles, wait_profiles = hold_startup_requests(page, "**/api/server-configs")
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator("#startupLoader")).to_be_visible()
    wait_profiles()
    page.clock.run_for(15001)
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect(page.locator("#configStartup")).to_be_visible()
    expect(page.locator("#configStartupReload")).to_be_enabled()
    expect(page.locator("#configProfile")).to_be_disabled()
    assert page.evaluate("ConfigEditor.initialPending")
    assert len(profiles) == 1
    profiles.pop().fulfill(status=503, json={"ok": False, "error": "Profile unavailable"})
    expect(page.locator("#configStartup")).to_contain_text("Profile unavailable")
    assert not page.evaluate("ConfigEditor.initialPending")
    expect(page.locator("#view-settings")).to_be_visible()


@pytest.mark.parametrize("route", ["settings", "mods"])
def test_pending_real_snapshot_releases_shell_without_unlocking_or_restarting_editor(
    page,
    dashboard,
    editing,  # noqa: F811
    route,
):
    page.clock.install()
    page.emulate_media(reduced_motion="reduce")
    width = 390 if route == "settings" else 1440
    page.set_viewport_size({"width": width, "height": 900})
    metadata, wait_metadata = hold_startup_requests(page, "**/api/mods?*")
    page.goto(dashboard["url"] + f"/#/{route}")
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Сервер")
    wait_metadata()
    expect(page.locator("#startupLoader")).to_be_visible()
    # The destination can change while the initial snapshot is still pending.
    destination = "mods" if route == "settings" else "settings"
    page.evaluate("route => { location.hash = '#/' + route; }", destination)
    page.clock.run_for(15001)
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect(page.locator(f"#view-{destination}")).to_be_visible()
    expect(page.locator("#configStartup")).to_be_visible()
    expect(page.locator("#configStartupReload")).to_be_enabled()
    expect(page.locator("#configProfile")).to_be_disabled()
    assert page.locator(f"#view-{destination} .editor-card").evaluate("el => el.inert")
    reload_button = page.locator("#configStartupReload")
    assert not reload_button.evaluate("el => el.closest('[inert]') !== null")
    assert not reload_button.evaluate("el => el.closest('[aria-busy=true]') !== null")
    page.locator(f"#view-{destination}").focus()
    page.keyboard.press("Shift+Tab")
    expect(reload_button).to_be_focused()
    page.clock.run_for(1000)
    assert len(metadata) == 1
    assert page.evaluate("ConfigEditor.initialPending")
    if destination == "mods":
        settings_link = page.locator("#view-mods .page-heading a")
        expect(settings_link).to_have_attribute("aria-disabled", "true")
        settings_link.dispatch_event("click")
    out = Path(".tmp-startup-loading")
    out.mkdir(exist_ok=True)
    capture_evidence(page, path=str(out / f"pending-{destination}-{width}.png"), full_page=True)
    page.locator('.nav [data-route="overview"]').click()
    expect(page.locator("#view-overview")).to_be_focused()
    expect(page.locator("#configStartup")).to_be_hidden()
    page.unroute("**/api/mods?*")
    metadata.pop().fallback()
    page.wait_for_function("!ConfigEditor.initialPending")
    expect(page.locator("#configStartup")).to_be_hidden()
    expect(page.locator("#configProfile")).to_be_enabled()
    expect(page.locator("#view-overview")).to_be_focused()
    expect(page.locator("#startupLoader")).to_be_hidden()
    assert dashboard["actions"] == []


@pytest.mark.parametrize("route", ["settings", "mods"])
def test_pending_profile_recovery_can_reload_with_the_keyboard(page, dashboard, route):
    page.clock.install()
    page.emulate_media(reduced_motion="reduce")
    profiles, wait_profiles = hold_startup_requests(page, "**/api/server-configs")
    page.goto(dashboard["url"] + f"/#/{route}")
    wait_profiles()
    page.clock.run_for(15001)
    expect(page.locator("#startupLoader")).to_be_hidden()
    reload_button = page.locator("#configStartupReload")
    assert not reload_button.evaluate("el => el.closest('[inert]') !== null")
    assert not reload_button.evaluate("el => el.closest('[aria-busy=true]') !== null")
    page.locator(f"#view-{route}").focus()
    page.keyboard.press("Shift+Tab")
    expect(reload_button).to_be_focused()
    with page.expect_request("**/api/server-configs"):
        reload_button.press("Enter")
    expect(page.locator("#startupLoader")).to_be_visible()
    wait_profiles(2)
    assert len(profiles) == 2
    profiles.pop().fallback()
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect(page.locator(f"#view-{route}")).to_be_visible()
    assert dashboard["actions"] == []


def test_initial_metadata_failure_preserves_the_loaded_profile_and_draft(
    page,
    dashboard,
    editing,  # noqa: F811
):
    metadata = []

    def fail_metadata(request):
        metadata.append(request)
        request.fulfill(status=503, json={"ok": False, "error": "Metadata unavailable"})

    page.route("**/api/mods?*", fail_metadata)
    page.goto(dashboard["url"] + "/#/mods")
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Сервер")
    expect(page.locator("#startupLoader")).to_be_hidden()
    assert len(metadata) == 1
    expect(page.locator("#configProfile")).to_have_value("world.ini")
    expect(page.locator("#configProfile option[value='world.ini']")).to_have_text("world.ini")
    expect(page.locator("#configError")).to_contain_text("Metadata unavailable")
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Сервер")
    expect(page.locator('[data-key="PublicName"]')).to_be_enabled()
    expect(page.locator("#configStartup")).to_be_hidden()
    assert not page.evaluate("ConfigEditor.initialPending")
    assert (
        page.evaluate(
            "ConfigEditor.focusSettings('server').then(() => 'ok', failure => failure.message)"
        )
        == "ok"
    )
    assert page.evaluate("ConfigEditor.focusModOrder().then(() => false, () => true)")
    page.unroute("**/api/mods?*")
    page.locator("#modRescan").click()
    expect(page.locator("#modPackages summary")).to_be_visible()
    assert (
        page.evaluate("ConfigEditor.focusModOrder().then(() => 'ok', failure => failure.message)")
        == "ok"
    )
    assert dashboard["actions"] == []


def test_first_paint_reserves_copy_until_the_saved_language_is_available(page, dashboard):
    page.add_init_script("localStorage.setItem('pz-language', 'en')")
    catalog, wait_catalog = hold_startup_requests(page, "**/static/catalogs.js")
    page.goto(dashboard["url"], wait_until="commit")
    wait_catalog()
    expect(page.locator(".startup-track")).to_be_visible()
    expect(page.locator(".startup-title")).to_be_hidden()
    expect(page.locator(".startup-label")).to_be_hidden()
    assert len(catalog) == 1
    catalog.pop().fallback()
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(page.locator(".startup-title")).to_have_text("PZ Console")


@pytest.mark.parametrize("font_failure", [False, True])
def test_delayed_or_failed_fonts_do_not_hold_the_startup_screen(page, dashboard, font_failure):
    language, width, height = ("ru", 1440, 420) if font_failure else ("en", 320, 640)
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    page.set_viewport_size({"width": width, "height": height})
    page.emulate_media(reduced_motion="reduce")
    if font_failure:
        page.route("**/*.woff2", lambda request: request.abort())
    else:
        fonts, wait_fonts = hold_startup_requests(page, "**/*.woff2")
    page.goto(dashboard["url"], wait_until="domcontentloaded")
    if not font_failure:
        wait_fonts()
    expect(page.locator("#startupLoader")).to_be_hidden(timeout=2500)
    expect(page.locator("#view-overview")).to_be_visible()
    expect(page.locator("#view-overview")).to_be_focused()
    assert not page.locator("#dashboardShell").evaluate("el => el.inert")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if not font_failure:
        assert fonts
        for request in fonts:
            request.fallback()
    out = Path(".tmp-startup-loading")
    out.mkdir(exist_ok=True)
    capture_evidence(page, path=str(out / f"font-{language}-{width}.png"), full_page=True)


def test_startup_transition_keeps_the_revealed_shell_inert_until_exit_finishes(
    page,
    dashboard,
    editing,  # noqa: F811
):
    page.emulate_media(reduced_motion="no-preference")
    page.add_init_script("""window.startupExitStates = [];
      new MutationObserver(records => {
        for (const {target} of records) {
          if (target.id === 'startupLoader' && target.dataset.state === 'leaving') {
            startupExitStates.push({
              inert: document.getElementById('dashboardShell').inert,
              profileDisabled: document.getElementById('configProfile').disabled,
              fieldsDisabled: [...document.querySelectorAll('#configFields [data-key]')]
                .every(field => field.disabled),
              editorBusy: document.getElementById('view-settings').getAttribute('aria-busy'),
              opening: document.body.classList.contains('is-opening'),
              booting: document.body.classList.contains('is-booting')
            });
          }
        }
      }).observe(document, {subtree:true, attributes:true, attributeFilter:['data-state']});""")
    page.goto(dashboard["url"])
    expect(page.locator("#startupLoader")).to_be_hidden()
    assert page.evaluate("startupExitStates") == [
        {
            "inert": True,
            "profileDisabled": True,
            "fieldsDisabled": True,
            "editorBusy": "true",
            "opening": True,
            "booting": True,
        }
    ]
    assert not page.locator("#dashboardShell").evaluate("el => el.inert")
    expect(page.locator('[data-key="PublicName"]')).to_be_enabled()
    expect(page.locator("#view-settings")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#view-overview")).to_be_focused()


@pytest.mark.parametrize("motion", ["reduce", "no-preference"])
def test_startup_with_blocked_fonts_does_not_depend_on_animation_frames(page, dashboard, motion):
    page.clock.install(time="2026-01-01T00:00:00Z")
    page.clock.pause_at("2026-01-01T00:00:01Z")
    page.emulate_media(reduced_motion=motion)
    # Background tabs may suspend frames while the bounded font wait still expires.
    page.add_init_script("window.requestAnimationFrame = () => 0")
    fonts, wait_fonts = hold_startup_requests(page, "**/*.woff2")
    page.goto(dashboard["url"], wait_until="domcontentloaded")
    wait_fonts()
    page.wait_for_function("document.body.classList.contains('is-opening')", polling=20)
    expect(page.locator("#startupLoader")).to_be_visible()
    page.clock.run_for(301)
    expect(page.locator("#startupLoader")).to_be_hidden(timeout=2500)
    assert not page.locator("#dashboardShell").evaluate("el => el.inert")
    assert page.evaluate("Date.now()") == 1767225601301
    assert fonts
    for request in fonts:
        request.fallback()


@pytest.mark.parametrize(
    "route,endpoint,target,retry",
    [
        ("players", "players", "#playersBody", "players"),
        ("backups", "backups", "#backupsBody", "backups"),
        ("events", "events?limit=200", "#eventsBody", "events"),
        ("overview", "events?limit=200", "#recentBody", "events"),
        ("overview", "stats", "#ramSub", "stats"),
    ],
)
def test_first_request_failure_offers_retry_and_later_failure_keeps_recovered_data(
    page, dashboard, route, endpoint, target, retry
):
    # Exercise the supported polling fallback without real SSE frames replacing errors.
    page.add_init_script("window.EventSource = undefined")
    pattern = "**/api/" + endpoint
    page.route(pattern, lambda request: request.abort())
    page.goto(dashboard["url"] + f"/#/{route}")
    expect(page.locator("#startupLoader")).to_be_hidden()
    content = page.locator(target)
    expect(content).to_have_attribute("data-state", "error")
    expect(content).not_to_have_text("")
    retry_button = content.locator(f'[data-load-retry="{retry}"]')
    expect(retry_button).to_be_enabled()
    page.unroute(pattern)
    retry_button.click()
    expect(content).not_to_have_attribute("data-state", "error")
    recovered_text = content.inner_text()
    recovered_state = content.get_attribute("data-state")
    page.route(pattern, lambda request: request.abort())
    with page.expect_event(
        "requestfailed", predicate=lambda request: request.url.endswith("/api/" + endpoint)
    ):
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(content).to_have_text(recovered_text)
    assert content.get_attribute("data-state") == recovered_state
    assert dashboard["actions"] == []


def test_demo_startup_uses_no_live_api_and_reveals_loaded_preview(page, dashboard):
    requests = []

    def reject_live_api(route):
        requests.append(route.request.url)
        route.fulfill(status=401, json={"ok": False})

    page.route("**/api/**", reject_live_api)
    page.goto(dashboard["url"] + "/?demo=1")
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect(page.locator("#demoBadge")).to_be_visible()
    expect(page.locator("#view-overview")).to_be_visible()
    expect(page.locator("#kpiOnline")).to_have_text("3")
    expect(page.locator("#btnStop")).to_be_disabled()
    assert requests == []
