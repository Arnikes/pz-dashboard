"""Read-only, bounded player snapshots and locally installed world-map vectors."""

import hashlib
import io
import json
import math
import os
import threading
import time
import zipfile
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

import config
import configeditor
import workshop
from configformats import FormatError, ini_entries, normalize_mod
from errors import EditorError

MAX_AGE_MS = 15_000
MAX_SNAPSHOT_BYTES = 128 * 1024
MAX_XML_BYTES = 256 * 1024 * 1024
MAX_INPUT_POINTS = 2_000_000
MAX_OUTPUT_POINTS = 150_000
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_FEATURES = 200_000
MAX_FEATURE_POINTS = 32_768
_LOCK = threading.Lock()
_SNAPSHOTS = OrderedDict()
_GEOMETRY = OrderedDict()
_CONTEXT = {"key": None, "value": {}, "at": 0.0, "loading": False}


class MapError(ValueError):
    """A safe, stable diagnostic code, never a local filename or XML content."""


def _safe_name(value):
    return (
        isinstance(value, str)
        and 0 < len(value.encode("utf-8")) <= 256
        and value not in (".", "..")
        and not any(ord(char) < 32 or char in "/\\" for char in value)
    )


def _file(root, relative):
    root = Path(root)
    path = root / relative
    if root.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise MapError("unsafe-path")
    current = path
    while current != root:
        if current.is_symlink():
            raise MapError("unsafe-path")
        current = current.parent
    return path


def _profile(context):
    active = context.get("activeFile")
    if not isinstance(active, str) or not active.endswith(".ini") or not _safe_name(active):
        raise MapError("profile-unavailable")
    profile = active[:-4]
    if not _safe_name(profile) or len(profile.encode("utf-8")) > 96:
        raise MapError("profile-unavailable")
    return profile


def _context_key():
    return (
        config.CFG["data_dir"],
        config.CFG["pz_container"],
        os.getenv("PZ_CONFIG_FILE"),
        os.getenv("PZ_VERSION"),
    )


def _background_context():
    """A slow/missing Docker daemon must never delay the online-name collector."""
    key = _context_key()
    with _LOCK:
        if _CONTEXT["key"] != key:
            _CONTEXT.update(key=key, value={}, at=0.0)
        value = dict(_CONTEXT["value"])
        if not _CONTEXT["loading"] and time.monotonic() - _CONTEXT["at"] >= 15:
            _CONTEXT["loading"] = True

            def refresh():
                try:
                    result = configeditor.context()
                except Exception:
                    result = {}
                with _LOCK:
                    if _CONTEXT["key"] == key:
                        _CONTEXT.update(value=result, at=time.monotonic())
                    _CONTEXT["loading"] = False

            threading.Thread(target=refresh, name="player-map-context", daemon=True).start()
    if not value and os.getenv("PZ_CONFIG_FILE"):
        value = {"activeFile": os.getenv("PZ_CONFIG_FILE")}
    return value


def _number(value, limit=100_000_000):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and abs(value) <= limit
        and math.isfinite(value)
    )


def _started_ms(context):
    try:
        started = context.get("serverStartedAt")
        if not started:
            return None
        return datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp() * 1000
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _read_snapshot(path, profile, context, now):
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_SNAPSHOT_BYTES + 1)
    except FileNotFoundError:
        raise MapError("integration-missing") from None
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise MapError("snapshot-invalid")
    try:
        data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise MapError("snapshot-invalid") from None
    if not isinstance(data, dict) or type(data.get("schemaVersion")) is not int:
        raise MapError("snapshot-invalid")
    if data["schemaVersion"] != 1 or data.get("profile") != profile:
        raise MapError("snapshot-invalid")
    captured = data.get("capturedAt")
    session = data.get("sessionId")
    if (
        not _number(captured, 100_000_000_000_000)
        or captured <= 0
        or not isinstance(session, str)
        or not 0 < len(session) <= 128
        or type(data.get("truncated")) is not bool
        or not isinstance(data.get("players"), list)
        or len(data["players"]) > 64
    ):
        raise MapError("snapshot-invalid")
    if captured > now + 5000:
        raise MapError("snapshot-future")
    started = _started_ms(context)
    if started is not None and captured < started - 1000:
        raise MapError("snapshot-before-start")
    positions = []
    for player in data["players"]:
        if (
            not isinstance(player, dict)
            or not isinstance(player.get("name"), str)
            or not 0 < len(player["name"].encode("utf-8")) <= 512
            or not all(_number(player.get(axis)) for axis in ("x", "y", "z"))
        ):
            raise MapError("snapshot-invalid")
        positions.append({key: player[key] for key in ("name", "x", "y", "z")})
    counts = Counter(player["name"] for player in positions)
    return {
        "capturedAt": captured,
        "sessionId": session,
        "truncated": data["truncated"],
        "positions": [player for player in positions if counts[player["name"]] == 1],
    }


