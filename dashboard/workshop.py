"""Steam-only B42 metadata index. No identity guesses based on names or order."""

import os
import hashlib
import re
import shlex
import threading
import time
import urllib.parse
import urllib.request
import json
from collections import OrderedDict
from pathlib import Path

import config
import dockerlib
import fileio
import i18n
from configformats import FormatError, SECRET_KEY, decode_string, normalize_mod

_CACHE = OrderedDict()
_LOCK = threading.Lock()
_TRANSLATIONS = OrderedDict()


def _store_cache(cache, key, value, ttl, max_entries=8):
    """Called under _LOCK; discard expired profiles and bound retained metadata."""
    now = time.time()
    for old_key, (created, _) in list(cache.items()):
        if now - created >= ttl:
            del cache[old_key]
    cache[key] = (now, value)
    cache.move_to_end(key)
    while len(cache) > max_entries:
        cache.popitem(last=False)


def invalidate():
    with _LOCK:
        _CACHE.clear()


def parse_translations(text, json_format=False):
    if json_format:
        try:
            values = json.loads(text)
        except ValueError:
            return {}
        if not isinstance(values, dict):
            return {}
        if isinstance(values.get("Sandbox"), dict):
            values = values["Sandbox"]
        return {
            key if key.startswith("Sandbox_") else "Sandbox_" + key: value
            for key, value in values.items()
            if isinstance(value, str)
        }
    result = {}
    for match in re.finditer(
        r'(Sandbox_[\w.]+)\s*=\s*("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')', text
    ):
        try:
            result[match[1]] = decode_string(match[2])
        except FormatError:
            continue
    return result


def vanilla_translations(version, allow_container=False):
    """Use translations from this installation; retain its version's cache offline."""
    key = (
        version,
        config.CFG["data_dir"],
        config.CFG["pz_container"],
        os.getenv("SERVER_FILES_DIR", "/server-files"),
        i18n.language(),
    )
    with _LOCK:
        cached = _TRANSLATIONS.get(key)
    if cached and time.time() - cached[0] < 300:
        return cached[1]
    bodies = []
    for base in (key[3], config.CFG["data_dir"]):
        for relative in (
            f"media/lua/shared/Translate/{i18n.language().upper()}/Sandbox_{i18n.language().upper()}.txt",
            f"media/lua/shared/Translate/{i18n.language().upper()}/Sandbox.json",
        ):
            path = Path(base) / relative
            if path.is_file() and not path.is_symlink() and path.stat().st_size < 512_000:
                bodies.append(
                    (path.as_posix(), path.read_text(encoding="utf-8-sig", errors="replace"))
                )
    if not bodies and allow_container and version_tuple(version):
        command = (
            "find / -maxdepth 10 -type f \\( -path '*/media/lua/shared/Translate/RU/Sandbox_RU.txt' "
            "-o -path '*/media/lua/shared/Translate/RU/Sandbox.json' \\) "
            "! -path '*/workshop/*' ! -path '*/mods/*' -size -512k "
            '-exec sh -c \'for f do printf "\\036%s\\037" "$f"; cat "$f"; done\' sh {} + 2>/dev/null'
        )
        if i18n.language() == "en":
            command = command.replace("/RU/", "/EN/").replace("Sandbox_RU", "Sandbox_EN")
        code, output, _ = dockerlib.container_exec(config.CFG["pz_container"], command, timeout=15)
        if code == 0 and len(output) < 2_000_000:
            for record in output.split("\x1e"):
                path, separator, body = record.partition("\x1f")
                if separator:
                    bodies.append((path, body))
    result = {}
    for path, body in sorted(bodies, key=lambda pair: pair[0].endswith(".json")):
        result.update(parse_translations(body, path.endswith(".json")))
    cache_path = (
        Path(config.CFG["dashboard_dir"])
        / "game-metadata"
        / (hashlib.sha256(json.dumps(key).encode()).hexdigest() + ".json")
    )
    if result:
        try:
            fileio.atomic_write(cache_path, json.dumps(result, ensure_ascii=False).encode("utf-8"))
        except OSError:
            pass
    else:
        try:
            stored = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                result = {k: v for k, v in stored.items() if isinstance(v, str)}
        except (ValueError, OSError):
            pass
    with _LOCK:
        _store_cache(_TRANSLATIONS, key, result, 300)
    return result


