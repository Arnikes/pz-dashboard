"""Profile-scoped drafts, optimistic concurrency and recoverable file transactions."""

import difflib
import hashlib
import json
import math
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import config
import configschema
import dockerlib
import ops
import workshop
from configformats import (
    FormatError,
    LuaTable,
    MOD_KEYS,
    SECRET,
    SECRET_KEY,
    edit_ini,
    ini_entries,
    mask_ini,
    mask_lua,
    normalize_mod,
    restore_ini_secrets,
    restore_raw_literals,
    literal_table,
    split_list,
    version_marker,
    preserve_newlines,
)

LOCK = threading.RLock()
_CONTEXT = {}


class EditorError(ops.OpsError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def sync_directory(path):
    """Persist rename/create/unlink entries on the Linux deployment filesystem."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_directory(path):
    missing = []
    candidate = path
    while not candidate.exists():
        missing.append(candidate)
        candidate = candidate.parent
    for candidate in reversed(missing):
        candidate.mkdir(mode=0o700, exist_ok=True)
        sync_directory(candidate.parent)


def durable_unlink(path):
    path.unlink()
    sync_directory(path.parent)


def atomic(path, data, mode=0o600, owner=None):
    durable_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".pz-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.chmod(temporary, mode)
            if hasattr(os, "chown"):
                if owner is None and path.exists():
                    stat = path.stat()
                    owner = (stat.st_uid, stat.st_gid)
                if owner is not None:
                    os.chown(temporary, *owner)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_json(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False, allow_nan=False).encode())


def load_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {} if default is None else default


def state_dir(file):
    profile_paths(file)
    return (
        Path(config.CFG["dashboard_dir"])
        / "config-editor"
        / hashlib.sha256(file.encode()).hexdigest()[:24]
    )


def profile_paths(file):
    if (
        not isinstance(file, str)
        or not file.endswith(".ini")
        or file in (".ini", "..ini")
        or Path(file).name != file
        or "/" in file
        or "\\" in file
        or "\0" in file
    ):
        raise EditorError("Выберите существующий профиль .ini")
    root = Path(config.CFG["data_dir"]) / "Server"
    paths = {"ini": root / file, "sandbox": root / (file[:-4] + "_SandboxVars.lua")}
    for path in paths.values():
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise EditorError("Ссылки и пути вне каталога Server запрещены")
    if not paths["ini"].is_file():
        raise EditorError("Профиль не найден", 404)
    return paths


def read_profile(file):
    paths = profile_paths(file)
    texts = {}
    for kind, path in paths.items():
        if path.exists() and path.stat().st_size > 1_000_000:
            raise EditorError("Файл конфигурации больше 1 МБ")
        try:
            texts[kind] = path.read_bytes().decode("utf-8-sig") if path.exists() else ""
        except UnicodeDecodeError:
            raise EditorError(
                "Конфигурация должна быть в UTF-8; исходный файл не изменён"
            ) from None
    return texts


def revision(texts):
    return hashlib.sha256(
        json.dumps(texts, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


PZ_RESET_COMMENT = re.compile(
    r"^(# Reset ID determines if the server has undergone a soft-reset\. "
    r"If this number does match the client, the client must create a new character\. "
    r"Used in conjunction with PlayerServerID\. It is strongly advised that you backup "
    r"these IDs somewhere Min: 0 Max: 2147483647 Default: )\d+(?=\r?\nResetID=\d+\r?$)",
    re.M,
)


def startup_profile_matches(expected, actual):
    """PZ regenerates this comment's random default without changing ResetID.

    Keep optimistic concurrency byte-exact everywhere else, including user
    comments, secrets and Sandbox. Only the confirmed startup may adopt it.
    """
    if expected == actual:
        return True
    return expected["sandbox"] == actual["sandbox"] and PZ_RESET_COMMENT.sub(
        r"\g<1><generated>", expected["ini"]
    ) == PZ_RESET_COMMENT.sub(r"\g<1><generated>", actual["ini"])


def adopt_startup_comment(text, actual):
    generated = PZ_RESET_COMMENT.search(actual)
    return PZ_RESET_COMMENT.sub(lambda _: generated[0], text) if generated else text


def startup_sandbox_defaults(expected, actual, discovered, selected):
    """Accept PZ's literal serialization and declared selected-mod defaults only."""
    if expected == actual:
        return {}
    before, after = literal_table(expected), literal_table(actual)
    if not before or not after or not before.values.keys() <= after.values.keys():
        return None

    def equal(left, right):
        # Lua number serialization may change 2.0 to 2, but true is never 1.
        return left == right and (
            type(left) is type(right) or type(left) in (int, float) and type(right) in (int, float)
        )

    if any(
        not equal(rec["value"], after.values[path]["value"]) for path, rec in before.values.items()
    ):
        return None
    defaults, ambiguous = {}, set()
    for records in discovered.values():
        for rec in records:
            if rec.get("modId") not in selected or rec.get("metadataStale"):
                continue
            for option in rec.get("options", []):
                if "default" not in option:
                    continue
                path = tuple(option["key"].split("."))
                if path in defaults and not equal(defaults[path], option["default"]):
                    ambiguous.add(path)
                defaults[path] = option["default"]
    added = after.values.keys() - before.values.keys()
    if any(
        path not in defaults
        or path in ambiguous
        or not equal(after.values[path]["value"], defaults[path])
        for path in added
    ):
        return None
    tables = before.tables.keys() | {path[:i] for path in added for i in range(1, len(path))}
    if after.tables.keys() != tables:
        return None
    return {".".join(path): after.values[path]["value"] for path in added}


def remember_mod_order(memory, selected):
    """Reorder active slots while keeping positions of disabled IDs."""
    memory = list(dict.fromkeys(memory))
    known = set(memory).intersection(selected)
    ordered = iter(mid for mid in selected if mid in known)
    return [next(ordered) if mid in known else mid for mid in memory] + [
        mid for mid in selected if mid not in memory
    ]


def shared_data_path(mounts, cache_dirs):
    """Translate DATA_DIR through actual Docker mounts, without exposing Env values."""
    data = Path(config.CFG["data_dir"]).absolute().as_posix()
    physical, writable = data, os.access(data, os.W_OK)
    own_mounts = []
    own = os.getenv("HOSTNAME")
    if own:
        code, output, _ = dockerlib.sh(["docker", "inspect", own], timeout=10)
        if code == 0:
            try:
                own_mounts = json.loads(output)[0].get("Mounts", [])
            except (ValueError, IndexError, TypeError, AttributeError):
                own_mounts = []
            candidates = [
                m
                for m in own_mounts
                if m.get("Source")
                and m.get("Destination")
                and (
                    data == m["Destination"] or data.startswith(m["Destination"].rstrip("/") + "/")
                )
            ]
            if not candidates:
                return None, False
            mount = max(candidates, key=lambda m: len(m["Destination"]))
            physical = posixpath.join(
                mount["Source"], posixpath.relpath(data, mount["Destination"])
            )
            writable = writable and mount.get("RW", False)
    physical = posixpath.normpath(physical.replace("\\", "/"))
    candidates = []
    for mount in mounts:
        source = posixpath.normpath(str(mount.get("Source", "")).replace("\\", "/"))
        destination = mount.get("Destination")
        if not destination or not source or source == ".":
            continue
        if physical == source or physical.startswith(source.rstrip("/") + "/"):
            path = posixpath.normpath(
                posixpath.join(destination, posixpath.relpath(physical, source))
            )
            candidates.append((len(source), path))
    if not candidates:
        return None, False
    path = max(candidates)[1]
    if cache_dirs and set(cache_dirs) != {path}:
        return None, False

    # A bind mount under Server can shadow the shared volume on either side.
    # Verify the directory alias as well as the root volume.
    def physical_directory(container_path, records, fallback):
        covering = [
            m
            for m in records
            if m.get("Source")
            and m.get("Destination")
            and (
                container_path == m["Destination"]
                or container_path.startswith(m["Destination"].rstrip("/") + "/")
            )
        ]
        if not covering:
            return fallback, True
        mount = max(covering, key=lambda m: len(m["Destination"]))
        source = posixpath.normpath(
            posixpath.join(
                mount["Source"], posixpath.relpath(container_path, mount["Destination"])
            ).replace("\\", "/")
        )
        return source, mount.get("RW", False)

    ours, own_rw = physical_directory(data + "/Server", own_mounts, physical + "/Server")
    theirs, _ = physical_directory(path + "/Server", mounts, "")
    if ours != theirs:
        return None, False
    # Also reject file-level shadowing below Server: otherwise some profiles could
    # be written to a different mount despite their parent directory matching.
    if any(
        str(m.get("Destination", "")).startswith(prefix + "/Server/")
        for records, prefix in ((own_mounts, data), (mounts, path))
        for m in records
    ):
        return None, False
    writable = writable and own_rw
    return path, bool(writable)


def context(refresh=False):
    container = config.CFG["pz_container"]
    key = (
        container,
        config.CFG["data_dir"],
        config.CFG["dashboard_dir"],
        os.getenv("PZ_CONFIG_FILE"),
        os.getenv("PZ_VERSION"),
    )
    if not refresh and _CONTEXT.get("key") == key and time.time() - _CONTEXT.get("at", 0) < 15:
        return _CONTEXT["value"]
    code, output, _ = dockerlib.sh(["docker", "inspect", container], timeout=10)
    env, command, mounts, inspect = {}, [], [], {}
    if code == 0:
        try:
            inspect = json.loads(output)[0]
            cfg = inspect.get("Config") or {}
            env = dict(v.split("=", 1) for v in cfg.get("Env", []) if "=" in v)
            command = (cfg.get("Cmd") or []) + (cfg.get("Entrypoint") or [])
            mounts = inspect.get("Mounts") or []
        except (ValueError, TypeError, IndexError, AttributeError):
            code, env, command, mounts, inspect = -1, {}, [], [], {}
    joined = " ".join(command)
    cache_dirs = [
        posixpath.normpath(next(part for part in match if part))
        for match in re.findall(r'-cachedir(?:=|\s+)(?:"([^"]+)"|\'([^\']+)\'|([^\s]+))', joined)
    ]
    server_data, data_writable = (
        shared_data_path(mounts, cache_dirs) if code == 0 else (None, False)
    )
    names = re.findall(r"-servername(?:=|\s+)([\w.-]+)", joined)
    if env.get("SERVER_NAME"):
        names.append(env["SERVER_NAME"])
    names = list(dict.fromkeys(names))
    active = names[0] + ".ini" if len(names) == 1 else None
    configured = os.getenv("PZ_CONFIG_FILE", "")
    if configured and not active:
        active = configured
    hint = os.getenv("PZ_VERSION", "")
    state = inspect.get("State") or {}
    started_at = state.get("StartedAt", "") if isinstance(state, dict) else ""
    if not isinstance(started_at, str):
        started_at = ""
    identity = [container, inspect.get("Id"), inspect.get("Image"), started_at]
    version_path = (
        Path(config.CFG["dashboard_dir"])
        / "game-metadata"
        / (hashlib.sha256(container.encode()).hexdigest() + "-version.json")
    )
    observed = ""
    if code == 0:
        try:
            cached = load_json(version_path)
            if started_at and isinstance(cached, dict) and cached.get("identity") == identity:
                candidate = cached.get("version", "")
                if isinstance(candidate, str) and re.fullmatch(r"\d{2}\.\d+(?:\.\d+)?", candidate):
                    observed = candidate
        except (OSError, ValueError):
            pass
        if not observed:
            observed = dockerlib.container_startup_version(container, started_at) or ""
            if observed:
                try:
                    save_json(version_path, {"identity": identity, "version": observed})
                except OSError:
                    pass
    version = observed or hint
    owners = {}
    mapping = {
        "MAX_PLAYERS": "MaxPlayers",
        "RCON_PASSWORD": "RCONPassword",
        "RCON_PORT": "RCONPort",
        "DEFAULT_PORT": "DefaultPort",
        "UDP_PORT": "UDPPort",
        "MOD_IDS": "Mods",
        "MOD_NAMES": "Mods",
        "MOD_WORKSHOP_IDS": "WorkshopItems",
        "WORKSHOP_IDS": "WorkshopItems",
        "WORKSHOP_ITEMS": "WorkshopItems",
    }
    generates = env.get("GENERATE_SETTINGS", "false").lower() == "true"
    externally_managed_mods = env.get("SELF_MANAGED_MODS", "false").lower() not in ("true", "1")
    for variable, ini_key in mapping.items():
        if variable in env and (
            generates
            or ini_key in ("RCONPassword", "RCONPort", "MaxPlayers", "DefaultPort", "UDPPort")
            or ini_key in MOD_KEYS
            and externally_managed_mods
        ):
            owners[ini_key] = variable
    value = {
        "activeFile": active,
        "version": version,
        "versionSource": "startup-log" if observed else "configured" if hint else "unknown",
        "versionDiagnostic": f"PZ_VERSION={hint} не совпадает с версией сервера {observed}; используется версия из запуска PZ"
        if observed and hint and hint != observed
        else None,
        "versionKnown": bool(re.fullmatch(r"42\.\d+(?:\.\d+)?", version)),
        "owners": owners,
        "dockerAvailable": code == 0,
        "generatesSettings": generates,
        "mountsKnown": server_data is not None,
        "serverDataPath": server_data,
        "dataWritable": data_writable,
        "dataDiagnostic": None
        if server_data and data_writable
        else "Не подтверждён общий каталог данных PZ и пульта или доступ к записи. Проверьте монтирование DATA_DIR и -cachedir",
    }
    _CONTEXT.update(key=key, at=time.time(), value=value)
    return value


def profiles():
    ctx = context()
    files = ops.list_server_inis()
    items = []
    for file in files:
        try:
            paths = profile_paths(file)
            state = load_json(state_dir(file) / "state.json")
            items.append(
                {
                    "file": file,
                    "active": file == ctx["activeFile"],
                    "sandboxExists": paths["sandbox"].exists(),
                    "writable": os.access(paths["ini"], os.W_OK),
                    "status": state.get("status", "unconfirmed"),
                    "installation": state.get("installation"),
                }
            )
        except EditorError:
            continue
    return {"ok": True, "profiles": items, **ctx}


def choose(file):
    if file:
        profile_paths(file)
        return file
    data = profiles()
    if data["activeFile"] in [p["file"] for p in data["profiles"]]:
        return data["activeFile"]
    if len(data["profiles"]) == 1:
        return data["profiles"][0]["file"]
    raise EditorError("Выберите профиль: активная конфигурация не определена", 409)


def recover(file, active=False):
    root = state_dir(file)
    journal = root / "transaction.json"
    if not journal.exists():
        return
    if ops.op_busy() and not active:
        raise EditorError("Идёт запись конфигурации: дождитесь завершения операции", 409)
    if confirmed_container()["running"] or ops.is_running():
        raise EditorError("Незавершённая запись: остановите сервер для восстановления файлов", 409)
    rec = load_json(journal)
    paths = profile_paths(file)
    backup = root / "history" / rec["historyId"]
    for kind, path in paths.items():
        saved = backup / kind
        if saved.exists():
            atomic(
                path,
                saved.read_bytes(),
                rec["modes"].get(kind, 0o600),
                owner=rec.get("owners", {}).get(kind),
            )
        elif kind == "sandbox" and path.exists():
            durable_unlink(path)
    durable_unlink(journal)


def draft(file=None):
    file = choose(file)
    with LOCK:
        recover(file)
        root = state_dir(file)
        state = load_json(root / "state.json")
        if state.get("status") == "applying" and not ops.op_busy():
            state.update(
                status="error",
                error="Операция прервана перезапуском пульта. Проверьте сервер и повторите применение или восстановите конфигурацию",
            )
            save_json(root / "state.json", state)
        current = read_profile(file)
        saved = load_json(root / "draft.json")
        if not saved:
            saved = {
                "file": file,
                "baseRevision": revision(current),
                "base": current,
                "texts": current.copy(),
                "draftRevision": uuid.uuid4().hex,
                "modOrder": split_list(
                    ini_entries(current["ini"]).get("Mods", {}).get("value", ""), mods=True
                ),
            }
            save_json(root / "draft.json", saved)
        return response(saved, current)


def mod_state(file, texts=None, refresh=False):
    file = choose(file)
    texts = texts or read_profile(file)
    entries = ini_entries(texts["ini"])
    items = split_list(entries.get("WorkshopItems", {}).get("value", ""))
    selected = split_list(entries.get("Mods", {}).get("value", ""), mods=True)
    version = context()["version"]
    index = workshop.scan(items, version, refresh)
    titles = ops._ws_titles(items)
    issues = workshop.problems(index, selected, version)
    packets = [
        {
            "workshopId": wid,
            "title": titles.get(wid, wid),
            "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}",
            "mods": [r["modId"] for r in index.get(wid, []) if r.get("modId")],
            "available": index.get(wid, []),
            "selected": [
                mid for mid in selected if any(r.get("modId") == mid for r in index.get(wid, []))
            ],
            "status": "downloaded" if index.get(wid) else "pending",
        }
        for wid in items
    ]
    return {
        "ok": True,
        "file": file,
        "files": ops.list_server_inis(),
        "workshop": packets,
        "mods": selected,
        "maps": split_list(entries.get("Map", {}).get("value", "")),
        "problems": issues,
        "paired": False,
        "pairs": [],
        "mappingSource": "metadata",
        "canManage": True,
        "unbound": [p["modId"] for p in issues if p["code"] == "unknown"],
        "installation": load_json(state_dir(file) / "state.json").get("installation"),
    }


def mod_response(file, draft_mode=False, refresh=False):
    file = choose(file)
    with LOCK:
        saved = load_json(state_dir(file) / "draft.json")
        texts = saved.get("texts") if draft_mode else None
        return {
            **mod_state(file, texts, refresh),
            "draftRevision": saved.get("draftRevision"),
            "baseRevision": saved.get("baseRevision"),
            "currentRevision": revision(read_profile(file)),
        }


def response(saved, current):
    ctx = context()
    owners = ctx["owners"] if ctx["activeFile"] in (None, saved["file"]) else {}
    texts = saved["texts"]
    schema = configschema.catalog(saved["base"]["ini"], saved["base"]["sandbox"], ctx["version"])
    translations = workshop.vanilla_translations(ctx["version"], ctx.get("dockerAvailable", False))
    fields = [
        configschema.field(key, rec["value"], schema=schema)
        for key, rec in ini_entries(texts["ini"]).items()
        if key not in MOD_KEYS
    ]
    for rec in fields:
        rec["secret"] = bool(SECRET_KEY.search(rec["key"]))
        if rec["secret"]:
            rec["value"] = SECRET
        rec["owner"] = owners.get(rec["key"])
    sandbox_fields, diagnostic = [], None
    masked_lua = mask_lua(texts["sandbox"]) if texts["sandbox"] else ""
    try:
        table = LuaTable(texts["sandbox"]) if texts["sandbox"] else None
        if table:
            for path, entry in table.values.items():
                key = ".".join(path)
                if key == "VERSION":
                    continue
                rec = configschema.field(
                    key, entry["value"], sandbox=True, schema=schema, translations=translations
                )
                rec["secret"] = bool(SECRET_KEY.search(key))
                if rec["secret"]:
                    rec["value"] = SECRET
                sandbox_fields.append(rec)
    except FormatError as error:
        diagnostic = str(error)
    state = load_json(state_dir(saved["file"]) / "state.json")
    changed = revision(saved["base"]) != revision(texts)
    changed_lines = sum(
        max(j2 - j1, i2 - i1)
        for kind in ("ini", "sandbox")
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, saved["base"][kind].splitlines(), texts[kind].splitlines(), autojunk=False
        ).get_opcodes()
        if tag != "equal"
    )
    operation_status = state.get("status", "unconfirmed")
    status = (
        operation_status if operation_status in ("applying", "error") or not changed else "draft"
    )
    current_rev = revision(current)
    if (
        status not in ("applying", "error")
        and state.get("savedRevision")
        and state["savedRevision"] != current_rev
    ):
        status = "unconfirmed" if not changed else "draft"
    if status == "applied" and state.get("startedAt") != (ops.container_state() or {}).get(
        "startedAt"
    ):
        status = "unconfirmed"
    legacy = ops.get_settings().get("modsDisabled", {})
    legacy_records = [
        {"workshopId": wid, "title": rec.get("title", ""), "modIds": rec.get("modIds", [])}
        for wid, rec in legacy.items()
        if isinstance(rec, dict)
    ]
    return {
        "ok": True,
        "file": saved["file"],
        "baseRevision": saved["baseRevision"],
        "draftRevision": saved["draftRevision"],
        "currentRevision": current_rev,
        "conflict": current_rev != saved["baseRevision"],
        "changed": changed,
        "changedLines": changed_lines,
        "texts": {"ini": mask_ini(texts["ini"]), "sandbox": masked_lua},
        "fields": fields,
        "sandboxFields": sandbox_fields,
        "sandboxDiagnostic": diagnostic,
        "schema": {"id": schema["id"], "version": schema["version"], "source": schema["source"]},
        "status": status,
        "state": {k: v for k, v in state.items() if k != "snapshots"},
        "canApply": ctx["activeFile"] == saved["file"],
        "canWrite": ctx["mountsKnown"]
        and ctx["dockerAvailable"]
        and ctx.get("dataWritable", True)
        and all(
            os.access(path if path.exists() else path.parent, os.W_OK)
            for path in profile_paths(saved["file"]).values()
        ),
        "legacyDisabled": legacy_records,
        **ctx,
    }