def snapshot(names, *, context=None, now=None):
    """Join a complete snapshot to this exact, authoritative RCON membership."""
    now = time.time() * 1000 if now is None else now
    names = list(dict.fromkeys(name for name in names if isinstance(name, str)))
    result = {
        "state": "setup",
        "profile": None,
        "capturedAt": None,
        "ageMs": None,
        "maxAgeMs": MAX_AGE_MS,
        "source": "PZConsoleMap",
        "positions": [],
        "missing": names,
        "truncated": False,
        "reason": "profile-unavailable",
        "sessionId": None,
    }
    context = _background_context() if context is None else context
    try:
        profile = _profile(context)
    except MapError:
        return result
    result["profile"] = profile
    key = (_context_key(), profile, context.get("serverIdentity"), context.get("serverStartedAt"))
    with _LOCK:
        # Never retain positions from a different current server or profile.
        for previous in list(_SNAPSHOTS):
            if previous != key:
                del _SNAPSHOTS[previous]
        previous = _SNAPSHOTS.get(key)
    valid, errors = [], []
    for slot in ("a", "b"):
        try:
            filename = f"pz-console-map-{profile.encode('utf-8').hex()}-{slot}.json"
            path = _file(Path(config.CFG["data_dir"]) / "Lua", filename)
            valid.append(_read_snapshot(path, profile, context, now))
        except (MapError, OSError, UnicodeError) as error:
            errors.append(str(error) if isinstance(error, MapError) else "snapshot-invalid")

    def generation(frame):
        session = frame["sessionId"]
        return (int(session) if session.isdecimal() else 0, frame["capturedAt"])

    latest = max(valid, key=generation) if valid else None
    if (
        latest
        and previous
        and latest["sessionId"] == previous["sessionId"]
        and latest["capturedAt"] < previous["capturedAt"]
    ):
        latest = None
    if latest:
        with _LOCK:
            _SNAPSHOTS[key] = latest
    frame = latest or previous
    if frame is None:
        result["reason"] = next(
            (code for code in errors if code != "integration-missing"), "integration-missing"
        )
        if result["reason"] != "integration-missing":
            result["state"] = "error"
        return result
    age = max(0, now - frame["capturedAt"])
    current = latest is not None and age <= MAX_AGE_MS
    positions = [player for player in frame["positions"] if player["name"] in names]
    located = {player["name"] for player in positions}
    result.update(
        state="fresh" if current else "stale",
        capturedAt=frame["capturedAt"],
        ageMs=age,
        positions=positions,
        missing=[name for name in names if name not in located],
        truncated=frame["truncated"],
        reason="ready" if current else "snapshot-stale",
        sessionId=frame["sessionId"],
    )
    if not names:
        result.update(
            state="empty" if current else "stale",
            reason="no-online-players" if current else "snapshot-stale",
        )
    return result


def _saved_settings(context):
    profile = _profile(context)
    text = configeditor.read_profile(profile + ".ini")["ini"]
    entries = ini_entries(text)
    values = {key: item["value"] for key, item in entries.items()}
    maps = list(
        dict.fromkeys(value.strip() for value in values.get("Map", "").split(";") if value.strip())
    )
    mods = list(
        dict.fromkeys(
            normalize_mod(value) for value in values.get("Mods", "").split(";") if value.strip()
        )
    )
    items = list(
        dict.fromkeys(
            value.strip() for value in values.get("WorkshopItems", "").split(";") if value.strip()
        )
    )
    if (
        len(maps) > 128
        or len(mods) > 512
        or len(items) > 256
        or not all(_safe_name(name) for name in maps)
    ):
        raise MapError("map-invalid")
    if not all(item.isdecimal() and len(item) <= 20 for item in items):
        raise MapError("map-invalid")
    return profile, maps, mods, items