def version_tuple(value):
    match = re.search(r"\b(\d{2}(?:\.\d+){0,2})\b", str(value or ""))
    if not match:
        return ()
    parts = tuple(int(p) for p in match[1].split("."))
    return (*parts, *(0 for _ in range(3 - len(parts))))


def metadata(text):
    result = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith(("#", ";")):
            key, value = line.split("=", 1)
            result[key.strip().lower()] = value.strip()
    return result


def roots():
    candidates = [os.getenv("WORKSHOP_DIR", "")]
    for base in (config.CFG["data_dir"], os.getenv("SERVER_FILES_DIR", "/server-files")):
        candidates.extend(
            [
                os.path.join(base, "steamapps/workshop/content/108600"),
                os.path.join(base, "workshop/content/108600"),
            ]
        )
    return [Path(p) for p in candidates if p and Path(p).is_dir()]


def local_files(items):
    files = {}
    for root in roots():
        for wid in items:
            folder = root / wid
            if not folder.is_dir():
                continue
            resolved_folder = folder.resolve()
            # Walk directory names, creating Paths only for metadata. Mod assets
            # can number in the tens of thousands; never follow directory links.
            for directory, _, names in os.walk(folder, followlinks=False):
                for name in names:
                    if name not in (
                        "mod.info",
                        "sandbox-options.txt",
                        "map.info",
                        "Sandbox.json",
                        "Sandbox_RU.txt",
                        "Sandbox_EN.txt",
                    ):
                        continue
                    path = Path(directory) / name
                    if (
                        path.is_symlink()
                        or not path.is_file()
                        or not path.resolve().is_relative_to(resolved_folder)
                    ):
                        continue
                    if path.stat().st_size > 512_000:
                        continue
                    files[f"{wid}/{path.relative_to(folder).as_posix()}"] = path.read_text(
                        encoding="utf-8-sig", errors="replace"
                    )
    return files


def container_files(items):
    """Read only small metadata files in one exec; never copy whole mod assets."""
    name = config.CFG["pz_container"]
    code, out, _ = dockerlib.container_exec(
        name,
        "find / -maxdepth 10 -type d -path '*/workshop/content/108600' 2>/dev/null | head -5",
        timeout=60,
    )
    if code != 0:
        return {}
    files = {}
    for root in out.splitlines()[:5]:
        root = root.strip()
        if not root.startswith("/"):
            continue
        dirs = " ".join(shlex.quote(root + "/" + wid) for wid in items)
        if not dirs:
            continue
        command = (
            f"find {dirs} -type f \\( -name mod.info -o -name sandbox-options.txt -o -name map.info "
            "-o -name Sandbox_RU.txt -o -name Sandbox_EN.txt -o -name Sandbox.json \\) -size -512k "
            '-exec sh -c \'for f do printf "\\036%s\\037" "$f"; cat "$f"; done\' sh {} + 2>/dev/null'
        )
        code, out, _ = dockerlib.container_exec(name, command, timeout=120)
        if code != 0 or len(out) > 16_000_000:
            continue
        for record in out.split("\x1e"):
            path, separator, body = record.partition("\x1f")
            if separator and path.startswith(root + "/"):
                files[path[len(root) + 1 :]] = body
    return files


