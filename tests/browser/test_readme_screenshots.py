"""Opt-in README captures using real UI/editor code and fictional local data."""

import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import ops
import workshop
from playwright.sync_api import expect
from test_editors import editing, env  # noqa: F401

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(
        os.getenv("PZ_README_SCREENSHOTS") != "1",
        reason="Opt-in screenshots: PZ_README_SCREENSHOTS=1",
    ),
]


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "locale": "en-US", "timezone_id": "UTC"}


def test_capture_readme(page, dashboard, editing, monkeypatch):  # noqa: F811
    data, context = editing
    context["version"] = "42.21.0"
    (data / "Server" / "world.ini").write_text(
        "PublicName=Riverside Co-op\nPublicDescription=A fictional community server\n"
        "MaxPlayers=16\nPVP=false\nPauseEmpty=true\nPassword=\n"
        "Mods=\\riverside-library;\\riverside-vehicles;\\riverside-buildings\n"
        "WorkshopItems=111;222;333\nMap=Muldraugh, KY\n",
        encoding="utf-8",
    )
    titles = {"111": "Riverside Library", "222": "Co-op Vehicles", "333": "Build Together"}
    monkeypatch.setattr(ops, "_ws_titles", lambda ids: titles)
    # Replace only the two sample directories created by the isolated editor fixture.
    sample_root = data / "steamapps/workshop/content/108600/111/mods"
    for folder in ["DifferentFolder", "PluginFolder"]:
        shutil.rmtree(sample_root / folder)
    for item, mid, title, dependency in [
        ("111", "riverside-library", "Riverside Library", ""),
        ("222", "riverside-vehicles", "Co-op Vehicles", "\\riverside-library"),
        ("333", "riverside-buildings", "Build Together", "\\riverside-library"),
    ]:
        directory = data / f"steamapps/workshop/content/108600/{item}/mods/{mid}/42"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "mod.info").write_text(
            f"id={mid}\nname={title}\n" + (f"require={dependency}\n" if dependency else ""),
            encoding="utf-8",
        )
    workshop.invalidate()
    now = "2026-10-06T18:00:00Z"
    overview = {
        **dashboard["overview"],
        "serverName": "Riverside Co-op",
        "container": "pzserver",
        "image": "indifferentbroccoli/projectzomboid-server-docker:latest",
        "containerInfo": {"running": True, "status": "running", "uptimeSec": 286200},
        "update": {"at": now, "available": False, "error": None},
        "modsCheck": {"at": now, "state": "up-to-date", "items": [], "error": None},
        "settings": {
            "autoUpdate": {"enabled": True, "intervalHours": 6, "warnSeconds": 300},
            "modsUpdate": {"enabled": True, "intervalHours": 6, "warnSeconds": 600},
            "backup": {"maxBackups": 7, "stopServer": False},
            "watchdog": {"enabled": True, "thresholdMin": 5, "gracePeriodMin": 5},
        },
        "backupsCount": 3,
    }
    stats = {
        "ok": True,
        "cpuPct": 18.4,
        "memUsed": 3.2 * 1024**3,
        "memLimit": 8 * 1024**3,
        "memPct": 40,
        "netIn": 2.4e9,
        "netOut": 1.1e9,
        "pids": 42,
    }
    players = {"ok": True, "names": ["Maple", "Sledge", "RiverScout", "Ash"], "count": 4}
    events = {
        "ok": True,
        "items": [
            {"ts": now, "type": "update-check", "text": "Image check complete: up to date"},
            {
                "ts": "2026-10-06T03:00:00Z",
                "type": "backup",
                "text": "Scheduled world backup created",
            },
            {
                "ts": "2026-10-05T19:30:00Z",
                "type": "restart",
                "text": "Configuration applied; server ready",
            },
        ],
    }
    backups = {
        "ok": True,
        "maxBackups": 7,
        "autoBackup": {
            "enabled": True,
            "time": "03:00",
            "stopServer": False,
            "nextRun": "2026-10-07T03:00:00Z",
        },
        "items": [
            {
                "name": f"riverside-2026100{day}-030000.tar.gz",
                "size": (610 + day * 8) * 1024**2,
                "mtime": f"2026-10-0{day}T03:00:00Z",
            }
            for day in [6, 5, 4]
        ],
        "journal": [
            {
                "ts": f"2026-10-0{day}T03:00:00Z",
                "trigger": "scheduled",
                "name": f"riverside-2026100{day}-030000.tar.gz",
                "status": "success",
                "size": (610 + day * 8) * 1024**2,
                "duration": 38.5,
            }
            for day in [6, 5, 4]
        ],
    }
    history = {
        "ok": True,
        "points": [
            {
                "ts": (
                    datetime(2026, 10, 5, 18, tzinfo=timezone.utc) + timedelta(minutes=i * 15)
                ).isoformat(),
                "count": [2, 1, 0, 0, 1, 2, 4, 5, 7, 6, 5, 4][min(i // 8, 11)],
            }
            for i in range(97)
        ],
    }
    stats_history = {
        "ok": True,
        "points": [
            {"ts": f"2026-10-06T17:{minute:02}:00Z", "cpu": cpu, "mem": 39 + minute / 60}
            for minute, cpu in zip(
                range(0, 60, 5), [12, 14, 19, 16, 13, 24, 20, 17, 15, 22, 19, 18]
            )
        ],
    }
    responses = {
        "overview": overview,
        "players": players,
        "players/history": history,
        "stats": stats,
        "stats/history": stats_history,
        "stats-history": stats_history,
        "players-history": history,
        "backups": backups,
        "backups/journal": {"ok": True, "items": backups["journal"], "hasMore": False},
        "events": events,
        "ops": {"ok": True, "active": None, "history": []},
    }

    def synthetic_api(route):
        path = urlsplit(route.request.url).path.removeprefix("/api/")
        if path == "stream":
            route.fulfill(
                content_type="text/event-stream",
                body="".join(
                    f"event: {channel}\ndata: {json.dumps(responses[channel])}\n\n"
                    for channel in [
                        "overview",
                        "players",
                        "stats",
                        "backups",
                        "events",
                        "ops",
                        "stats-history",
                        "players-history",
                    ]
                ),
            )
        elif path in responses:
            route.fulfill(json=responses[path])
        else:
            route.fallback()

    page.route("**/api/**", synthetic_api)
    page.add_init_script("localStorage.setItem('pz-language', 'en')")
    page.clock.install(time=datetime(2026, 10, 6, 18, tzinfo=timezone.utc))
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(dashboard["url"] + "/#/settings")
    expect(page.locator('[data-key="PublicName"]')).to_have_value("Riverside Co-op")
    expect(page.locator("#cpuSparkLine")).not_to_have_attribute("points", "")
    page.evaluate("document.fonts.ready")
    page.add_style_tag(content="* {animation:none!important;transition:none!important}")
    out = Path(__file__).resolve().parents[2] / "docs" / "screenshots"
    out.mkdir(parents=True, exist_ok=True)
    for route in ["overview", "settings", "mods", "backups", "players"]:
        page.evaluate("route => location.hash = '#/' + route", route)
        expect(page.locator(f"#view-{route}")).to_be_visible()
        if route == "mods":
            expect(page.locator("#view-mods")).to_contain_text("Co-op Vehicles")
        page.wait_for_timeout(250)
        page.evaluate("window.scrollTo(0,0)")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not re.search("[А-Яа-яЁё]", page.locator(f"#view-{route}").inner_text())
        page.screenshot(path=str(out / f"{route}.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("location.hash='#/overview'")
    expect(page.locator("#view-overview")).to_be_visible()
    page.wait_for_timeout(250)
    page.evaluate("window.scrollTo(0,0)")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(out / "mobile.png"))
    assert dashboard["actions"] == []
