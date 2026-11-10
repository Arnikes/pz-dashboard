"""Real vector/snapshot contracts with fictional, isolated local game files."""

import io
import json
import time
import zipfile
from datetime import datetime, timezone

import pytest

import config
import payloads
import playermap
from test_api import api  # noqa: F401

NOW = 1_800_000_000_000


@pytest.fixture
def map_env(tmp_path, monkeypatch):
    data, server = tmp_path / "data", tmp_path / "server"
    (data / "Server").mkdir(parents=True)
    (data / "Lua").mkdir()
    server.mkdir()
    monkeypatch.setitem(config.CFG, "data_dir", str(data))
    monkeypatch.setitem(config.CFG, "pz_container", "fictional-map-test")
    monkeypatch.setenv("SERVER_FILES_DIR", str(server))
    monkeypatch.delenv("PZ_MAP_CELL_SIZE", raising=False)
    monkeypatch.delenv("WORKSHOP_DIR", raising=False)
    monkeypatch.delenv("PZ_CONFIG_FILE", raising=False)
    monkeypatch.setattr(playermap, "_SNAPSHOTS", {})
    monkeypatch.setattr(playermap, "_GEOMETRY", playermap.OrderedDict())
    (data / "Server/world.ini").write_text("Map=Muldraugh, KY\nMods=\nWorkshopItems=\n")
    context = {
        "activeFile": "world.ini",
        "version": "42.21.0",
        "serverIdentity": "fictional-one",
        "serverStartedAt": datetime.fromtimestamp((NOW - 60_000) / 1000, timezone.utc).isoformat(),
    }
    return data, server, context


def write_snapshot(data, *, slot="a", profile="world", **values):
    frame = {
        "schemaVersion": 1,
        "profile": profile,
        "sessionId": "fictional-session",
        "capturedAt": NOW - 2000,
        "truncated": False,
        "players": [{"name": "Alice", "x": 10500.25, "y": 9200, "z": 1}],
    }
    frame.update(values)
    path = data / "Lua" / f"pz-console-map-{profile.encode().hex()}-{slot}.json"
    path.write_text(json.dumps(frame, ensure_ascii=False), encoding="utf-8")
    return path


def xml_map(path, *, cell_size="300", cell=(2, -1), kind="water", points=None):
    points = points or [(1, 2), (120, 2), (120, 100), (1, 2)]
    attribute = f' cellSize="{cell_size}"' if cell_size is not None else ""
    text = f'<world{attribute}><cell x="{cell[0]}" y="{cell[1]}"><feature><geometry type="Polygon"><coordinates>'
    text += "".join(f'<point x="{x}" y="{y}"/>' for x, y in points)
    text += f'</coordinates></geometry><properties><property name="{kind}" value="yes"/></properties></feature></cell></world>'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_snapshot_joins_exact_membership_and_keeps_unlocated_names(map_env):
    data, _, context = map_env
    write_snapshot(
        data,
        players=[
            {"name": "Alice", "x": 1, "y": 2, "z": 0},
            {"name": "alice", "x": 9, "y": 9, "z": 1},
            {"name": "Gone", "x": 4, "y": 4, "z": 0},
        ],
    )
    result = playermap.snapshot(["Alice", "Bob"], context=context, now=NOW)
    assert result["state"] == "fresh" and result["ageMs"] == 2000
    assert result["positions"] == [{"name": "Alice", "x": 1, "y": 2, "z": 0}]
    assert result["missing"] == ["Bob"]
    assert playermap.snapshot([], context=context, now=NOW)["state"] == "empty"