def options(text, translations):
    result = []
    for match in re.finditer(r"\boption\s+([\w.]+)\s*\{([^{}]*)\}", text, re.S):
        props = dict(
            (k.lower(), v.strip().strip('"'))
            for k, v in re.findall(r"(\w+)\s*=\s*([^,\r\n]*)", match[2])
        )
        kind = props.get("type", "string").lower()
        key = match[1]
        rec = {
            "key": key,
            "type": kind,
            "group": translations.get(
                "Sandbox_" + props.get("page", ""), props.get("page", "Настройки модов")
            ),
            "label": translations.get("Sandbox_" + props.get("translation", key), key),
            "hint": translations.get("Sandbox_" + props.get("translation", key) + "_tooltip", ""),
            "custom": True,
            "page": props.get("page", ""),
            "translation": props.get("translation", key),
            "secret": bool(SECRET_KEY.search(key)),
            "valueTranslation": props.get("valuetranslation", props.get("translation", key)),
        }
        for prop in ("min", "max", "default", "numvalues"):
            if prop in props:
                value = props[prop]
                try:
                    rec[prop] = float(value) if "." in value else int(value)
                except ValueError:
                    rec[prop] = (
                        value.lower() == "true" if value.lower() in ("true", "false") else value
                    )
        if kind == "enum":
            try:
                count = int(rec.get("numvalues", 0))
            except (ValueError, TypeError, OverflowError):
                count = 0
            rec["choices"] = [
                {
                    "value": i,
                    "label": translations.get(
                        "Sandbox_"
                        + props.get("valuetranslation", props.get("translation", key))
                        + f"_option{i}",
                        str(i),
                    ),
                }
                for i in range(1, min(count, 100) + 1)
            ]
        result.append(rec)
    return result


def build_index(files, items, version):
    records = []
    game = version_tuple(version)
    groups = {}
    for path in files:
        parts = path.split("/")
        if len(parts) < 4 or parts[1] != "mods":
            continue
        folder = "/".join(parts[:3])
        branch = parts[3] if len(parts) > 4 else "root"
        variants = groups.setdefault(folder, {})
        variants.setdefault(branch, None)
        if path.endswith("/mod.info"):
            variants[branch] = path
    for folder, variants in groups.items():
        compatible = [
            b
            for b in variants
            if re.fullmatch(r"42(?:\.\d+){0,2}", b) and game and version_tuple(b) <= game
        ]
        b42 = [b for b in variants if re.fullmatch(r"42(?:\.\d+){0,2}", b)]
        branch = (
            max(compatible, key=version_tuple)
            if compatible
            else min(b42, key=version_tuple)
            if b42
            else "common"
            if "common" in variants
            else None
        )
        if branch is None:
            continue
        compatible_version = bool(compatible or branch == "common") if game else None
        info_path = variants[branch] or variants.get("common")
        if not info_path:
            continue
        info = metadata(files[info_path])
        mid = info.get("id", "").strip()
        if not mid:
            records.append(
                {
                    "workshopId": folder.split("/")[0],
                    "modId": "",
                    "name": info.get("name", ""),
                    "error": "В mod.info отсутствует id=",
                    "path": info_path,
                }
            )
            continue
        layers = [folder + "/common/", folder + "/" + branch + "/"]
        effective = {}
        for prefix in dict.fromkeys(layers):
            for path, body in files.items():
                if path.startswith(prefix):
                    effective[path[len(prefix) :]] = body
        translations = {}
        for language in ("EN", "RU") if i18n.language() == "ru" else ("EN",):
            for path, body in effective.items():
                if path.endswith(f"Sandbox_{language}.txt") or path.endswith(
                    f"Translate/{language}/Sandbox.json"
                ):
                    translations.update(parse_translations(body, path.endswith(".json")))

        def dependencies(key):
            return [normalize_mod(v) for v in info.get(key, "").split(",") if v.strip()]

        records.append(
            {
                "workshopId": folder.split("/")[0],
                "modId": mid,
                "name": info.get("name", mid),
                "folder": folder.split("/")[-1],
                "path": info_path,
                "branch": branch,
                "compatible": compatible_version,
                "versionMin": info.get("versionmin", ""),
                "versionMax": info.get("versionmax", ""),
                "require": dependencies("require"),
                "incompatible": dependencies("incompatible"),
                "after": dependencies("loadmodafter"),
                "before": dependencies("loadmodbefore"),
                "maps": [
                    p.split("/")[2]
                    for p in effective
                    if p.startswith("media/maps/") and p.endswith("/map.info")
                ],
                "options": options(effective.get("media/sandbox-options.txt", ""), translations),
            }
        )
    return {wid: [r for r in records if r["workshopId"] == wid] for wid in items}