def _mod_layers(root, active_mods, version):
    """Inspect only installed mod identities/branches, never arbitrary game assets."""
    if not root.is_dir() or root.is_symlink():
        return []
    folders = []
    for index, folder in enumerate(root.iterdir()):
        if index >= 512:
            break
        if not folder.is_dir() or folder.is_symlink():
            continue
        branches = ["common"]
        game = workshop.version_tuple(version)
        candidates = [
            child.name
            for child in folder.iterdir()
            if child.is_dir()
            and not child.is_symlink()
            and child.name.startswith("42")
            and workshop.version_tuple(child.name)
            and game
            and workshop.version_tuple(child.name) <= game
        ]
        if candidates:
            branches.append(max(candidates, key=workshop.version_tuple))
        elif (folder / "42").is_dir():
            branches.append("42")
        else:
            branches.append("")
        identity = None
        for branch in reversed(branches):
            try:
                path = _file(folder, Path(branch) / "mod.info")
                if path.is_file() and path.stat().st_size <= 512_000:
                    identity = workshop.metadata(path.read_text(encoding="utf-8-sig")).get("id")
                    if identity:
                        break
            except (MapError, OSError, UnicodeError):
                continue
        if identity in active_mods:
            folders.append((active_mods.index(identity), folder, branches))
    return sorted(folders, key=lambda record: record[0])


def _sources(context):
    profile, maps, mods, items = _saved_settings(context)
    server = Path(os.getenv("SERVER_FILES_DIR", "/server-files"))
    roots = [(server, Path("media/maps"))]
    mod_roots = [Path(config.CFG["data_dir"]) / "mods"]
    for root in workshop.roots():
        for item in items:
            try:
                mod_roots.append(_file(root, Path(item) / "mods"))
            except MapError:
                continue
    layers = []
    for root in mod_roots:
        layers.extend(_mod_layers(root, mods, context.get("version", "")))
    for _, folder, branches in sorted(layers, key=lambda record: record[0]):
        for branch in branches:
            roots.append((folder, Path(branch) / "media/maps"))
    sources, missing = [], []
    # Earlier Map entries override later entries in PZ, so paint them last.
    for name in reversed(maps):
        selected = None
        for root, prefix in roots:
            try:
                path = _file(root, prefix / name / "worldmap.xml")
                if path.is_file():
                    selected = path
            except (MapError, OSError):
                continue
        if selected:
            sources.append(selected)
        else:
            missing.append(name)
    return profile, maps, sources, missing


class _XMLReader:
    def __init__(self, stream, budget):
        self.stream, self.budget, self.tail = stream, budget, b""

    def read(self, size=-1):
        data = self.stream.read(size)
        self.budget["bytes"] += len(data)
        if self.budget["bytes"] > MAX_XML_BYTES:
            raise MapError("map-budget-exceeded")
        scan = (self.tail + data).upper()
        if b"\0" in data or b"<!DOCTYPE" in scan or b"<!ENTITY" in scan:
            raise MapError("unsupported-map-schema")
        self.tail = scan[-16:]
        return data


def _kind(feature):
    properties = {
        node.get("name", ""): node.get("value", "")
        for node in feature.findall("./properties/property")
    }
    if properties.get("water") or properties.get("natural") == "water":
        return "water"
    if properties.get("highway") or properties.get("railway"):
        return "road"
    if properties.get("building"):
        return "building"
    if properties.get("landuse") in ("forest", "wood") or properties.get("natural") in (
        "wood",
        "forest",
    ):
        return "forest"
    return "other"


def _simplify(points, polygon):
    """A bounded overview keeps real coordinates; it never invents a basemap."""
    if len(points) <= 32:
        return points
    stride = max(1, math.ceil(len(points) / 128))
    simplified = points[::stride]
    if simplified[-1] != points[-1]:
        simplified.append(points[-1])
    if polygon and simplified[0] != simplified[-1]:
        simplified.append(simplified[0])
    return simplified