def patch(data):
    if ops.op_busy():
        raise EditorError("Дождитесь завершения операции перед изменением черновика", 409)
    file = choose(data.get("file"))
    with LOCK:
        if ops.op_busy():
            raise EditorError("Дождитесь завершения операции перед изменением черновика", 409)
        draft(file)
        root = state_dir(file)
        saved = load_json(root / "draft.json")
        if data.get("draftRevision") != saved["draftRevision"]:
            raise EditorError("Черновик изменён другой вкладкой. Загрузите его заново", 409)
        current = read_profile(file)
        if revision(current) != saved["baseRevision"] and not data.get("discard"):
            raise EditorError(
                "Рабочие файлы изменились. Сначала просмотрите конфликт или отмените черновик", 409
            )
        if data.get("discard") is True:
            saved.update(base=current, texts=current.copy(), baseRevision=revision(current))
        else:
            texts = saved["texts"].copy()
            sources = data.get("texts", {})
            if not isinstance(sources, dict):
                raise EditorError("texts должен быть объектом")
            for kind in ("ini", "sandbox"):
                if kind not in sources:
                    continue
                text = sources[kind]
                if not isinstance(text, str) or len(text.encode()) > 1_000_000:
                    raise EditorError("Неверный размер или тип исходника")
                text = preserve_newlines(texts[kind], text)
                if kind == "ini":
                    texts[kind] = restore_ini_secrets(text, texts[kind])
                else:
                    if not literal_table(texts[kind]) and texts[kind]:
                        raw = restore_raw_literals(text, texts[kind])
                        if raw != texts[kind] and not literal_table(raw):
                            syntax_check(raw)
                        texts[kind] = raw
                        continue
                    table = LuaTable(text)
                    old = LuaTable(texts[kind]) if texts[kind] else None
                    restore = {
                        ".".join(p): old.values[p]["value"]
                        for p, rec in table.values.items()
                        if rec["value"] == SECRET
                        and SECRET_KEY.search(".".join(p))
                        and old
                        and p in old.values
                    }
                    texts[kind] = table.edit(restore) if restore else text
            for kind in ("ini", "sandbox"):
                changes = data.get(kind, {})
                if not isinstance(changes, dict):
                    raise EditorError("Изменения полей должны быть объектом")
                changes = {k: v for k, v in changes.items() if v != SECRET}
                if kind == "ini":
                    changes = {
                        k: (str(v).lower() if isinstance(v, bool) else v)
                        for k, v in changes.items()
                    }
                    texts[kind] = edit_ini(texts[kind], changes)
                elif changes or data.get("removeSandbox"):
                    texts[kind] = LuaTable(texts[kind]).edit(changes, data.get("removeSandbox", []))
            if "mods" in data:
                selection = data["mods"]
                if not isinstance(selection, dict):
                    raise EditorError("mods должен быть объектом")
                if type(selection.get("preserveOrder", False)) is not bool:
                    raise EditorError("preserveOrder должен быть boolean")
                edits = {}
                for field, ini_key in (
                    ("items", "WorkshopItems"),
                    ("selected", "Mods"),
                    ("maps", "Map"),
                ):
                    if field not in selection:
                        continue
                    values = selection[field]
                    if not isinstance(values, list) or any(
                        not isinstance(v, str) or not v.strip() or any(c in v for c in ";\n\r\0")
                        for v in values
                    ):
                        raise EditorError("Неверный список модов или карт")
                    if field == "items" and any(not v.isdigit() or len(v) > 20 for v in values):
                        raise EditorError("Workshop ID должен быть числом")
                    values = [
                        normalize_mod(v) if field == "selected" else v.strip() for v in values
                    ]
                    if (
                        any(not v for v in values)
                        or len(values) > 300
                        or len(set(values)) != len(values)
                    ):
                        raise EditorError("Список содержит повторы или более 300 записей")
                    if field == "selected" and selection.get("preserveOrder"):
                        memory = saved.get("modOrder") or split_list(
                            ini_entries(saved["texts"]["ini"]).get("Mods", {}).get("value", ""),
                            mods=True,
                        )
                        values = [mid for mid in memory if mid in values] + [
                            mid for mid in values if mid not in memory
                        ]
                    edits[ini_key] = ";".join(
                        "\\" + normalize_mod(v) if field == "selected" else v for v in values
                    )
                texts["ini"] = edit_ini(texts["ini"], edits)
            saved["texts"] = texts
        selected = split_list(
            ini_entries(saved["texts"]["ini"]).get("Mods", {}).get("value", ""), mods=True
        )
        memory = saved.get("modOrder") or split_list(
            ini_entries(saved["base"]["ini"]).get("Mods", {}).get("value", ""), mods=True
        )
        saved["modOrder"] = remember_mod_order(memory, selected)
        saved["draftRevision"] = uuid.uuid4().hex
        save_json(root / "draft.json", saved)
        return response(saved, current)


