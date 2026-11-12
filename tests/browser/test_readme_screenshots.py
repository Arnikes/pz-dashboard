"""Opt-in README captures using real UI/editor code and fictional local data."""

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

NOW = datetime(2026, 11, 10, 18, tzinfo=timezone.utc)
BACKUP_TIME = NOW.replace(hour=3)
S3_ARCHIVE = f"pz-backup-{BACKUP_TIME:%Y%m%d-%H%M%S}-5b21de4a6d92440f82bb0da37a9bcefe.tar.gz"


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "locale": "en-US",
        "timezone_id": "UTC",
        "reduced_motion": "reduce",
    }


def player_map_geometry():
    river = [[0, 100], [230, 60], [480, 110], [730, 75], [1100, 130]]
    shoreline = [[x, y + 85] for x, y in reversed(river)]
    roads = [
        [[80, 420], [370, 350], [730, 350], [1030, 435]],
        [[370, 200], [370, 350], [435, 640]],
        [[730, 210], [730, 350], [810, 625]],
        [[435, 640], [615, 550], [810, 625]],
    ]
    buildings = [
        (260, 305, 52, 39),
        (305, 396, 58, 42),
        (435, 285, 45, 41),
        (511, 296, 54, 32),
        (455, 417, 82, 48),
        (614, 391, 66, 55),
        (792, 287, 56, 43),
        (865, 315, 61, 35),
        (608, 590, 73, 45),
    ]
    return {
        "ok": True,
        "state": "ready",
        "profile": "world",
        "bounds": {"minX": 0, "minY": 0, "maxX": 1100, "maxY": 700},
        "features": [
            {
                "kind": "forest",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [1100, 0], [1100, 700], [0, 700], [0, 0]]],
                },
            },
            {
                "kind": "water",
                "geometry": {"type": "Polygon", "coordinates": [river + shoreline + river[:1]]},
            },
            *(
                {"kind": "road", "geometry": {"type": "LineString", "coordinates": road}}
                for road in roads
            ),
            *(
                {
                    "kind": "building",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]]],
                    },
                }
                for x, y, w, h in buildings
            ),
        ],
    }


