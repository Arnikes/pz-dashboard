"""Settings normalization and validation, without persistence or shared state."""

import json
import math
import re


DEFAULTS = {
    "autoUpdate": {
        "enabled": False,
        "intervalHours": 6,
        "warnSeconds": 300,
        "backupBeforeUpdate": True,
    },
    "modsUpdate": {
        "enabled": False,
        "intervalHours": 6,
        "restartOnUpdate": True,
        "warnSeconds": 600,
    },
    "telegram": {
        "enabled": False,
        "botToken": "",
        "chatId": "",
        "groups": {"ops": True, "backup": True, "update": True, "problems": True},
    },
    "backup": {"stopServer": False, "maxBackups": 7},
    "autoBackup": {"enabled": False, "time": "03:00", "stopServer": False},
    "watchdog": {"enabled": False, "thresholdMin": 5, "gracePeriodMin": 5, "autoRestart": False},
    "nextCheck": None,
    "nextModsCheck": None,
    "nextBackupRun": None,
    "modsDisabled": {},  # workshop id -> {title, modIds, at} — выключенные из конфига
}

# Field order preserves the API's first validation error for each section.
RULES = {
    "autoUpdate": {
        "enabled": "enabled",
        "intervalHours": (1, 168),
        "warnSeconds": (0, 3600),
        "backupBeforeUpdate": "backupBeforeUpdate",
    },
    "modsUpdate": {
        "enabled": "enabled",
        "intervalHours": (1, 168),
        "warnSeconds": (0, 3600),
        "restartOnUpdate": "restartOnUpdate",
    },
    "watchdog": {
        "enabled": "watchdog.enabled",
        "thresholdMin": (1, 60),
        "autoRestart": "watchdog.autoRestart",
        "gracePeriodMin": (0, 60),
    },
    "backup": {"stopServer": "stopServer", "maxBackups": (0, 200)},
    "autoBackup": {"enabled": "enabled", "time": None, "stopServer": "stopServer"},
}
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def clamp_int(value, lo, hi):
    if isinstance(value, bool):
        return None
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def valid_hhmm(value):
    if not isinstance(value, str):
        return False
    match = TIME_RE.match(value.strip())
    return bool(match and 0 <= int(match[1]) <= 23 and 0 <= int(match[2]) <= 59)


def norm_hhmm(value):
    match = TIME_RE.match(str(value).strip())
    return f"{int(match[1]):02d}:{int(match[2]):02d}"


def _validate_section(section, values):
    for key, rule in RULES[section].items():
        if key not in values:
            continue
        if isinstance(rule, tuple):
            normalized = clamp_int(values[key], *rule)
            if normalized is None:
                return f"{key} должен быть числом {rule[0]}–{rule[1]}"
            values[key] = normalized
        elif rule is None:
            if not valid_hhmm(values[key]):
                return "time должен быть временем в формате ЧЧ:ММ"
            values[key] = norm_hhmm(values[key])
        elif not isinstance(values[key], bool):
            return f"{rule} должен быть true/false"
    return None


def _validate_telegram(values):
    if "enabled" in values and not isinstance(values["enabled"], bool):
        return "telegram.enabled должен быть true/false"
    groups = values.get("groups")
    if groups is not None:
        if not isinstance(groups, dict):
            return "неверный формат groups"
        values["groups"] = {
            key: bool(groups[key])
            for key in ("ops", "backup", "update", "problems")
            if key in groups
        }
    token = values.get("botToken")
    if token is None or isinstance(token, str) and not token.strip():
        values.pop("botToken", None)
    elif isinstance(token, str):
        token = token.strip()
        if "•" in token:
            values.pop("botToken", None)
        else:
            values["botToken"] = token[:80]
    else:
        return "botToken должен быть строкой"
    chat_id = values.get("chatId")
    if chat_id is None:
        values.pop("chatId", None)
    elif isinstance(chat_id, str):
        values["chatId"] = chat_id.strip()[:32]
    else:
        return "chatId должен быть строкой"
    return None


def prepare_patch(current, patch, next_daily_run):
    """Return a detached candidate and error; never mutate either input.

    Scheduling is supplied by the caller so the model does not own a clock.
    Unknown section fields retain the existing forward-compatible behavior.
    """
    if not isinstance(patch, dict):
        return None, "неверный формат настроек"
    patch = json.loads(json.dumps(patch))
    updated = json.loads(json.dumps(current))
    for section in (*RULES, "telegram"):
        values = patch.get(section)
        if values is None:
            continue
        if not isinstance(values, dict):
            return None, f"неверный формат {section}"
        error = (
            _validate_telegram(values)
            if section == "telegram"
            else _validate_section(section, values)
        )
        if error:
            return None, error
        updated[section].update(values)
        if section == "autoBackup" and ("enabled" in values or "time" in values):
            updated["nextBackupRun"] = (
                next_daily_run(updated[section]["time"]) if updated[section]["enabled"] else None
            )
    return updated, None


def merge_loaded(settings, data):
    """Merge persisted data with the current settings before normalization."""
    for k, v in data.items():
        if isinstance(DEFAULTS.get(k), dict) and isinstance(v, dict) and k != "modsDisabled":
            settings[k].update(v)
        elif k == "modsDisabled" and isinstance(v, dict):
            settings["modsDisabled"] = v
        elif k in ("nextCheck", "nextModsCheck", "nextBackupRun"):
            # метка планировщика — только число; строка/список из рук
            # иначе роняли бы планировщик TypeError'ом каждые 20 с
            settings[k] = (
                v
                if isinstance(v, (int, float))
                and not isinstance(v, bool)
                and not (isinstance(v, float) and not math.isfinite(v))
                else None
            )


def normalize_loaded(settings):
    """Normalize legacy or manually edited settings in the caller-owned object."""
    # значения из старого/ручного файла не должны обходить валидацию patch_settings:
    # интервал 0 превратил бы планировщик в цикл проверок каждые 20 с
    for section, fields in RULES.items():
        for key, rule in fields.items():
            if not isinstance(rule, tuple):
                continue
            value = settings[section].get(key)
            normalized = clamp_int(value, *rule) if isinstance(value, (int, float)) else None
            settings[section][key] = DEFAULTS[section][key] if normalized is None else normalized
    for section, defaults in DEFAULTS.items():
        if not isinstance(defaults, dict):
            continue
        for key, default in defaults.items():
            if isinstance(default, bool) and not isinstance(settings[section].get(key), bool):
                settings[section][key] = default
            elif isinstance(default, str) and not isinstance(settings[section].get(key), str):
                settings[section][key] = default
    groups = settings["telegram"].get("groups")
    settings["telegram"]["groups"] = {
        key: groups[key]
        if isinstance(groups, dict) and isinstance(groups.get(key), bool)
        else default
        for key, default in DEFAULTS["telegram"]["groups"].items()
    }
    # время автобэкапа — строка «ЧЧ:ММ»; мусор из рук заменяется значением по умолчанию
    if not valid_hhmm(settings["autoBackup"].get("time")):
        settings["autoBackup"]["time"] = DEFAULTS["autoBackup"]["time"]
    else:
        settings["autoBackup"]["time"] = norm_hhmm(settings["autoBackup"]["time"])