def preparation_texts(saved):
    """Stage one only adds Steam packages to the saved configuration."""
    base, desired = saved["base"], saved["texts"]
    old_items = split_list(ini_entries(base["ini"]).get("WorkshopItems", {}).get("value", ""))
    target = split_list(ini_entries(desired["ini"]).get("WorkshopItems", {}).get("value", ""))
    items = list(dict.fromkeys(old_items + target))
    return {**base, "ini": edit_ini(base["ini"], {"WorkshopItems": ";".join(items)})}


def validate(file, include_mods=True, prepare=False, draft_revision=None):
    file = choose(file)
    saved = load_json(state_dir(file) / "draft.json")
    if not saved:
        draft(file)
        saved = load_json(state_dir(file) / "draft.json")
    if draft_revision is not None and draft_revision != saved["draftRevision"]:
        raise EditorError("Черновик изменён другой вкладкой: загрузите актуальную ревизию", 409)
    if type(prepare) is not bool:
        raise EditorError("prepare должен быть boolean")
    texts = preparation_texts(saved) if prepare else saved["texts"]
    base = saved["base"]
    errors, warnings = [], []
    ctx = context()
    managed = ctx["activeFile"] in (None, file)
    owners = ctx["owners"] if managed else {}
    schema = configschema.catalog(base["ini"], base["sandbox"], ctx["version"])
    if ctx["dockerAvailable"] and (not ctx["mountsKnown"] or not ctx.get("dataWritable", True)):
        errors.append(
            {
                "message": ctx.get("dataDiagnostic")
                or "Проверьте общий каталог данных и права записи"
            }
        )
    if ctx["version"] and not str(ctx["version"]).startswith("42."):
        errors.append({"message": "Редактор поддерживает только B42; проверьте версию сервера"})
    old, new = ini_entries(base["ini"]), ini_entries(texts["ini"])
    for key, rec in new.items():
        if key in owners and rec["value"] != old.get(key, {}).get("value"):
            errors.append({"key": key, "message": f"{key} перезаписывается из {owners[key]}"})
        field = configschema.field(key, rec["value"], schema=schema)
        destination = errors if rec["value"] != old.get(key, {}).get("value") else warnings
        check_field(field, rec["value"], destination)
    for key in owners:
        if key in old and key not in new:
            errors.append({"key": key, "message": f"{key} управляется окружением"})
    if managed and ctx["generatesSettings"] and texts != base:
        errors.append(
            {
                "message": "GENERATE_SETTINGS=true: образ может перезаписать конфигурацию. Отключите генерацию в Compose"
            }
        )
    if base["sandbox"] and version_marker(texts["sandbox"]) != version_marker(base["sandbox"]):
        errors.append({"key": "VERSION", "message": "Изменять VERSION нельзя"})
    if texts["sandbox"]:
        try:
            lua = LuaTable(texts["sandbox"])
            old_lua = None
            if base["sandbox"]:
                old_lua = literal_table(base["sandbox"])
            if old_lua:
                if lua.values.get(("VERSION",), {}).get("value") != old_lua.values.get(
                    ("VERSION",), {}
                ).get("value"):
                    errors.append({"key": "VERSION", "message": "Изменять VERSION нельзя"})
            for path, entry in lua.values.items():
                destination = (
                    errors
                    if not old_lua or entry["value"] != old_lua.values.get(path, {}).get("value")
                    else warnings
                )
                check_field(
                    configschema.field(".".join(path), entry["value"], sandbox=True, schema=schema),
                    entry["value"],
                    destination,
                )
        except FormatError:
            if texts["sandbox"] != base["sandbox"]:
                try:
                    syntax_check(texts["sandbox"])
                except EditorError as syntax_error:
                    errors.append({"message": str(syntax_error)})
            else:
                warnings.append(
                    {"message": "Sandbox содержит неподдерживаемый Lua и сохранится без изменений"}
                )
    if revision(read_profile(file)) != saved["baseRevision"]:
        errors.append({"message": "Рабочие файлы изменились: конфликт ревизии"})
    mod_changes = any(new.get(k, {}).get("value") != old.get(k, {}).get("value") for k in MOD_KEYS)
    if include_mods and not prepare:
        mods = mod_state(file, texts)
        base_selected = set(split_list(old.get("Mods", {}).get("value", ""), mods=True))
        for issue in mods["problems"]:
            if (
                issue["severity"] == "error"
                or issue.get("code") == "unknown"
                and issue.get("modId") not in base_selected
            ):
                errors.append(issue)
            else:
                warnings.append(issue)
        custom = {
            option["key"]: option
            for packet in mods["workshop"]
            for rec in packet["available"]
            if rec.get("modId") in mods["mods"]
            for option in rec.get("options", [])
        }
        if texts["sandbox"] and literal_table(texts["sandbox"]):
            for path, entry in LuaTable(texts["sandbox"]).values.items():
                key = ".".join(path)
                if key in custom:
                    check_field(
                        configschema.field(key, entry["value"], sandbox=True, custom=custom[key]),
                        entry["value"],
                        errors,
                    )
    if texts != base and not ctx["versionKnown"]:
        errors.append(
            {
                "message": "Версия B42 неизвестна: укажите PZ_VERSION или дождитесь определения из логов"
            }
        )
    before, after = {}, {}
    for kind in ("ini", "sandbox"):
        before[kind] = mask_ini(base[kind]) if kind == "ini" else mask_lua(base[kind])
        after[kind] = mask_ini(texts[kind]) if kind == "ini" else mask_lua(texts[kind])
    diffs = {
        kind: "".join(
            difflib.unified_diff(
                (before[kind] or "").splitlines(True),
                (after[kind] or "").splitlines(True),
                fromfile="До",
                tofile="После",
            )
        )
        for kind in before
    }
    current = read_profile(file)
    conflict_diff = {}
    if revision(current) != saved["baseRevision"]:
        for kind in before:
            latest = mask_ini(current[kind]) if kind == "ini" else mask_lua(current[kind])
            conflict_diff[kind] = "".join(
                difflib.unified_diff(
                    (before[kind] or "").splitlines(True),
                    (latest or "").splitlines(True),
                    fromfile="Исходная ревизия",
                    tofile="Сейчас на диске",
                )
            )
    return {
        "ok": True,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "diff": diffs,
        "conflictDiff": conflict_diff,
        "modChanges": mod_changes,
        "changed": texts != base,
        "draftRevision": saved["draftRevision"],
        "prepare": prepare,
    }