def test_snapshot_partial_slot_and_actual_age_without_new_input(map_env):
    data, _, context = map_env
    write_snapshot(data, slot="b")
    write_snapshot(data).write_bytes(b'{"schemaVersion":')
    assert playermap.snapshot(["Alice"], context=context, now=NOW)["state"] == "fresh"
    stale = playermap.snapshot(["Alice"], context=context, now=NOW + 20_000)
    assert stale["state"] == "stale" and stale["capturedAt"] == NOW - 2000
    assert stale["positions"][0]["name"] == "Alice"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"capturedAt": NOW + 6000}, "snapshot-future"),
        ({"capturedAt": NOW - 70_000}, "snapshot-before-start"),
        ({"profile": "other"}, "snapshot-invalid"),
        ({"schemaVersion": True}, "snapshot-invalid"),
        ({"players": [{"name": "Alice", "x": float("nan"), "y": 1, "z": 0}]}, "snapshot-invalid"),
        ({"players": [{"name": "Alice", "x": 1e100, "y": 1, "z": 0}]}, "snapshot-invalid"),
        ({"players": [{"name": "\ud800", "x": 1, "y": 1, "z": 0}]}, "snapshot-invalid"),
    ],
)
def test_snapshot_rejects_untrusted_or_wrong_generation_data(map_env, change, reason):
    data, _, context = map_env
    path = write_snapshot(data)
    frame = json.loads(path.read_text())
    frame.update(change)
    path.write_text(json.dumps(frame), encoding="utf-8")
    result = playermap.snapshot(["Alice"], context=context, now=NOW)
    assert result["state"] == "error" and result["reason"] == reason
    assert result["positions"] == []


def test_snapshot_ambiguous_usernames_are_never_selected_arbitrarily(map_env):
    data, _, context = map_env
    write_snapshot(data, players=[{"name": "Alice", "x": x, "y": 2, "z": 0} for x in (1, 2)])
    result = playermap.snapshot(["Alice"], context=context, now=NOW)
    assert result["positions"] == [] and result["missing"] == ["Alice"]


def test_snapshot_cache_does_not_cross_restarts_profiles_or_rollbacks(map_env):
    data, _, context = map_env
    write_snapshot(data)
    assert playermap.snapshot(["Alice"], context=context, now=NOW)["state"] == "fresh"
    restarted = {
        **context,
        "serverIdentity": "restarted",
        "serverStartedAt": datetime.fromtimestamp((NOW + 5000) / 1000, timezone.utc).isoformat(),
    }
    assert playermap.snapshot(["Alice"], context=restarted, now=NOW + 6000)["positions"] == []
    assert (
        playermap.snapshot(["Alice"], context={**context, "activeFile": "other.ini"}, now=NOW)[
            "positions"
        ]
        == []
    )


def test_snapshot_missing_oversize_and_unsafe_paths_do_not_mutate_game_data(map_env):
    data, _, context = map_env
    assert (
        playermap.snapshot(["Alice"], context=context, now=NOW)["reason"] == "integration-missing"
    )
    path = write_snapshot(data)
    path.write_bytes(b" " * (playermap.MAX_SNAPSHOT_BYTES + 1))
    before = path.read_bytes()
    assert playermap.snapshot(["Alice"], context=context, now=NOW)["reason"] == "snapshot-invalid"
    assert path.read_bytes() == before
    for active in ("../world.ini", "..\\world.ini", "a" * 97 + ".ini", None):
        assert (
            playermap.snapshot(["Alice"], context={"activeFile": active}, now=NOW)["reason"]
            == "profile-unavailable"
        )


def test_snapshot_unicode_and_json_escaped_names_remain_exact(map_env):
    data, _, context = map_env
    name = 'Игрок "A"\\road\nline'
    write_snapshot(data, players=[{"name": name, "x": 0, "y": -2.5, "z": -1}])
    result = playermap.snapshot([name], context=context, now=NOW)
    assert result["positions"][0] == {"name": name, "x": 0, "y": -2.5, "z": -1}