def _feature(element, origin, budget):
    geometry = element.find("geometry")
    if geometry is None or geometry.get("type") not in ("Polygon", "LineString"):
        raise MapError("unsupported-map-schema")
    polygon = geometry.get("type") == "Polygon"
    rings = []
    for coordinates in geometry.findall("coordinates"):
        points = []
        for point in coordinates.findall("point"):
            budget["input"] += 1
            if budget["input"] > MAX_INPUT_POINTS:
                raise MapError("map-budget-exceeded")
            try:
                x, y = float(point.get("x", "")), float(point.get("y", ""))
            except ValueError:
                raise MapError("map-invalid") from None
            if not _number(x) or not _number(y):
                raise MapError("map-invalid")
            points.append([round(origin[0] + x, 2), round(origin[1] + y, 2)])
        if len(points) < (3 if polygon else 2):
            raise MapError("map-invalid")
        if polygon and points[0] != points[-1]:
            points.append(points[0])
        rings.append(_simplify(points, polygon))
    if not rings or not polygon and len(rings) != 1:
        raise MapError("map-invalid")
    budget["output"] += sum(len(ring) for ring in rings)
    if budget["output"] > MAX_OUTPUT_POINTS:
        raise MapError("map-budget-exceeded")
    return {
        "kind": _kind(element),
        "geometry": {"type": geometry.get("type"), "coordinates": rings if polygon else rings[0]},
    }


def _parse(path, context, budget, features, bounds):
    override = os.getenv("PZ_MAP_CELL_SIZE", "")
    if override and override not in ("256", "300"):
        raise MapError("cell-size-required")
    depth, cell, cell_size, feature_points = 0, None, None, 0
    with path.open("rb") as stream:
        for event, element in ElementTree.iterparse(
            _XMLReader(stream, budget), events=("start", "end")
        ):
            if event == "start":
                depth += 1
                if depth > 20:
                    raise MapError("unsupported-map-schema")
                if element.tag == "feature":
                    feature_points = 0
                elif element.tag == "point":
                    feature_points += 1
                    budget["seen"] = budget.get("seen", 0) + 1
                    if feature_points > MAX_FEATURE_POINTS or budget["seen"] > MAX_INPUT_POINTS:
                        raise MapError("map-budget-exceeded")
                if depth == 1:
                    if element.tag != "world":
                        raise MapError("unsupported-map-schema")
                    declared = element.get("cellSize")
                    if declared is not None:
                        if declared not in ("256", "300"):
                            raise MapError("unsupported-map-schema")
                        cell_size = int(declared)
                    elif override:
                        cell_size = int(override)
                    elif str(context.get("version", "")).startswith("41."):
                        cell_size = 300
                    else:
                        raise MapError("cell-size-required")
                elif element.tag == "cell":
                    if depth != 2:
                        raise MapError("unsupported-map-schema")
                    try:
                        cx, cy = int(element.get("x", "")), int(element.get("y", ""))
                    except ValueError:
                        raise MapError("map-invalid") from None
                    if abs(cx) > 100_000 or abs(cy) > 100_000:
                        raise MapError("map-invalid")
                    cell = (cx * cell_size, cy * cell_size)
                    bounds[0] = min(bounds[0], cell[0])
                    bounds[1] = min(bounds[1], cell[1])
                    bounds[2] = max(bounds[2], cell[0] + cell_size)
                    bounds[3] = max(bounds[3], cell[1] + cell_size)
            else:
                if element.tag == "feature":
                    budget["features"] += 1
                    if cell is None or depth != 3:
                        raise MapError("unsupported-map-schema")
                    if budget["features"] > MAX_FEATURES:
                        raise MapError("map-budget-exceeded")
                    feature = _feature(element, cell, budget)
                    features.append(feature)
                    shape = feature["geometry"]
                    rings = (
                        shape["coordinates"]
                        if shape["type"] == "Polygon"
                        else [shape["coordinates"]]
                    )
                    for ring in rings:
                        for x, y in ring:
                            bounds[0], bounds[1] = min(bounds[0], x), min(bounds[1], y)
                            bounds[2], bounds[3] = max(bounds[2], x), max(bounds[3], y)
                    element.clear()
                elif element.tag == "cell":
                    cell = None
                    element.clear()
                depth -= 1