def syntax_check(text):
    compiler = next(
        (shutil.which(name) for name in ("luac5.1", "luac-5.1", "luac") if shutil.which(name)), None
    )
    if not compiler:
        raise EditorError(
            "Для исходника с выражениями нужен синтаксический проверяющий luac (включён в Docker-образ пульта)"
        )
    fd, path = tempfile.mkstemp(suffix=".lua")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        result = subprocess.run(
            [compiler, "-p", path], capture_output=True, timeout=10, check=False
        )
        if result.returncode:
            raise EditorError("Ошибка синтаксиса Lua. Исправьте исходник перед сохранением")
    except subprocess.TimeoutExpired:
        raise EditorError("Проверка синтаксиса Lua превысила время ожидания") from None
    finally:
        os.unlink(path)


def check_field(field, value, errors):
    kind = field["type"]
    if kind == "boolean":
        if not isinstance(value, bool) and value not in ("true", "false"):
            errors.append({"key": field["key"], "message": "Нужно true или false"})
    elif kind in ("integer", "double", "enum"):
        try:
            if isinstance(value, bool):
                raise ValueError
            number = float(value)
            if not math.isfinite(number):
                raise ValueError
            if kind in ("integer", "enum") and not number.is_integer():
                raise ValueError
            if "min" in field and number < field["min"] or "max" in field and number > field["max"]:
                raise ValueError
            if field.get("choices") and number not in [v["value"] for v in field["choices"]]:
                raise ValueError
        except (TypeError, ValueError):
            errors.append({"key": field["key"], "message": "Число вне допустимого диапазона"})
    elif kind in ("string", "multiline", "list") and not isinstance(value, str):
        errors.append({"key": field["key"], "message": "Нужен текст"})