@pytest.mark.parametrize("size", ["256", "300"])
def test_geometry_uses_declared_vector_grid_and_real_properties(map_env, size):
    _, server, context = map_env
    xml_map(server / "media/maps/Muldraugh, KY/worldmap.xml", cell_size=size)
    result = playermap.geometry(context=context)
    assert result["ok"] and result["state"] == "ready" and result["profile"] == "world"
    feature = result["features"][0]
    assert feature["kind"] == "water"
    assert feature["geometry"]["coordinates"][0][0] == [2 * int(size) + 1, -int(size) + 2]
    assert result["bounds"] == {
        "minX": 2 * int(size),
        "minY": -int(size),
        "maxX": 3 * int(size),
        "maxY": 0,
    }


def test_geometry_missing_grid_requires_explicit_override_on_b42(map_env, monkeypatch):
    _, server, context = map_env
    xml_map(server / "media/maps/Muldraugh, KY/worldmap.xml", cell_size=None)
    assert playermap.geometry(context=context)["reason"] == "cell-size-required"
    monkeypatch.setenv("PZ_MAP_CELL_SIZE", "256")
    assert playermap.geometry(context=context)["features"][0]["geometry"]["coordinates"][0][0] == [
        513,
        -254,
    ]
    # Reliable legacy B41 metadata supports the documented 300-tile legacy grid.
    monkeypatch.delenv("PZ_MAP_CELL_SIZE")
    assert playermap.geometry(context={**context, "version": "41.78.16"})["ok"]


def test_geometry_declared_grid_is_not_overridden_and_unknown_schema_rejected(map_env, monkeypatch):
    _, server, context = map_env
    path = xml_map(server / "media/maps/Muldraugh, KY/worldmap.xml", cell_size="300")
    monkeypatch.setenv("PZ_MAP_CELL_SIZE", "256")
    assert playermap.geometry(context=context)["features"][0]["geometry"]["coordinates"][0][0] == [
        601,
        -298,
    ]
    xml_map(path, cell_size="512")
    assert playermap.geometry(context=context)["reason"] == "unsupported-map-schema"
    path.write_text('<!DOCTYPE world [<!ENTITY x "evil">]><world cellSize="300"/>')
    assert playermap.geometry(context=context)["reason"] == "unsupported-map-schema"


def test_geometry_preserves_polygon_holes_and_line_strings(map_env):
    _, server, context = map_env
    path = server / "media/maps/Muldraugh, KY/worldmap.xml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '<world cellSize="300"><cell x="0" y="0"><feature><geometry type="Polygon"><coordinates><point x="0" y="0"/><point x="20" y="0"/><point x="20" y="20"/></coordinates><coordinates><point x="5" y="5"/><point x="8" y="5"/><point x="8" y="8"/></coordinates></geometry></feature><feature><geometry type="LineString"><coordinates><point x="0" y="0"/><point x="30" y="40"/></coordinates></geometry><properties><property name="highway" value="primary"/></properties></feature></cell></world>'
    )
    result = playermap.geometry(context=context)
    assert len(result["features"][0]["geometry"]["coordinates"]) == 2
    assert (
        result["features"][0]["geometry"]["coordinates"][1][0]
        == result["features"][0]["geometry"]["coordinates"][1][-1]
    )
    assert result["features"][1] == {
        "kind": "road",
        "geometry": {"type": "LineString", "coordinates": [[0, 0], [30, 40]]},
    }


def test_geometry_active_mod_map_order_and_missing_maps_are_explicit(map_env):
    data, server, context = map_env
    (data / "Server/world.ini").write_text(
        "Map=Expansion;Unknown;Muldraugh, KY\nMods=\\TownMod\nWorkshopItems=123\n"
    )
    xml_map(server / "media/maps/Muldraugh, KY/worldmap.xml", cell=(0, 0), kind="building")
    mod = server / "steamapps/workshop/content/108600/123/mods/Town/42"
    mod.mkdir(parents=True)
    (mod / "mod.info").write_text("id=TownMod\n")
    xml_map(mod / "media/maps/Expansion/worldmap.xml", cell=(4, 5), kind="highway")
    result = playermap.geometry(context=context)
    assert result["state"] == "partial" and result["missingMaps"] == ["Unknown"]
    assert [feature["kind"] for feature in result["features"]] == ["building", "road"]
    (data / "Server/world.ini").write_text("Map=Expansion\nMods=OtherMod\nWorkshopItems=123\n")
    assert playermap.geometry(context=context)["reason"] == "map-files-missing"


