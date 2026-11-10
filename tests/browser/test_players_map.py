"""Player map interactions use fictional local geometry and observations."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


GEOMETRY = {
    "ok": True,
    "state": "ready",
    "profile": "world",
    "revision": "fictional-fixture-1",
    "bounds": {"minX": 0, "minY": 0, "maxX": 1000, "maxY": 800},
    "features": [
        {
            "kind": "forest",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [1000, 0], [1000, 800], [0, 800], [0, 0]]],
            },
        },
        {
            "kind": "water",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [1000, 0], [1000, 130], [0, 210], [0, 0]]],
            },
        },
        {
            "kind": "road",
            "geometry": {"type": "LineString", "coordinates": [[100, 400], [900, 400]]},
        },
    ],
    "missingMaps": [],
    "truncated": False,
}


def start_map(page, dashboard, language="en", width=1440):
    page.set_viewport_size({"width": width, "height": 1000 if width > 740 else 844})
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    dashboard["players"].update(
        names=["Maple", "RiverScout", "No coordinates"],
        count=3,
        map={
            "state": "fresh",
            "profile": "world",
            "capturedAt": 1794340800000,
            "ageMs": 0,
            "maxAgeMs": 15000,
            "source": "PZConsoleMap",
            "sessionId": "fictional-session",
            "positions": [
                {"name": "Maple", "x": 300, "y": 350, "z": 0},
                {"name": "RiverScout", "x": 700, "y": 550, "z": 1},
            ],
            "missing": ["No coordinates"],
            "truncated": False,
        },
    )
    page.route("**/api/players/map/geometry", lambda route: route.fulfill(json=GEOMETRY))
    page.goto(dashboard["url"] + "/#/players")
    expect(page.locator("#startupLoader")).to_be_hidden()
    page.evaluate(
        "liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer);renderPlayers(structuredClone(S.players))"
    )
    expect(page.locator(".player-map-marker")).to_have_count(2)
    expect(page.locator("#playerMapGeometryStatus")).to_be_hidden()


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("width", [320, 390, 820, 1440])
def test_map_selection_search_responsive_and_sanctions(page, dashboard, language, width):
    start_map(page, dashboard, language, width)
    expect(page.locator("#playerMapCoverage")).to_have_text(
        "Located: 2 of 3" if language == "en" else "На карте: 2 из 3"
    )
    page.locator('[data-locate="Maple"]').click()
    expect(page.locator("#playerMapSelectedName")).to_have_text("Maple")
    expect(page.locator("#playerMapSelectedPosition")).to_contain_text("X 300")
    expect(page.locator('[data-row-key="Maple"]')).to_have_class("player-row is-map-selected")
    expect(page.locator('[data-locate="No coordinates"]')).to_be_disabled()
    page.locator("#playerMapFit").click()
    page.locator("#playerMapViewport").focus()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("+")
    page.keyboard.press("Home")
    expect(page.locator("#playerMapViewport")).to_be_focused()
    page.locator("#playerQuery").fill("river")
    expect(page.locator("#playersBody .player-row")).to_have_count(1)
    expect(page.locator(".player-map-marker")).to_have_count(1)
    expect(page.locator("#playersCount")).to_have_text("3")
    page.locator("#playerQuery").fill("absent")
    expect(page.locator("#playersBody")).to_contain_text(
        "No matching players." if language == "en" else "Игроки не найдены."
    )
    page.locator("#playerQuery").fill("")
    expect(page.locator("#playersBody .player-row")).to_have_count(3)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    kick = page.locator('[data-p="kick"]').first
    kick.click()
    expect(page.get_by_role("alertdialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(kick).to_be_focused()
    if width in (390, 1440):
        target = Path("test-results/player-map-review")
        target.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(target / f"players-{language}-{width}.png"), full_page=True)


def test_map_expires_without_new_payload_and_rejects_offline_current_status(page, dashboard):
    start_map(page, dashboard)
    page.evaluate("liveSource?.close(); liveSource=null; clearTimeout(sseStartupTimer)")
    page.clock.install()
    page.evaluate("renderPlayers(structuredClone(S.players))")
    expect(page.locator("#playerMapViewport")).to_have_attribute("data-state", "fresh")
    page.clock.run_for(16001)
    expect(page.locator("#playerMapViewport")).to_have_attribute("data-state", "stale")
    expect(page.locator("#playerMapStatus")).to_contain_text("Positions are stale")
    page.evaluate("applyPlayers({ok:false,error:'offline'})")
    expect(page.locator("#playerMapStatus")).to_contain_text("Connection lost")
    page.locator("#playerQuery").fill("Maple")
    expect(page.locator("#playerMapStatus")).to_contain_text("Connection lost")


def test_map_preserves_nodes_camera_and_safe_names_on_identical_frame(page, dashboard):
    start_map(page, dashboard)
    page.evaluate("""() => {
      liveSource?.close();liveSource=null;clearTimeout(sseStartupTimer);
      window.playerMarker=document.querySelector('.player-map-marker');
      window.kickButton=document.querySelector('[data-p=kick]');
      kickButton.focus();
      window.rosterChanges=0;
      new MutationObserver(r=>rosterChanges+=r.length).observe(document.getElementById('playersBody'),{subtree:true,attributes:true,childList:true,characterData:true});
      renderPlayers(S.players);
    }""")
    assert page.evaluate("document.activeElement===kickButton && rosterChanges===0")
    assert page.evaluate("document.querySelector('.player-map-marker')===playerMarker")
    page.evaluate("""() => {
      const frame=structuredClone(S.players), name='<img src=x onerror=alert(1)>';
      frame.names=[name];frame.count=1;
      frame.map.positions=[{name,x:400,y:400,z:0}];renderPlayers(frame);
    }""")
    expect(page.locator(".p-name")).to_have_text("<img src=x onerror=alert(1)>")
    expect(page.locator("#playersBody img")).to_have_count(0)


def test_map_old_payload_and_geometry_setup_keep_roster_usable(page, dashboard):
    page.route(
        "**/api/players/map/geometry",
        lambda route: route.fulfill(
            json={"ok": False, "state": "setup", "reason": "cell-size-required"}
        ),
    )
    dashboard["players"].update(names=["Old client fixture"], count=1)
    page.goto(dashboard["url"] + "/#/players")
    expect(page.locator("#startupLoader")).to_be_hidden()
    expect(page.locator(".p-name")).to_have_text("Old client fixture")
    expect(page.locator("#playerMapGeometryStatus")).to_contain_text("PZ_MAP_CELL_SIZE")
    expect(page.locator("#playerMapSetup")).to_be_visible()
    expect(page.locator("[data-locate]")).to_be_disabled()
    expect(page.locator("[data-p=kick]")).to_be_enabled()


def test_map_zero_online_clears_markers_and_new_profile_rejects_old_geometry(page, dashboard):
    start_map(page, dashboard)
    page.evaluate("""() => {
      const frame=structuredClone(S.players); frame.map.profile='another-world';
      renderPlayers(frame);
    }""")
    expect(page.locator("#playerMapGeometryStatus")).to_contain_text("active server profile")
    page.evaluate("""() => {
      const frame=structuredClone(S.players); frame.names=[];frame.count=0;
      frame.map.positions=[];frame.map.state='empty';renderPlayers(frame);
    }""")
    expect(page.locator(".player-map-marker")).to_have_count(0)
    expect(page.locator("#playerMapStatus")).to_have_text("No players online.")
    expect(page.locator("#playersCount")).to_have_text("0")


@pytest.mark.parametrize("language", ["en", "ru"])
def test_map_explains_unverified_overlapping_sources(page, dashboard, language):
    start_map(page, dashboard, language)
    overlapping = {**GEOMETRY, "state": "partial", "reason": "map-overlap-unverified"}
    page.route("**/api/players/map/geometry", lambda route: route.fulfill(json=overlapping))
    page.locator("#playerMapRefresh").click()
    expect(page.locator("#playerMapGeometryStatus")).to_have_text(
        "Mod map layering is unverified. Geometry may overlap."
        if language == "en"
        else "Наложение мод-карт не проверено. Геометрия может перекрываться."
    )
    expect(page.locator(".player-map-marker")).to_have_count(2)