def confirmed_container():
    state = ops.container_state()
    if not state or type(state.get("running")) is not bool:
        raise EditorError("Состояние сервера неизвестно: проверьте доступ к Docker", 409)
    return state


def commit(file, texts, reason, expected=None):
    """Single operation slot owns writes; recovery journal survives crashes."""
    if confirmed_container()["running"] or ops.is_running():
        raise EditorError("Сначала остановите сервер", 409)
    ctx = context(refresh=True)
    if not ctx["mountsKnown"] or not ctx.get("dataWritable", True):
        raise EditorError(
            ctx.get("dataDiagnostic") or "Не подтверждён общий каталог данных сервера", 409
        )
    root, paths = state_dir(file), profile_paths(file)
    current = read_profile(file)
    if expected is not None and revision(current) != expected:
        raise EditorError("Файлы изменились перед записью: конфликт ревизии", 409)
    hid = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    backup = root / "history" / hid
    durable_directory(backup)
    modes, owners = {}, {}
    for kind, path in paths.items():
        if path.exists():
            stat = path.stat()
            modes[kind] = stat.st_mode & 0o777
            owners[kind] = (stat.st_uid, stat.st_gid)
            atomic(backup / kind, path.read_bytes())
    save_json(
        backup / "record.json",
        {"id": hid, "at": ops.now_iso(), "reason": reason, "revision": revision(current)},
    )
    journal = root / "transaction.json"
    transaction = {"historyId": hid, "modes": modes, "owners": owners}
    save_json(journal, transaction)
    try:
        for kind, path in paths.items():
            if texts[kind] != current[kind]:
                encoding = (
                    "utf-8-sig"
                    if path.exists() and path.read_bytes().startswith(b"\xef\xbb\xbf")
                    else "utf-8"
                )
                atomic(
                    path,
                    texts[kind].encode(encoding),
                    modes.get(kind, modes["ini"]),
                    owner=owners["ini"] if not path.exists() else None,
                )
        durable_unlink(journal)
    except OSError:
        # Unlink can succeed before its directory sync fails. Retain a recovery
        # marker until the original pair has been durably restored.
        if not journal.exists():
            save_json(journal, transaction)
        recover(file, active=True)
        raise EditorError("Запись не удалась. Исходная конфигурация восстановлена", 500) from None
    return hid