def capture_view(page, route, *, mobile=False):
    page.evaluate("route => location.hash = '#/' + route", route)
    view = page.locator(f"#view-{route}")
    expect(view).to_be_visible()
    if route == "overview":
        expect(page.locator("#kpiOnline")).to_have_text("3")
        expect(page.locator("#cpuSparkLine")).not_to_have_attribute("points", "")
        if mobile:
            expect(page.locator("#navMore")).to_be_visible()
        else:
            expect(page.locator("#btnCustomizeOverview")).to_be_visible()
    elif route == "settings":
        expect(page.locator('[data-key="PublicName"]')).to_have_value("Riverside Co-op")
    elif route == "mods":
        page.locator('#modPackages [data-item="222"] summary').click()
        expect(page.locator('[data-modid="riverside-vehicles"]')).to_be_checked()
        expect(view).to_contain_text("Requires: riverside-library")
        page.mouse.move(0, 0)
    elif route == "players":
        expect(page.locator("#playerMapViewport")).to_have_attribute("data-state", "fresh")
        expect(page.locator(".player-map-marker:not([hidden])")).to_have_count(3)
        expect(page.locator("#playerMapGeometryStatus")).to_be_hidden()
        expect(page.locator("#phEmpty")).to_be_hidden()
        assert page.locator("#playersBody").evaluate(
            "list => list.scrollHeight <= list.clientHeight"
        )
    elif route == "backups":
        expect(page.locator('#backupsBody [data-verified="true"]')).to_have_count(1)
        for selector in ("#backupsBody .b-name", "#bkJournalBody .j-name"):
            name = page.locator(selector).filter(has_text=S3_ARCHIVE)
            expect(name).to_be_visible()
            assert name.evaluate("name => name.scrollWidth <= name.clientWidth")
    elif route == "maintenance":
        expect(page.locator("#chatAnnouncementMessage")).to_have_value(
            "Welcome to Riverside Co-op!"
        )
        expect(page.locator("#scheduledRestartEnabled")).to_be_checked()
        expect(page.locator("#scheduledRestartStatus")).to_contain_text("Next run:")
        expect(page.locator("#chatAnnouncementsStatus")).to_contain_text("Next run:")

    page.evaluate("document.fonts.ready")
    if not mobile:
        # Full-page screenshots put fixed UI in the middle of long pages.
        page.set_viewport_size(
            {"width": 1440, "height": page.evaluate("document.documentElement.scrollHeight")}
        )
    page.evaluate("new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))")
    page.evaluate("window.scrollTo(0, 0)")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), route
    # inner_text includes the language picker's unselected Russian option.
    localized_text = "\n".join(line for line in view.inner_text().splitlines() if line != "Русский")
    assert not re.search("[А-Яа-яЁё]", localized_text), route
    if not mobile:
        assert page.evaluate("document.documentElement.scrollHeight <= innerHeight"), route
        content = view.bounding_box()
        footer = page.locator(".site-footer").bounding_box()
        assert content["y"] + content["height"] <= footer["y"], route
    return page.screenshot(animations="disabled")


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
    now = NOW.isoformat()
    server_digest = "sha256:" + "a1b2c3d4" * 8
    console_digest = "sha256:" + "d4c3b2a1" * 8
    overview = {
        **dashboard["overview"],
        "serverName": "Riverside Co-op",
        "container": "pzserver",
        "image": "indifferentbroccoli/projectzomboid-server-docker:latest",
        "containerInfo": {"running": True, "status": "running", "uptimeSec": 286200},
        "update": {"at": now, "local": server_digest, "remote": server_digest, "available": False},
        "dashboardUpdate": {
            "supported": True,
            "image": "ghcr.io/example/pz-console:latest",
            "local": console_digest,
            "remote": console_digest,
            "available": False,
        },
        "modsCheck": {"at": now, "state": "up-to-date", "items": [], "error": None},
        "settings": {
            "autoUpdate": {"enabled": True, "intervalHours": 6, "warnSeconds": 300},
            "modsUpdate": {"enabled": True, "intervalHours": 6, "warnSeconds": 600},
            "backup": {"maxBackups": 7, "stopServer": False},
            "watchdog": {"enabled": True, "thresholdMin": 5, "gracePeriodMin": 5},
            "chatAnnouncements": {
                "enabled": True,
                "message": "Welcome to Riverside Co-op!",
                "intervalMinutes": 30,
            },
            "scheduledRestart": {"enabled": True, "intervalHours": 24},
            "nextChatAnnouncement": (NOW + timedelta(minutes=30)).timestamp(),
            "nextScheduledRestart": (NOW + timedelta(hours=10)).timestamp(),
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
    positions = [
        {"name": name, "x": x, "y": y, "z": z}
        for name, x, y, z in [
            ("Maple", 323, 410, 0),
            ("RiverScout", 900, 358, 0),
            ("Ash", 619, 597, 1),
        ]
    ]
    players = {
        "ok": True,
        "names": [position["name"] for position in positions],
        "count": len(positions),
        "map": {
            "state": "fresh",
            "profile": "world",
            "capturedAt": int(NOW.timestamp() * 1000),
            "ageMs": 0,
            "maxAgeMs": 15000,
            "sessionId": "riverside-fixture",
            "positions": positions,
        },
    }
    events = {
        "ok": True,
        "items": [
            {"ts": now, "type": "update-check", "text": "Image check complete: up to date"},
            {
                "ts": BACKUP_TIME.isoformat(),
                "type": "backup",
                "text": "Scheduled world backup created",
            },
            {
                "ts": (NOW - timedelta(hours=22, minutes=30)).isoformat(),
                "type": "restart",
                "text": "Configuration applied; server ready",
            },
        ],
    }
    archives = []
    for days_ago in range(3):
        created_at = BACKUP_TIME - timedelta(days=days_ago)
        storage = "local" if days_ago else "s3"
        archives.append(
            {
                "name": S3_ARCHIVE
                if storage == "s3"
                else f"pz-backup-{created_at:%Y%m%d-%H%M%S}.tar.gz",
                "size": (690 - days_ago * 8) * 1024**2,
                "mtime": created_at.isoformat(),
                "storage": storage,
            }
        )
    backups = {
        "ok": True,
        "maxBackups": 7,
        "autoBackup": {
            "enabled": True,
            "time": "03:00",
            "stopServer": False,
            "nextRun": (BACKUP_TIME + timedelta(days=1)).isoformat(),
        },
        "items": [
            {
                **archives[0],
                "verification": {
                    "verifiedAt": (BACKUP_TIME + timedelta(minutes=1)).isoformat(),
                    "files": 3271,
                    "hasServerIni": True,
                    "hasMapData": True,
                },
            },
            *archives[1:],
        ],
        "journal": [
            {
                "ts": archive["mtime"],
                "trigger": "scheduled",
                "name": archive["name"],
                "status": "success",
                "size": archive["size"],
                "duration": 38.5,
            }
            for archive in archives
        ],
    }
    history = {
        "ok": True,
        "points": [
            {
                "ts": (NOW - timedelta(minutes=(96 - i) * 15)).isoformat(),
                "count": [2, 1, 0, 0, 1, 2, 4, 5, 7, 6, 5, 3][min(i // 8, 11)],
            }
            for i in range(97)
        ],
    }
    stats_history = {
        "ok": True,
        "points": [
            {
                "ts": (NOW - timedelta(minutes=60 - minute)).isoformat(),
                "cpu": cpu,
                "mem": 39 + minute / 60,
            }
            for minute, cpu in zip(
                range(0, 60, 5), [12, 14, 19, 16, 13, 24, 20, 17, 15, 22, 19, 18]
            )
        ],
    }
    responses = {
        "overview": overview,
        "players": players,
        "players/history": history,
        "players/map/geometry": player_map_geometry(),
        "stats": stats,
        "stats/history": stats_history,
        "backups": backups,
        "backups/journal": {"ok": True, "items": backups["journal"], "hasMore": False},
        "events": events,
        "ops": {"ok": True, "active": None, "history": []},
    }

    blocked_requests = []

    def local_read_only(route):
        request = route.request
        path = urlsplit(request.url).path.removeprefix("/api/")
        if not request.url.startswith(dashboard["url"] + "/") or request.method != "GET":
            blocked_requests.append((request.method, request.url))
            route.abort()
        elif path in responses:
            route.fulfill(json=responses[path])
        else:
            route.fallback()

    page.route("**/*", local_read_only)
    # Use the real polling fallback so a finite test SSE response cannot make the map stale.
    page.add_init_script(
        "localStorage.setItem('pz-language', 'en'); window.EventSource = undefined;"
    )
    page.clock.set_fixed_time(NOW)
    desktop = {"width": 1440, "height": 900}
    page.set_viewport_size(desktop)
    page.goto(dashboard["url"] + "/#/overview")
    expect(page.locator("#startupLoader")).to_be_hidden()
    captures = {}
    for route in ("overview", "settings", "mods", "players", "backups", "maintenance"):
        page.set_viewport_size(desktop)
        captures[route] = capture_view(page, route)
    page.set_viewport_size({"width": 390, "height": 844})
    captures["mobile"] = capture_view(page, "overview", mobile=True)

    assert not blocked_requests
    assert dashboard["actions"] == []
    out = Path(__file__).resolve().parents[2] / "docs" / "screenshots"
    out.mkdir(parents=True, exist_ok=True)
    for name, pixels in captures.items():
        (out / f"{name}.png").write_bytes(pixels)