def manifest_items(text):
    """Read installed item records from Steam's data-only KeyValues manifest."""
    tokens = re.findall(r'"((?:\\.|[^"\\])*)"|([{}])', text)
    position = 0

    def table(depth=0):
        nonlocal position
        if depth > 20:
            raise ValueError("Manifest nesting")
        result = {}
        while position < len(tokens):
            key, bracket = tokens[position]
            position += 1
            if bracket == "}":
                return result
            if bracket or position >= len(tokens):
                raise ValueError("Invalid manifest")
            value, bracket = tokens[position]
            position += 1
            result[key] = table(depth + 1) if bracket == "{" else value
        return result

    parsed = table().get("AppWorkshop", {})
    if not isinstance(parsed, dict) or str(parsed.get("appid", "108600")) != "108600":
        return {}
    installed = parsed.get("WorkshopItemsInstalled", {})
    return installed if isinstance(installed, dict) else {}


def download_manifest(items):
    texts = []
    for root in roots():
        path = root.parent.parent / "appworkshop_108600.acf"
        if path.is_file() and path.stat().st_size < 512_000:
            texts.append(path.read_text(encoding="utf-8-sig", errors="replace"))
    if not texts:
        code, output, _ = dockerlib.container_exec(
            config.CFG["pz_container"],
            "find / -maxdepth 10 -type f -name appworkshop_108600.acf -size -512k -exec cat {} \\; 2>/dev/null",
            timeout=60,
        )
        if code == 0 and output and len(output) < 2_000_000:
            texts.append(output)
    if not texts:
        return None
    installed = {}
    for text in texts:
        try:
            installed.update(manifest_items(text))
        except (ValueError, TypeError):
            continue
    return {
        wid: bool(isinstance(installed.get(wid), dict) and installed[wid].get("manifest"))
        for wid in items
    }


def scan(items, version, refresh=False):
    items = list(dict.fromkeys(str(i) for i in items if str(i).isdigit()))
    key = (
        tuple(items),
        version,
        config.CFG["data_dir"],
        config.CFG["pz_container"],
        i18n.language(),
    )
    with _LOCK:
        cached = _CACHE.get(key)
    if cached and not refresh and time.time() - cached[0] < 120:
        return cached[1]
    files = local_files(items)
    missing = [wid for wid in items if not any(p.startswith(wid + "/") for p in files)]
    if missing:
        files.update(container_files(missing))
    index = build_index(files, items, version)
    cache_path = (
        Path(config.CFG["dashboard_dir"])
        / "workshop-index"
        / (hashlib.sha256(json.dumps(key).encode()).hexdigest() + ".json")
    )
    if files:
        try:
            fileio.atomic_write(cache_path, json.dumps(index, ensure_ascii=False).encode("utf-8"))
        except OSError:
            pass
    elif cache_path.is_file():
        try:
            index = json.loads(cache_path.read_text(encoding="utf-8"))
            for records in index.values():
                for rec in records:
                    rec["metadataStale"] = True
        except (ValueError, OSError):
            pass
    with _LOCK:
        _store_cache(_CACHE, key, index, 120)
    return index