def history(file):
    root = state_dir(choose(file)) / "history"
    return {
        "ok": True,
        "items": [
            load_json(p / "record.json") for p in sorted(root.glob("*"), reverse=True) if p.is_dir()
        ][:50],
    }


def restore_history(data):
    if ops.op_busy():
        raise EditorError("Дождитесь завершения операции", 409)
    file = choose(data.get("file"))
    hid = data.get("historyId", "")
    if not isinstance(hid, str) or not re.fullmatch(r"\d{8}-\d{6}-[a-f0-9]{8}", hid):
        raise EditorError("Некорректная версия истории")
    with LOCK:
        if ops.op_busy():
            raise EditorError("Дождитесь завершения операции", 409)
        draft(file)
        saved = load_json(state_dir(file) / "draft.json")
        if saved["draftRevision"] != data.get("draftRevision"):
            raise EditorError("Черновик изменился", 409)
        folder = state_dir(file) / "history" / hid
        if not folder.is_dir():
            raise EditorError("Версия не найдена", 404)
        texts = {
            kind: (folder / kind).read_bytes().decode("utf-8-sig")
            if (folder / kind).exists()
            else ""
            for kind in ("ini", "sandbox")
        }
        current = read_profile(file)
        saved.update(
            texts=texts,
            base=current,
            baseRevision=revision(current),
            draftRevision=uuid.uuid4().hex,
        )
        saved["modOrder"] = remember_mod_order(
            saved.get("modOrder", []),
            split_list(ini_entries(texts["ini"]).get("Mods", {}).get("value", ""), mods=True),
        )
        save_json(state_dir(file) / "draft.json", saved)
        return response(saved, read_profile(file))