def test_geometry_cache_avoids_reparse_and_invalidates_actual_file_change(map_env, monkeypatch):
    _, server, context = map_env
    path = xml_map(server / "media/maps/Muldraugh, KY/worldmap.xml")
    first = playermap.geometry(context=context)
    parser = playermap._parse
    monkeypatch.setattr(playermap, "_parse", lambda *args: pytest.fail("unchanged map reparsed"))
    assert playermap.geometry(context=context) is first
    monkeypatch.setattr(playermap, "_parse", parser)
    xml_map(path, cell=(33, 44))
    second = playermap.geometry(context=context)
    assert second["revision"] != first["revision"]
    assert second["bounds"]["minX"] == 9900


@pytest.mark.parametrize(
    "cell_size,cell,overlap",
    [
        ("300", (0, 0), True),
        ("256", (1, 0), True),
        ("300", (1, 0), False),
        ("300", (0, 1), False),
        ("256", (2, 0), False),
    ],
)
def test_geometry_reports_unverified_layer_overlap_without_inventing_replacement(
    map_env, cell_size, cell, overlap
):
    data, server, context = map_env
    (data / "Server/world.ini").write_text("Map=Expansion;Muldraugh, KY\n")
    xml_map(server / "media/maps/Muldraugh, KY/worldmap.xml", cell=(0, 0), kind="building")
    xml_map(
        server / "media/maps/Expansion/worldmap.xml",
        cell_size=cell_size,
        cell=cell,
        kind="highway",
    )
    result = playermap.geometry(context=context)
    assert result["ok"] and not result["truncated"]
    assert result["state"] == ("partial" if overlap else "ready")
    assert result["reason"] == ("map-overlap-unverified" if overlap else "ready")
    # Both actual sources remain intact, even in the ambiguous overlapping area.
    assert [feature["kind"] for feature in result["features"]] == ["building", "road"]
    assert result["features"][0]["geometry"]["coordinates"][0][0] == [1, 2]
    assert result["features"][1]["geometry"]["coordinates"][0][0] == [
        cell[0] * int(cell_size) + 1,
        cell[1] * int(cell_size) + 2,
    ]
    assert playermap.geometry(context=context) is result


def test_empty_overlapping_source_cell_still_marks_composite_unverified(map_env):
    data, server, context = map_env
    (data / "Server/world.ini").write_text("Map=Expansion;Muldraugh, KY\n")
    xml_map(server / "media/maps/Muldraugh, KY/worldmap.xml", cell=(0, 0))
    path = server / "media/maps/Expansion/worldmap.xml"
    path.parent.mkdir(parents=True)
    path.write_text('<world cellSize="300"><cell x="0" y="0"/></world>')
    result = playermap.geometry(context=context)
    assert result["state"] == "partial" and result["reason"] == "map-overlap-unverified"
    assert len(result["features"]) == 1 and not result["truncated"]


@pytest.mark.parametrize(
    "limit", ["MAX_XML_BYTES", "MAX_INPUT_POINTS", "MAX_OUTPUT_POINTS", "MAX_FEATURES"]
)
def test_geometry_budgets_fail_honestly_without_touching_source(map_env, monkeypatch, limit):
    _, server, context = map_env
    path = xml_map(server / "media/maps/Muldraugh, KY/worldmap.xml")
    original = path.read_bytes()
    monkeypatch.setattr(playermap, limit, 0)
    result = playermap.geometry(context=context)
    assert not result["ok"] and result["reason"] == "map-budget-exceeded"
    assert path.read_bytes() == original