def problems(index, selected, version):
    result = []
    providers = {}
    for records in index.values():
        for rec in records:
            if rec.get("error"):
                result.append(
                    {
                        "workshopId": rec["workshopId"],
                        "modId": "",
                        "severity": "warning",
                        "code": "metadata",
                        "message": rec["error"],
                    }
                )
            if rec.get("compatible") is False and rec.get("modId") not in selected:
                result.append(
                    {
                        "workshopId": rec["workshopId"],
                        "modId": rec.get("modId", ""),
                        "severity": "warning",
                        "code": "version",
                        "message": f"{rec.get('modId', '')}: нет подходящего каталога B42 для {version}",
                    }
                )
            if rec.get("modId"):
                providers.setdefault(rec["modId"], []).append(rec)
    positions = {mid: i for i, mid in enumerate(selected)}
    edges = {mid: set() for mid in selected}
    for mid in selected:
        found = providers.get(mid, [])
        if not found:
            result.append(
                {
                    "modId": mid,
                    "severity": "warning",
                    "code": "unknown",
                    "message": f"{mid}: Steam-содержимое не определено",
                }
            )
            continue
        if len(found) != 1:
            result.append(
                {
                    "modId": mid,
                    "severity": "error",
                    "code": "ambiguous",
                    "message": f"{mid}: несколько поставщиков ModID",
                }
            )
            continue
        rec = found[0]
        if rec.get("compatible") is False:
            result.append(
                {
                    "workshopId": rec["workshopId"],
                    "modId": mid,
                    "severity": "error",
                    "code": "version",
                    "message": f"{mid}: нет подходящего каталога B42 для {version}",
                }
            )
        if rec.get("metadataStale"):
            result.append(
                {
                    "modId": mid,
                    "severity": "warning",
                    "code": "cached",
                    "message": f"{mid}: последние сохранённые метаданные; перечитайте пакет после запуска сервера",
                }
            )
        for dep in rec["require"]:
            if dep not in positions:
                result.append(
                    {
                        "modId": mid,
                        "dependency": dep,
                        "severity": "error",
                        "code": "dependency",
                        "message": f"{mid}: требуется {dep}",
                    }
                )
            else:
                edges[mid].add(dep)
        for other in rec["incompatible"]:
            if other in positions:
                result.append(
                    {
                        "modId": mid,
                        "severity": "error",
                        "code": "incompatible",
                        "message": f"{mid}: несовместим с {other}",
                    }
                )
        for prop, relation in (("after", "after"), ("before", "before")):
            for other in rec[prop]:
                if other in positions:
                    edges[mid if relation == "after" else other].add(
                        other if relation == "after" else mid
                    )
                    wrong = (
                        positions[mid] < positions[other]
                        if relation == "after"
                        else positions[mid] > positions[other]
                    )
                    if wrong:
                        result.append(
                            {
                                "modId": mid,
                                "severity": "error",
                                "code": "order",
                                "message": f"{mid}: нарушено ограничение {prop}={other}",
                            }
                        )
        current = version_tuple(version)
        for prop, bad in (
            ("versionMin", lambda v: current < v),
            ("versionMax", lambda v: current > v),
        ):
            bound = version_tuple(rec[prop])
            if current and bound and bad(bound):
                result.append(
                    {
                        "modId": mid,
                        "severity": "error",
                        "code": "version",
                        "message": f"{mid}: ограничение {prop}={rec[prop]}",
                    }
                )
    visiting, visited = set(), set()

    def visit(mid):
        if mid in visiting:
            return True
        if mid in visited:
            return False
        visiting.add(mid)
        cycle = any(visit(dep) for dep in edges[mid])
        visiting.remove(mid)
        visited.add(mid)
        return cycle

    if any(visit(mid) for mid in edges):
        result.append(
            {
                "severity": "error",
                "code": "cycle",
                "message": "Цикл обязательных зависимостей или ограничений порядка",
            }
        )
    return result


def steam_call(method, ids):
    field = "collectioncount" if method == "GetCollectionDetails" else "itemcount"
    params = {field: len(ids), **{f"publishedfileids[{i}]": wid for i, wid in enumerate(ids)}}
    req = urllib.request.Request(
        f"https://api.steampowered.com/ISteamRemoteStorage/{method}/v1/",
        data=urllib.parse.urlencode(params).encode(),
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read(2_000_000)).get("response", {})