def wait_ready(timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ops.is_running():
            try:
                reply = ops.rcon("players", quiet=True)
                if isinstance(reply, str) and re.search(
                    r"\bPlayers\s+connected\s*\(\d+\)", reply, re.I
                ):
                    return
            except ops.rconlib.RCONError:
                pass
        time.sleep(3)
    raise EditorError(
        "PZ/RCON не готов после запуска. Проверьте логи; автоматического повторного рестарта не будет",
        500,
    )


def run(data, prepare=False):
    confirmed_container()
    file = choose(data.get("file"))
    saved = load_json(state_dir(file) / "draft.json")
    if not saved or saved["draftRevision"] != data.get("draftRevision"):
        raise EditorError("Черновик изменился", 409)
    result = validate(file, prepare=prepare, draft_revision=data["draftRevision"])
    if not result["valid"]:
        raise EditorError("; ".join(e["message"] for e in result["errors"]))
    restart = data.get("restart") is True or prepare
    if restart and context(refresh=True)["activeFile"] != file:
        raise EditorError("Рестарт доступен только для подтверждённого активного профиля", 409)
    if not restart and ops.is_running():
        raise EditorError("Сохранение доступно после остановки сервера", 409)
    texts = saved["texts"].copy()
    current = read_profile(file)
    if prepare:
        current_items = split_list(
            ini_entries(current["ini"]).get("WorkshopItems", {}).get("value", "")
        )
        target_items = split_list(
            ini_entries(texts["ini"]).get("WorkshopItems", {}).get("value", "")
        )
        items = list(dict.fromkeys(current_items + target_items))
        texts = preparation_texts(saved)
        if texts == current and not load_json(state_dir(file) / "state.json").get("installation"):
            raise EditorError("Нет новых Workshop items для загрузки")
    before_mods = ini_entries(texts["ini"])
    selected = split_list(before_mods.get("Mods", {}).get("value", ""), mods=True)
    actual_items = split_list(before_mods.get("WorkshopItems", {}).get("value", ""))
    known_ids = {
        rec["modId"]
        for records in workshop.scan(actual_items, context()["version"]).values()
        for rec in records
        if rec.get("modId")
    }
    root = state_dir(file)
    state = load_json(root / "state.json")
    prior_installation = state.get("installation")
    state.pop("error", None)
    state.pop("verificationProblems", None)
    state.pop("worldBackup", None)
    state.pop("historyId", None)
    state.pop("operationCompletedAt", None)
    state.update(status="applying", operationStartedAt=ops.now_iso())
    save_json(root / "state.json", state)
    stopped = False
    committed = False
    try:
        warn = data.get("warnSeconds", 300)
        if not isinstance(warn, int) or isinstance(warn, bool) or not 0 <= warn <= 3600:
            raise EditorError("Предупреждение должно быть от 0 до 3600 секунд")
        if ops.is_running():
            if warn:
                ops._set_phase("Предупреждение игроков", "Применение конфигурации")
                ops.rcon_warn_broadcast(warn, "Изменение конфигурации")
            ops._set_phase("Остановка", "Сохранение мира")
            if ops.graceful_stop() != "stopped":
                raise EditorError("Сервер сам перезапустился; запись отменена", 409)
            stopped = True
        if revision(read_profile(file)) != saved["baseRevision"]:
            raise EditorError("Конфигурация изменилась во время остановки; запись отменена", 409)
        if result["modChanges"] or prepare:
            ops._set_phase("Бэкап", "Резервная копия мира перед изменением модов")
            backup = ops.run_backup_job("manual", False)
            if isinstance(backup, dict) and backup.get("name"):
                state["worldBackup"] = backup["name"]
                save_json(root / "state.json", state)
        if revision(read_profile(file)) != saved["baseRevision"]:
            raise EditorError("Файлы изменились во время бэкапа; запись отменена", 409)
        hid = commit(
            file,
            texts,
            "Загрузка Workshop" if prepare else "Применение конфигурации",
            expected=saved["baseRevision"],
        )
        committed = True
        state.update(
            status="applying" if restart else "saved", savedRevision=revision(texts), historyId=hid
        )
        if prepare:
            state["installation"] = {
                "stage": "downloading",
                "previousItems": prior_installation["previousItems"]
                if prior_installation
                else current_items,
                "targetItems": target_items,
                "addedItems": list(
                    dict.fromkeys(
                        (prior_installation.get("addedItems", []) if prior_installation else [])
                        + [w for w in items if w not in current_items]
                    )
                ),
            }
        save_json(root / "state.json", state)
        if prepare:
            saved["texts"]["ini"] = edit_ini(
                saved["texts"]["ini"], {"WorkshopItems": ";".join(items)}
            )
            saved.update(
                base=texts.copy(), baseRevision=revision(texts), draftRevision=uuid.uuid4().hex
            )
            save_json(root / "draft.json", saved)
        else:
            # A committed pair is the draft's new base even if starting PZ later fails.
            # The previous pair remains available in history for explicit recovery.
            saved.update(
                base=texts.copy(),
                texts=texts.copy(),
                baseRevision=revision(texts),
                draftRevision=uuid.uuid4().hex,
            )
            save_json(root / "draft.json", saved)
        if restart:
            ops._set_phase(
                "Загрузка пакетов" if prepare else "Запуск",
                "Ожидание готовности PZ и RCON; подробности в логах сервера",
            )
            code, _, _ = dockerlib.container_start(config.CFG["pz_container"])
            if code != 0:
                raise EditorError("Не удалось запустить контейнер", 500)
            wait_ready()
            started_texts = read_profile(file)
            if not startup_profile_matches(
                {**texts, "sandbox": started_texts["sandbox"]}, started_texts
            ):
                raise EditorError(
                    "Конфигурация изменилась при запуске PZ. Проверьте перезапись настроек образом; применение не подтверждено",
                    409,
                )
            workshop.invalidate()
            started_context = context(refresh=True)
            version = started_context["version"]
            if not started_context["versionKnown"]:
                raise EditorError(
                    "После запуска не подтверждена поддерживаемая версия B42; применение не подтверждено",
                    409,
                )
            discovered = workshop.scan(actual_items, version, refresh=True)
            added_defaults = startup_sandbox_defaults(
                texts["sandbox"], started_texts["sandbox"], discovered, selected
            )
            if added_defaults is None:
                raise EditorError(
                    "SandboxVars изменился при запуске PZ: значения, неизвестные параметры или структура не совпадают; применение не подтверждено",
                    409,
                )
            normalized_sandbox = texts["sandbox"] != started_texts["sandbox"]
            if started_texts != texts:
                if prepare:
                    saved["texts"]["ini"] = adopt_startup_comment(
                        saved["texts"]["ini"], started_texts["ini"]
                    )
                    if added_defaults:
                        pending = literal_table(saved["texts"]["sandbox"])
                        if not pending:
                            raise EditorError(
                                "Добавлены Sandbox-параметры PZ: объедините их с исходником черновика вручную",
                                409,
                            )
                        saved["texts"]["sandbox"] = pending.edit(
                            {
                                key: value
                                for key, value in added_defaults.items()
                                if tuple(key.split(".")) not in pending.values
                            }
                        )
                else:
                    saved["texts"] = started_texts.copy()
                texts = started_texts
                saved.update(base=texts.copy(), baseRevision=revision(texts))
                state["savedRevision"] = revision(texts)
            if prepare:
                manifest = workshop.download_manifest(state["installation"]["addedItems"])
                if manifest is not None and not all(manifest.values()):
                    raise EditorError(
                        "Steam-манифест не подтверждает установку всех пакетов. Проверьте загрузку и повторите операцию",
                        500,
                    )
                state["installation"]["manifestVerified"] = manifest is not None
                if any(
                    not discovered.get(w)
                    or not any(
                        r.get("modId") and r.get("compatible") is not False for r in discovered[w]
                    )
                    or any(r.get("metadataStale") for r in discovered[w])
                    for w in state["installation"]["addedItems"]
                ):
                    raise EditorError(
                        "Не все пакеты содержат доступные метаданные B42. Проверьте загрузку и логи",
                        500,
                    )
            # Steam can update packages during either stage. Check the active
            # selection, not the draft selection awaiting stage two.
            issues = workshop.problems(discovered, selected, version)
            if normalized_sandbox:
                issues.append(
                    {
                        "severity": "info",
                        "code": "serialization",
                        "message": "PZ переписал оформление SandboxVars; заданные значения сохранены. "
                        f"Добавлено значений по умолчанию выбранных модов: {len(added_defaults)}. Исходная версия доступна в истории.",
                    }
                )
            lua = literal_table(texts["sandbox"])
            if lua:
                for records in discovered.values():
                    for rec in records:
                        if rec.get("modId") not in selected:
                            continue
                        for option in rec.get("options", []):
                            entry = lua.values.get(tuple(option["key"].split(".")))
                            if entry:
                                option_errors = []
                                check_field(option, entry["value"], option_errors)
                                issues.extend(
                                    dict(
                                        error, severity="error", code="sandbox", modId=rec["modId"]
                                    )
                                    for error in option_errors
                                )
            state["verificationProblems"] = issues
            failures = [
                issue
                for issue in issues
                if issue["severity"] == "error"
                or issue["code"] == "unknown"
                and issue.get("modId") in known_ids
            ]
            if failures:
                raise EditorError(
                    "Steam-содержимое изменилось при запуске; применение не подтверждено: "
                    + "; ".join(issue["message"] for issue in failures),
                    409,
                )
            if prepare:
                state["installation"]["stage"] = "select-mods"
            else:
                state.pop("installation", None)
            verified = not any(issue["code"] in ("unknown", "cached") for issue in issues)
            state.update(status="applied" if verified else "unconfirmed")
            if verified:
                state.update(
                    appliedRevision=revision(texts),
                    startedAt=(ops.container_state() or {}).get("startedAt"),
                )
            if prepare:
                state["status"] = "select-mods"
        state["operationCompletedAt"] = ops.now_iso()
        save_json(root / "draft.json", saved)
        save_json(root / "state.json", state)
        outcome = "пакеты загружены, выберите ModID" if prepare else "сохранена"
        if restart and not prepare:
            outcome += (
                " и применена"
                if state["status"] == "applied"
                else "; проверка состава не подтверждена"
            )
        ops.log_event("mods", f"Конфигурация {file}: {outcome}")
        ops._set_phase(
            "Готово", "Выберите ModID для второй стадии" if prepare else "Конфигурация сохранена"
        )
    except (ops.OpsError, OSError) as error:
        state.update(status="error", error=str(error), operationCompletedAt=ops.now_iso())
        if prepare and state.get("installation"):
            state["installation"]["stage"] = "error"
        save_json(root / "state.json", state)
        # Failed pre-write checks should not leave a previously running server stopped.
        if (
            stopped
            and not committed
            and not ops.is_running()
            and not (root / "transaction.json").exists()
            and revision(read_profile(file)) == saved["baseRevision"]
        ):
            dockerlib.container_start(config.CFG["pz_container"])
        raise


def queue(data, prepare=False):
    # Fail synchronously before claiming the single operation slot.
    confirmed_container()
    file = choose(data.get("file"))
    if not isinstance(data.get("restart", False), bool):
        raise EditorError("restart должен быть boolean")
    with LOCK:
        saved = load_json(state_dir(file) / "draft.json")
        if not saved or data.get("draftRevision") != saved["draftRevision"]:
            raise EditorError("Черновик изменился", 409)
        previous_items = split_list(
            ini_entries(saved["base"]["ini"]).get("WorkshopItems", {}).get("value", "")
        )
        new_items = split_list(
            ini_entries(saved["texts"]["ini"]).get("WorkshopItems", {}).get("value", "")
        )
        workshop.validate_items([wid for wid in new_items if wid not in previous_items])
        result = validate(file, prepare=prepare, draft_revision=data["draftRevision"])
        if not result["valid"]:
            raise EditorError("; ".join(e["message"] for e in result["errors"]))
        if (prepare or data.get("restart")) and context()["activeFile"] != file:
            raise EditorError("Выбран неактивный или неподтверждённый профиль", 409)
        if not prepare and not data.get("restart") and ops.is_running():
            raise EditorError("Сначала остановите сервер", 409)
    operation = "prepare-workshop" if prepare else "apply-config"
    ops.start_op(operation, lambda: run(dict(data, file=file), prepare))
    return {"ok": True, "operation": operation}


def legacy_toggle(data):
    if type(data.get("enable")) is not bool:
        raise EditorError("enable должен быть boolean")
    confirmed_container()
    if ops.op_busy() or ops.is_running():
        raise EditorError("Изменение состава доступно после остановки сервера", 409)
    file = choose(data.get("file"))
    wid = data.get("workshopId")
    if not isinstance(wid, str) or not wid.isdigit():
        raise EditorError("Workshop ID должен быть числом")
    with LOCK:
        current = draft(file)
        if current["changed"]:
            raise EditorError("Есть черновик: используйте редактор модов", 409)
        mods = mod_state(file)
        index = workshop.scan(
            list(dict.fromkeys([w["workshopId"] for w in mods["workshop"]] + [wid])),
            context()["version"],
        )
        mids = [r["modId"] for r in index.get(wid, []) if r.get("modId")]
        if not mids:
            raise EditorError("Сначала загрузите пакет и определите его ModID")
        items = [w["workshopId"] for w in mods["workshop"]]
        selected = list(mods["mods"])
        legacy = load_json(state_dir(file) / "disabled.json")
        if data["enable"]:
            if wid not in items:
                items.append(wid)
            selected.extend(
                mid for mid in legacy.get(wid, []) if mid in mids and mid not in selected
            )
        else:
            subset = [mid for mid in selected if mid in mids]
            if subset or wid not in legacy:
                legacy[wid] = subset
            selected = [mid for mid in selected if mid not in mids]
        changed = patch(
            {
                "file": file,
                "draftRevision": current["draftRevision"],
                "mods": {"items": items, "selected": selected, "preserveOrder": True},
            }
        )
        result = validate(file)
        if not result["valid"]:
            raise EditorError("; ".join(e["message"] for e in result["errors"]))
        queued = queue({"file": file, "draftRevision": changed["draftRevision"], "restart": False})
        save_json(state_dir(file) / "disabled.json", legacy)
        return {**queued, "draftRevision": changed["draftRevision"]}


def modpack(data):
    file = choose(data.get("file"))
    if "pack" not in data:
        saved = load_json(state_dir(file) / "draft.json")
        mods = mod_state(file, saved.get("texts") if saved else None)
        texts = saved["texts"] if saved else read_profile(file)
        lua = literal_table(texts["sandbox"])
        base = saved.get("base", texts) if saved else texts
        schema = configschema.catalog(base["ini"], base["sandbox"], context()["version"])
        return {
            "ok": True,
            "pack": {
                "format": "pz-pult-b42",
                "version": 1,
                "items": [w["workshopId"] for w in mods["workshop"]],
                "selected": mods["mods"],
                "maps": mods["maps"],
                "sandbox": {
                    ".".join(p): r["value"]
                    for p, r in lua.values.items()
                    if p != ("VERSION",)
                    and not SECRET_KEY.search(".".join(p))
                    and not configschema.field(
                        ".".join(p), r["value"], sandbox=True, schema=schema
                    ).get("stock")
                }
                if lua
                else {},
            },
        }
    pack = data["pack"]
    if (
        not isinstance(pack, dict)
        or pack.get("format") != "pz-pult-b42"
        or pack.get("version") != 1
    ):
        raise EditorError("Неподдерживаемый формат набора модов")
    items = pack.get("items", [])
    settings = pack.get("sandbox", {})
    if not isinstance(settings, dict) or any(
        not isinstance(key, str)
        or key == "VERSION"
        or configschema.field(key, value, sandbox=True).get("stock")
        for key, value in settings.items()
    ):
        raise EditorError("Набор модов содержит штатные параметры мира или неверные настройки")
    if (
        not isinstance(items, list)
        or len(items) > 300
        or any(not isinstance(w, str) or not w.isdigit() or len(w) > 20 for w in items)
    ):
        raise EditorError("Неверный список Workshop items")
    workshop.validate_items(items)
    workshop.invalidate()
    return patch(
        {
            "file": file,
            "draftRevision": data.get("draftRevision"),
            "mods": {k: pack.get(k, []) for k in ("items", "selected", "maps")},
            "sandbox": pack.get("sandbox", {}),
        }
    )