def geometry(*, context=None):
    """Build a cacheable, authenticated overview from this installation's files."""
    result = {
        "ok": False,
        "state": "setup",
        "profile": None,
        "revision": None,
        "bounds": None,
        "features": [],
        "reason": "map-files-missing",
        "missingMaps": [],
        "truncated": False,
    }
    try:
        context = configeditor.context() if context is None else context
        profile, maps, paths, missing = _sources(context)
        result.update(profile=profile, missingMaps=missing)
        if not paths:
            return result
        stamps = [(str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths]
        key = (
            _context_key(),
            profile,
            tuple(maps),
            tuple(stamps),
            os.getenv("PZ_MAP_CELL_SIZE"),
            context.get("version"),
        )
        with _LOCK:
            cached = _GEOMETRY.get(key)
            if cached is not None:
                _GEOMETRY.move_to_end(key)
                return cached
        features, bounds = [], [math.inf, math.inf, -math.inf, -math.inf]
        budget = dict(bytes=0, input=0, output=0, features=0)
        errors, source_extents, overlap = [], [], False
        for path in paths:
            if path.stat().st_size > MAX_XML_BYTES - budget["bytes"]:
                errors.append("map-budget-exceeded")
                continue
            source_bounds = [math.inf, math.inf, -math.inf, -math.inf]
            exhausted = False
            try:
                _parse(path, context, budget, features, source_bounds)
            except (MapError, ElementTree.ParseError, OSError) as error:
                errors.append(str(error) if isinstance(error, MapError) else "map-invalid")
                exhausted = errors[-1] == "map-budget-exceeded"
            if all(math.isfinite(value) for value in source_bounds):
                # Runtime replacement rules are not inferred from paint order.
                # Conservative source extents can over-report overlap across gaps.
                overlap = overlap or any(
                    source_bounds[0] < other[2]
                    and source_bounds[2] > other[0]
                    and source_bounds[1] < other[3]
                    and source_bounds[3] > other[1]
                    for other in source_extents
                )
                source_extents.append(source_bounds)
                bounds = [
                    min(bounds[0], source_bounds[0]),
                    min(bounds[1], source_bounds[1]),
                    max(bounds[2], source_bounds[2]),
                    max(bounds[3], source_bounds[3]),
                ]
            if exhausted:
                break
        if not features:
            result.update(
                reason=errors[0] if errors else "map-invalid",
                state="setup" if errors and errors[0] == "cell-size-required" else "error",
            )
            return result
        result.update(
            ok=True,
            state="partial" if missing or errors or overlap else "ready",
            features=features,
            bounds=dict(zip(("minX", "minY", "maxX", "maxY"), bounds)),
            reason=errors[0] if errors else "map-overlap-unverified" if overlap else "ready",
            truncated=bool(errors),
            revision=hashlib.sha256(repr(key).encode()).hexdigest()[:20],
        )
        while (
            len(json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode())
            > MAX_OUTPUT_BYTES
            and result["features"]
        ):
            del result["features"][len(result["features"]) // 2 :]
            result.update(state="partial", truncated=True, reason="map-budget-exceeded")
        with _LOCK:
            _GEOMETRY[key] = result
            _GEOMETRY.move_to_end(key)
            while len(_GEOMETRY) > 4:
                _GEOMETRY.popitem(last=False)
        return result
    except (MapError, EditorError, FormatError, OSError, UnicodeError) as error:
        result["reason"] = str(error) if isinstance(error, MapError) else "map-invalid"
        return result


def integration_archive():
    """Only the fixed, public integration package can enter this archive."""
    root = Path(__file__).resolve().parent / "integrations/PZConsoleMap"
    names = ("README.md", "common/README.md", "42/mod.info", "42/media/lua/server/PZConsoleMap.lua")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            path = _file(root, name)
            if path.stat().st_size > MAX_SNAPSHOT_BYTES:
                raise MapError("integration-unavailable")
            archive.writestr("PZConsoleMap/" + name, path.read_bytes())
    return output.getvalue()