def _steam_records(method, ids, key):
    """Match every response to its requested ID; Steam may return another order."""
    records = {}
    for offset in range(0, len(ids), 100):
        batch = ids[offset : offset + 100]
        response = steam_call(method, batch).get(key, [])
        if not isinstance(response, list):
            raise ValueError("Steam вернул некорректный список items")
        for record in response:
            if not isinstance(record, dict):
                raise ValueError("Steam вернул некорректные метаданные item")
            wid = str(record.get("publishedfileid", ""))
            if wid not in batch or wid in records:
                raise ValueError("Steam вернул неполный или неоднозначный список items")
            records[wid] = record
    if len(records) != len(ids):
        raise ValueError("Steam вернул неполный или неоднозначный список items")
    return [records[wid] for wid in ids]


def _published_items(ids):
    details = _steam_records("GetPublishedFileDetails", ids, "publishedfiledetails")
    if any(
        r.get("result") != 1 or r.get("consumer_app_id") != 108600 or r.get("banned")
        for r in details
    ):
        raise ValueError("Steam item недоступен или не принадлежит Project Zomboid")
    return details


def _collections(ids):
    # The public legacy details endpoint omits file_type even for collections.
    # CollectionDetails returns result=9 for an ordinary published file.
    records = _steam_records("GetCollectionDetails", ids, "collectiondetails")
    if any(r.get("result") not in (1, 9) for r in records):
        raise ValueError("Не удалось проверить коллекции Steam")
    return records


def resolve(value, with_source=False):
    if not isinstance(value, str):
        raise ValueError("Нужна Steam-ссылка или Workshop ID")
    if value.strip().isdigit():
        wid = value.strip()
    else:
        url = urllib.parse.urlparse(value.strip())
        if (
            url.scheme != "https"
            or url.hostname != "steamcommunity.com"
            or url.path
            not in (
                "/sharedfiles/filedetails/",
                "/sharedfiles/filedetails",
                "/workshop/filedetails/",
                "/workshop/filedetails",
            )
        ):
            raise ValueError("Нужна ссылка steamcommunity.com/sharedfiles/filedetails/?id=…")
        wid = urllib.parse.parse_qs(url.query).get("id", [""])[0]
    if not wid.isdigit() or len(wid) > 20:
        raise ValueError("Некорректный Workshop ID")
    details = _published_items([wid])
    collection = _collections([wid])[0]
    is_collection = collection["result"] == 1
    source = {
        "kind": "collection" if is_collection else "item",
        "workshopId": wid,
        "title": details[0].get("title", ""),
    }
    if is_collection:
        children = collection.get("children")
        if not isinstance(children, list):
            raise ValueError("Не удалось прочитать содержимое коллекции Steam")
        if any(not isinstance(child, dict) for child in children):
            raise ValueError("Коллекция содержит некорректные метаданные item")
        ids = [str(r.get("publishedfileid", "")) for r in children]
        if len(ids) > 300:
            raise ValueError("В одной операции поддерживается до 300 Steam items")
        if any(not child.isdigit() or len(child) > 20 for child in ids):
            raise ValueError("Коллекция содержит некорректный Workshop ID")
        ids = list(dict.fromkeys(ids))
        details = _published_items(ids)
        if any(r["result"] == 1 for r in _collections(ids)):
            raise ValueError("Коллекция содержит вложенные коллекции; добавьте их отдельно")
    items = [
        {"workshopId": str(r["publishedfileid"]), "title": r.get("title", "")} for r in details
    ]
    return {"items": items, "source": source} if with_source else items


def validate_items(ids):
    if not ids:
        return
    _published_items(ids)
    if any(r["result"] == 1 for r in _collections(ids)):
        raise ValueError("WorkshopItems содержит коллекцию; выберите пакеты внутри неё")