def test_representative_map_is_bounded_and_cached(map_env):
    _, server, context = map_env
    path = server / "media/maps/Muldraugh, KY/worldmap.xml"
    xml_map(path)
    one = '<feature><geometry type="Polygon"><coordinates><point x="0" y="0"/><point x="10" y="0"/><point x="10" y="10"/></coordinates></geometry><properties><property name="building" value="yes"/></properties></feature>'
    path.write_text('<world cellSize="300"><cell x="2" y="3">' + one * 10_000 + "</cell></world>")
    started = time.monotonic()
    result = playermap.geometry(context=context)
    elapsed = time.monotonic() - started
    assert result["ok"] and len(result["features"]) == 10_000
    assert len(json.dumps(result).encode()) < playermap.MAX_OUTPUT_BYTES
    # A broad guard catches accidental per-feature filesystem/network work.
    assert elapsed < 15
    assert playermap.geometry(context=context) is result


def test_payload_retains_original_fields_with_optional_snapshot(map_env, monkeypatch):
    _, _, context = map_env
    monkeypatch.setattr(payloads.ops, "docker_ok_cached", lambda: False)
    monkeypatch.setattr(
        payloads.ops, "fetch_players", lambda: {"names": ["Alice"], "count": 1, "raw": "-Alice"}
    )
    monkeypatch.setattr(playermap, "_background_context", lambda: context)
    result = payloads.players_payload()
    assert (
        result["ok"]
        and result["names"] == ["Alice"]
        and result["count"] == 1
        and result["raw"] == "-Alice"
    )
    assert result["map"]["reason"] == "integration-missing"


def test_geometry_api_uses_authenticated_route(api, monkeypatch):  # noqa: F811
    fixture = {"ok": True, "state": "ready", "features": [], "profile": "world"}
    monkeypatch.setattr(playermap, "geometry", lambda: fixture)
    assert api("GET", "/api/players/map/geometry") == (200, fixture)
    assert api("GET", "/api/players/map/geometry", headers={"Cookie": ""})[0] == 401
    assert api("GET", "/api/players/map/integration.zip", headers={"Cookie": ""})[0] == 401


def test_xml_rejects_encoded_dtd_and_huge_feature_before_accumulation(map_env, monkeypatch):
    _, server, context = map_env
    path = xml_map(server / "media/maps/Muldraugh, KY/worldmap.xml")
    path.write_bytes(
        '<!DOCTYPE world [<!ENTITY evil "expansion">]><world cellSize="300"/>'.encode("utf-16")
    )
    assert playermap.geometry(context=context)["reason"] == "unsupported-map-schema"
    xml_map(path)
    monkeypatch.setattr(playermap, "MAX_FEATURE_POINTS", 2)
    assert playermap.geometry(context=context)["reason"] == "map-budget-exceeded"


def test_new_bridge_session_replaces_cached_generation_even_when_clock_moves(map_env):
    data, _, context = map_env
    write_snapshot(data, sessionId=str(NOW - 60_000))
    assert playermap.snapshot(["Alice"], context=context, now=NOW)["state"] == "fresh"
    write_snapshot(data, slot="b", sessionId=str(NOW - 1000), capturedAt=NOW - 3000, players=[])
    result = playermap.snapshot(["Alice"], context=context, now=NOW)
    assert result["sessionId"] == str(NOW - 1000) and result["positions"] == []


def test_packaged_archive_contains_only_fixed_mod_files():
    archive = zipfile.ZipFile(io.BytesIO(playermap.integration_archive()))
    assert set(archive.namelist()) == {
        "PZConsoleMap/README.md",
        "PZConsoleMap/common/README.md",
        "PZConsoleMap/42/mod.info",
        "PZConsoleMap/42/media/lua/server/PZConsoleMap.lua",
    }
    assert b"id=PZConsoleMap" in archive.read("PZConsoleMap/42/mod.info")
