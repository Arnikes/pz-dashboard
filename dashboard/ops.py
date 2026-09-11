#!/usr/bin/env python3
"""Ядро пульта: события, настройки, операции (старт/стоп/бэкапы/обновления),
планировщик автообновления. Всё на стандартной библиотеке."""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from datetime import datetime, timedelta, timezone

import config
import dockerlib
import notify as notifylib
import rcon as rconlib

# ─────────────────────────── утилиты времени ───────────────────────────


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _iso_ts(value):
    try:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone()
            .isoformat(timespec="seconds")
        )
    except (ValueError, AttributeError):
        return value or ""


def fmt_size(n):
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.1f} {unit}" if unit != "Б" else f"{int(n)} Б"
        n /= 1024
    return f"{n:.1f} ТБ"


# ─────────────────────────── события ───────────────────────────

_EV_LOCK = threading.Lock()
_EV_MEM = deque(maxlen=300)

_EVENT_TYPES = {
    "start",
    "stop",
    "restart",
    "backup",
    "restore",
    "update",
    "update-check",
    "auto",
    "console",
    "warn",
    "delete",
    "rcon-error",
    "error",
    "docker",
    "backup-delete",
    "mods",
}


def log_event(kind, text, detail=None, notify=True):
    kind = kind if kind in _EVENT_TYPES else "error"
    rec = {"ts": now_iso(), "type": kind, "text": text}
    if detail:
        rec["detail"] = str(detail)[:400]
    with _EV_LOCK:
        _EV_MEM.appendleft(rec)
        try:
            os.makedirs(os.path.dirname(config.CFG["events_file"]), exist_ok=True)
            with open(config.CFG["events_file"], "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # события в памяти всё равно работают
    if notify:
        try:
            notifylib.enqueue(kind, text)
        except Exception:  # noqa: BLE001 — уведомления не ломают события
            pass


def get_events(limit=100):
    out = list(_EV_MEM)[:limit]
    if not out:
        # поднимаем историю из файла при первом обращении
        try:
            with open(config.CFG["events_file"], encoding="utf-8") as f:
                lines = f.read().splitlines()
            with _EV_LOCK:
                for line in lines[-200:]:
                    try:
                        _EV_MEM.appendleft(json.loads(line))
                    except (ValueError, KeyError):
                        pass
                out = list(_EV_MEM)[:limit]
        except OSError:
            pass
    return out


# ─────────────────────────── настройки ───────────────────────────

_SET_LOCK = threading.Lock()
_DEFAULTS = {
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
    "watchdog": {"enabled": False, "thresholdMin": 5, "autoRestart": False},
    "nextCheck": None,
    "nextModsCheck": None,
    "nextBackupRun": None,
    "modsDisabled": {},  # workshop id -> {title, modIds, at} — выключенные из конфига
}

# рабочая копия настроек: мутируется в рантайме, _DEFAULTS остаётся эталоном
_SETTINGS = json.loads(json.dumps(_DEFAULTS))


def _clamp_int(value, lo, hi):
    """int с клампом для patch_settings; None — если значение не числовое."""
    if isinstance(value, bool):
        return None
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return None


_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _valid_hhmm(value):
    """Строка «ЧЧ:ММ» с валидными часами и минутами."""
    if not isinstance(value, str):
        return False
    m = _TIME_RE.match(value.strip())
    if not m:
        return False
    return 0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59


def _norm_hhmm(value):
    m = _TIME_RE.match(str(value).strip())
    return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"


def _next_daily_run(time_str, now=None):
    """Следующий суточный запуск в «ЧЧ:ММ» по локальному времени пульта."""
    hh, mm = _norm_hhmm(time_str).split(":") if _valid_hhmm(time_str) else ("3", "0")
    base = datetime.fromtimestamp(now if now is not None else time.time())
    run = base.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if run <= base:
        run += timedelta(days=1)
    return run.timestamp()


def _load_settings():
    try:
        with open(config.CFG["settings_file"], encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k in _SETTINGS and isinstance(v, dict) and k != "modsDisabled":
                _SETTINGS[k].update(v)
            elif k == "modsDisabled" and isinstance(v, dict):
                _SETTINGS["modsDisabled"] = v
            elif k in ("nextCheck", "nextModsCheck", "nextBackupRun"):
                # метка планировщика — только число; строка/список из рук
                # иначе роняли бы планировщик TypeError'ом каждые 20 с
                _SETTINGS[k] = (
                    v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
                )
    except (OSError, ValueError):
        pass
    # значения из старого/ручного файла не должны обходить валидацию patch_settings:
    # интервал 0 превратил бы планировщик в цикл проверок каждые 20 с
    for section, key, lo, hi in (
        ("autoUpdate", "intervalHours", 1, 168),
        ("autoUpdate", "warnSeconds", 0, 3600),
        ("modsUpdate", "intervalHours", 1, 168),
        ("modsUpdate", "warnSeconds", 0, 3600),
        ("watchdog", "thresholdMin", 1, 60),
        ("backup", "maxBackups", 0, 200),
    ):
        val = _SETTINGS[section].get(key)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            _SETTINGS[section][key] = _DEFAULTS[section][key]
        else:
            _SETTINGS[section][key] = max(lo, min(hi, int(val)))
    # время автобэкапа — строка «ЧЧ:ММ»; мусор из рук заменяется значением по умолчанию
    if not _valid_hhmm(_SETTINGS["autoBackup"].get("time")):
        _SETTINGS["autoBackup"]["time"] = _DEFAULTS["autoBackup"]["time"]
    else:
        _SETTINGS["autoBackup"]["time"] = _norm_hhmm(_SETTINGS["autoBackup"]["time"])


def _save_settings():
    try:
        os.makedirs(os.path.dirname(config.CFG["settings_file"]), exist_ok=True)
        tmp = config.CFG["settings_file"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_SETTINGS, f, ensure_ascii=False, indent=2)
        os.replace(tmp, config.CFG["settings_file"])
    except OSError:
        pass


def get_settings():
    with _SET_LOCK:
        data = json.loads(json.dumps(_SETTINGS))
    # полный токен бота не покидает сервер — наружу только маска
    tg = data.get("telegram")
    if tg and tg.get("botToken"):
        tok = tg["botToken"]
        tg["botTokenMasked"] = ("•••" + tok[-4:]) if len(tok) >= 8 else "•••"
        tg["botToken"] = ""
    return data


def telegram_settings_raw():
    """Полный блок настроек Telegram с НАСТОЯЩИМ токеном — только для
    внутренней отправки (notify). Наружу идёт get_settings() с маской:
    уведомления и кнопка «Проверить» читали маску с пустым botToken и
    считали токен незаданным, хотя он был сохранён."""
    with _SET_LOCK:
        return json.loads(json.dumps(_SETTINGS.get("telegram") or {}))


def patch_settings(patch):
    """Обновить настройки с валидацией. Возвращает текст ошибки или None."""
    with _SET_LOCK:
        au = patch.get("autoUpdate")
        if au is not None:
            if not isinstance(au, dict):
                return "неверный формат autoUpdate"
            if "enabled" in au and not isinstance(au["enabled"], bool):
                return "enabled должен быть true/false"
            if "intervalHours" in au:
                v = _clamp_int(au["intervalHours"], 1, 168)
                if v is None:
                    return "intervalHours должен быть числом 1–168"
                au["intervalHours"] = v
            if "warnSeconds" in au:
                v = _clamp_int(au["warnSeconds"], 0, 3600)
                if v is None:
                    return "warnSeconds должен быть числом 0–3600"
                au["warnSeconds"] = v
            if "backupBeforeUpdate" in au and not isinstance(au["backupBeforeUpdate"], bool):
                return "backupBeforeUpdate должен быть true/false"
            _SETTINGS["autoUpdate"].update(au)
        mu = patch.get("modsUpdate")
        if mu is not None:
            if not isinstance(mu, dict):
                return "неверный формат modsUpdate"
            if "enabled" in mu and not isinstance(mu["enabled"], bool):
                return "enabled должен быть true/false"
            if "intervalHours" in mu:
                v = _clamp_int(mu["intervalHours"], 1, 168)
                if v is None:
                    return "intervalHours должен быть числом 1–168"
                mu["intervalHours"] = v
            if "warnSeconds" in mu:
                v = _clamp_int(mu["warnSeconds"], 0, 3600)
                if v is None:
                    return "warnSeconds должен быть числом 0–3600"
                mu["warnSeconds"] = v
            if "restartOnUpdate" in mu and not isinstance(mu["restartOnUpdate"], bool):
                return "restartOnUpdate должен быть true/false"
            _SETTINGS["modsUpdate"].update(mu)
        wd = patch.get("watchdog")
        if wd is not None:
            if not isinstance(wd, dict):
                return "неверный формат watchdog"
            if "enabled" in wd and not isinstance(wd["enabled"], bool):
                return "watchdog.enabled должен быть true/false"
            if "thresholdMin" in wd:
                v = _clamp_int(wd["thresholdMin"], 1, 60)
                if v is None:
                    return "thresholdMin должен быть числом 1–60"
                wd["thresholdMin"] = v
            if "autoRestart" in wd and not isinstance(wd["autoRestart"], bool):
                return "watchdog.autoRestart должен быть true/false"
            _SETTINGS["watchdog"].update(wd)
        bk = patch.get("backup")
        if bk is not None:
            if not isinstance(bk, dict):
                return "неверный формат backup"
            if "stopServer" in bk and not isinstance(bk["stopServer"], bool):
                return "stopServer должен быть true/false"
            if "maxBackups" in bk:
                v = _clamp_int(bk["maxBackups"], 0, 200)
                if v is None:
                    return "maxBackups должен быть числом 0–200"
                bk["maxBackups"] = v
            _SETTINGS["backup"].update(bk)
        abk = patch.get("autoBackup")
        if abk is not None:
            if not isinstance(abk, dict):
                return "неверный формат autoBackup"
            if "enabled" in abk and not isinstance(abk["enabled"], bool):
                return "enabled должен быть true/false"
            if "time" in abk:
                if not _valid_hhmm(abk["time"]):
                    return "time должен быть временем в формате ЧЧ:ММ"
                abk["time"] = _norm_hhmm(abk["time"])
            if "stopServer" in abk and not isinstance(abk["stopServer"], bool):
                return "stopServer должен быть true/false"
            _SETTINGS["autoBackup"].update(abk)
            if "enabled" in abk or "time" in abk:
                # расписание изменилось — пересчитать следующий запуск
                _SETTINGS["nextBackupRun"] = (
                    _next_daily_run(_SETTINGS["autoBackup"]["time"])
                    if _SETTINGS["autoBackup"]["enabled"]
                    else None
                )
        tg = patch.get("telegram")
        if tg is not None:
            if not isinstance(tg, dict):
                return "неверный формат telegram"
            if "enabled" in tg and not isinstance(tg["enabled"], bool):
                return "telegram.enabled должен быть true/false"
            groups = tg.get("groups")
            if groups is not None:
                if not isinstance(groups, dict):
                    return "неверный формат groups"
                tg["groups"] = {
                    k: bool(groups[k])
                    for k in ("ops", "backup", "update", "problems")
                    if k in groups
                }
            tok = tg.get("botToken")
            if tok is None or (isinstance(tok, str) and not tok.strip()):
                tg.pop("botToken", None)  # пустое поле — не менять токен
            elif isinstance(tok, str):
                tok = tok.strip()
                if "•" in tok:  # маска от get_settings — не менять
                    tg.pop("botToken", None)
                else:
                    tg["botToken"] = tok[:80]
            else:
                return "botToken должен быть строкой"
            cid = tg.get("chatId")
            if cid is None:
                tg.pop("chatId", None)
            elif isinstance(cid, str):
                tg["chatId"] = cid.strip()[:32]
            else:
                return "chatId должен быть строкой"
            _SETTINGS["telegram"].update(tg)
        _save_settings()
        return None


# ─────────────────────────── RCON-хелперы ───────────────────────────

_RCON_CACHE = {"state": "unknown", "error": None, "at": None}
_RCON_LOG = {"at": 0.0}  # когда последний раз писали rcon-error событие


def rcon(command, quiet=False):
    """Выполнить RCON-команду. Бросает RCONError при недоступности."""
    cfg = config.CFG
    try:
        text = rconlib.run_command(
            cfg["rcon_host"], cfg["rcon_port"], cfg["rcon_password"], command
        )
        if _RCON_CACHE["state"] != "ok":
            _RCON_LOG["at"] = 0.0  # связь восстановилась — следующий сбой снова заметен
        _RCON_CACHE.update({"state": "ok", "error": None, "at": now_iso()})
        return text
    except rconlib.RCONError as e:
        was_ok = _RCON_CACHE["state"] == "ok"
        _RCON_CACHE.update({"state": "error", "error": str(e), "at": now_iso()})
        now = time.time()
        # сбойный RCON опрашивается каждые 5 с (SSE players) — пишем событие
        # на переход «было ok» и далее раз в 5 минут, иначе журнал и Telegram
        # заливаются дубликатами
        if not quiet and (was_ok or now - _RCON_LOG["at"] >= 300):
            _RCON_LOG["at"] = now
            log_event("rcon-error", "RCON: " + str(e))
        raise


def rcon_warn_broadcast(seconds, reason, abort_check=None):
    """Отправляет игрокам отсчёт перед остановкой. Возвращает False при сбое RCON.

    abort_check — вызывается перед каждым шагом отсчёта; истинный результат
    прерывает рассылку (например, сервер уже перезапустили вручную)."""
    if seconds <= 0:
        return True
    thresholds = {30, 10}
    thresholds.update(range(60, seconds + 1, 60))
    if seconds < 10:
        thresholds.add(seconds)  # совсем короткий отсчёт всё равно слышен
    last_sent = None
    ok = True
    # шаг 10 с, старт выровнен вниз до кратности: иначе (например 45 с)
    # отсчёт молча пропускает все пороги и сервер останавливается без предупреждения
    start = seconds if seconds < 10 else seconds - (seconds % 10)
    for left in range(start, 0, -10):
        if abort_check is not None and abort_check():
            return ok
        if left in thresholds and left != last_sent:
            text = (
                f"{reason} через {left // 60} мин" if left >= 60 else f"{reason} через {left} сек"
            )
            try:
                rcon(f'servermsg "{text}"', quiet=True)
                last_sent = left
            except rconlib.RCONError:
                ok = False
                break
        time.sleep(min(10, left))
    return ok


# ─────────────────────────── статус контейнера ───────────────────────────

_DOCKER_CACHE = {"ok": None, "at": 0.0}
_COMPOSE_OK = {"ok": None, "at": 0.0}


def docker_ok_cached(ttl=60):
    """Доступность docker — с кэшем: опрос каждые 3 с не должен дёргать CLI."""
    now = time.time()
    if _DOCKER_CACHE["ok"] is None or now - _DOCKER_CACHE["at"] > ttl:
        _DOCKER_CACHE["ok"] = dockerlib.docker_version()
        _DOCKER_CACHE["at"] = now
    return _DOCKER_CACHE["ok"]


def compose_ok_cached(ttl=60):
    """Доступность плагина compose — тоже с кэшем: overview отдаётся каждые 3 с,
    а `docker compose version` — заметно более тяжёлый вызов, чем docker version."""
    now = time.time()
    if _COMPOSE_OK["ok"] is None or now - _COMPOSE_OK["at"] > ttl:
        _COMPOSE_OK["ok"] = dockerlib.compose_version()
        _COMPOSE_OK["at"] = now
    return _COMPOSE_OK["ok"]


def container_state():
    return dockerlib.inspect_container(config.CFG["pz_container"])


def is_running():
    st = container_state()
    return bool(st and st["running"])


def wait_until_stopped(timeout=240, started_at=None):
    """Ждать остановки контейнера.

    Возвращает "stopped", "running" (не дождались) или "resurrected" —
    docker сам поднял контейнер заново (restart policy), StartedAt сменился."""
    waited = 0
    while waited < timeout:
        st = container_state()
        if not st or not st["running"]:
            return "stopped"
        if started_at and st["startedAt"] and st["startedAt"] != started_at:
            return "resurrected"
        time.sleep(5)
        waited += 5
    return "running" if is_running() else "stopped"


def wait_until_running(timeout=120):
    waited = 0
    while waited < timeout:
        if is_running():
            return True
        time.sleep(4)
        waited += 4
    return is_running()


def graceful_stop(phase_hook=None):
    """Правильная остановка PZ: RCON quit (сохранение мира), затем docker stop.

    Контейнер PZ обычно живёт с restart: unless-stopped/always — тогда через
    секунду после quit docker поднимает его обратно, и ожидание остановки
    не заканчивается никогда: даунтайм в доли секунды не виден 5-секундному
    опросу, а со стороны выглядит как самопроизвольный рестарт. Поэтому
    на время остановки политика рестарта временно глушится и восстанавливается
    после (docker update не запускает контейнер, так что это безопасно).

    Возвращает "stopped" или "resurrected" — docker сам перезапустил контейнер,
    т.е. рестарт уже произошёл без нас."""

    def hook(msg):
        if phase_hook:
            phase_hook(msg)

    if not is_running():
        return "stopped"
    container = config.CFG["pz_container"]
    started_at = (container_state() or {}).get("startedAt")
    orig_policy = dockerlib.get_restart_policy(container)
    policy_off = False
    if orig_policy and orig_policy != "no":
        policy_off = dockerlib.set_restart_policy(container, "no")
        if policy_off:
            hook(f"Restart policy {orig_policy} временно отключена")
        else:
            log_event(
                "warn",
                "Не удалось временно отключить restart policy — "
                "docker может сам перезапустить контейнер при остановке",
            )
    try:
        hook("Команда quit через RCON (сохранение мира)")
        try:
            rcon("quit", quiet=True)
        except rconlib.RCONError:
            pass
        hook("Ожидание остановки контейнера")
        first = wait_until_stopped(240, started_at=started_at)
        if first == "resurrected":
            return "resurrected"
        if first == "running":
            hook("Принудительная остановка контейнера")
            dockerlib.container_stop(container, seconds=180)
            second = wait_until_stopped(60, started_at=started_at)
            if second == "resurrected":
                return "resurrected"
            if second == "running":
                raise OpsError("Контейнер не остановился — смотрите docker logs " + container)
        log_event("stop", "Сервер остановлен")
        return "stopped"
    finally:
        if policy_off:
            dockerlib.set_restart_policy(container, orig_policy)


# ─────────────────────────── операции ───────────────────────────


class OpsError(Exception):
    pass


class OpsErrorReported(OpsError):
    """OpsError, о которой уже сообщено (событие и журнал) — воркер не дублирует."""

    pass


_OP_LOCK = threading.Lock()
_ACTIVE = {"op": None, "phase": "", "message": "", "startedAt": None}
_OP_HISTORY = deque(maxlen=10)

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def op_state():
    with _OP_LOCK:
        return {
            "active": dict(_ACTIVE) if _ACTIVE["op"] else None,
            "history": list(_OP_HISTORY),
        }


def op_busy():
    with _OP_LOCK:
        return _ACTIVE["op"] is not None


def _set_phase(phase, message=""):
    with _OP_LOCK:
        _ACTIVE["phase"] = phase
        _ACTIVE["message"] = message


def _start_worker(op, fn):
    def worker():
        try:
            fn()
            with _OP_LOCK:
                _OP_HISTORY.appendleft(
                    {"op": op, "ok": True, "message": _ACTIVE["message"], "finishedAt": now_iso()}
                )
        except OpsErrorReported as e:
            # событие уже записал источник (run_backup_job) — фиксируем только статус
            with _OP_LOCK:
                _ACTIVE["message"] = str(e)
                _OP_HISTORY.appendleft(
                    {"op": op, "ok": False, "message": str(e), "finishedAt": now_iso()}
                )
        except OpsError as e:
            log_event("error", f"Операция «{op}» не удалась: {e}")
            with _OP_LOCK:
                _ACTIVE["message"] = str(e)
                _OP_HISTORY.appendleft(
                    {"op": op, "ok": False, "message": str(e), "finishedAt": now_iso()}
                )
        except Exception as e:  # noqa: BLE001 — не роняем поток
            log_event("error", f"Операция «{op}»: {e}")
            with _OP_LOCK:
                _ACTIVE["message"] = f"Внутренняя ошибка: {e}"
                _OP_HISTORY.appendleft(
                    {"op": op, "ok": False, "message": str(e), "finishedAt": now_iso()}
                )
        finally:
            with _OP_LOCK:
                _ACTIVE["op"] = None
                _ACTIVE["phase"] = ""
                _ACTIVE["startedAt"] = None

    with _OP_LOCK:
        if _ACTIVE["op"]:
            raise OpsError("Уже выполняется другая операция, подождите")
        _ACTIVE.update({"op": op, "phase": "Подготовка…", "message": "", "startedAt": now_iso()})
    t = threading.Thread(target=worker, daemon=True, name=f"op-{op}")
    t.start()


def start_op(op, fn):
    """Запустить операцию в фоне. Бросает OpsError, если занято."""
    _start_worker(op, fn)


# ─── старт / стоп / рестарт ───


def _do_start():
    if is_running():
        raise OpsError("Сервер уже запущен")
    _set_phase("Запуск", "docker start")
    code, out, err = dockerlib.container_start(config.CFG["pz_container"])
    if code != 0:
        raise OpsError(f"Не удалось запустить: {err or out}")
    wait_until_running(150)
    log_event("start", "Сервер запущен")
    _set_phase("Готово", "Сервер запущен")


def _do_stop(warn_seconds):
    if not is_running():
        raise OpsError("Сервер не запущен")
    if warn_seconds > 0:
        _set_phase("Предупреждение игроков", f"отсчёт {warn_seconds} с")
        rcon_warn_broadcast(warn_seconds, "Остановка сервера")
    if graceful_stop(lambda m: _set_phase("Остановка", m)) == "resurrected":
        raise OpsError(
            "Docker сам перезапустил контейнер — остановка не удалась, "
            "проверьте restart policy и повторите"
        )
    _set_phase("Готово", "Сервер остановлен")


def _restarted_since(started_at):
    """Что стало с контейнером с момента started_at: 'restarted', 'stopped' или None."""
    st = container_state()
    if not st or not st["running"]:
        return "stopped"
    if started_at and st.get("startedAt") and st["startedAt"] != started_at:
        return "restarted"
    return None


def _do_restart(warn_seconds, reason="Перезапуск сервера", guard_restarted=False):
    if not is_running():
        raise OpsError("Сервер не запущен — сначала запустите его")
    # guard_restarted: авторестарт (моды) не должен дублировать ручной рестарт
    # админа — если за время отсчёта контейнер уже перезапустили, отменяемся
    guard_at = (container_state() or {}).get("startedAt") if guard_restarted else None

    def _guard_tripped():
        return _restarted_since(guard_at) if guard_restarted else None

    if warn_seconds > 0:
        _set_phase("Предупреждение игроков", f"отсчёт {warn_seconds} с")
        rcon_warn_broadcast(warn_seconds, reason, abort_check=_guard_tripped)
    if guard_restarted:
        verdict = _restarted_since(guard_at)
        if verdict:
            msg = (
                "Сервер уже перезапущен вручную — авторестарт отменён"
                if verdict == "restarted"
                else "Сервер остановлен вручную — авторестарт отменён"
            )
            log_event("auto", msg)
            _set_phase("Готово", "Авторестарт отменён: сервер уже перезапущен")
            return "aborted"
    if graceful_stop(lambda m: _set_phase("Остановка", m)) == "resurrected":
        # docker сам поднял контейнер (restart policy) — рестарт уже случился,
        # остаётся дождаться запуска сервера
        wait_until_running(150)
        log_event("restart", f"Сервер перезапущен ({reason})")
        _set_phase("Готово", "Сервер перезапущен")
        return
    _set_phase("Запуск", "docker start")
    code, out, err = dockerlib.container_start(config.CFG["pz_container"])
    if code != 0:
        raise OpsError(f"Не удалось запустить после рестарта: {err or out}")
    wait_until_running(150)
    log_event("restart", f"Сервер перезапущен ({reason})")
    _set_phase("Готово", "Сервер перезапущен")


# ─── обновления ───


def _effective_image():
    """Фактический образ контейнера (если пульт на хосте) или из конфига.
    Реальный образ может отличаться от дефолта — проверяем то, что запущено."""
    st = container_state()
    img = (st or {}).get("image") or config.CFG["pz_image"]
    if ":" not in img.rsplit("/", 1)[-1]:
        img = img + ":latest"
    return img


_LAST_CHECK = {
    "at": None,
    "image": None,
    "local": None,
    "remote": None,
    "hubUpdated": None,
    "available": None,
    "error": None,
}


def check_update(force_event=False):
    """Сравнить локальный и актуальный digest образа, который реально запущен."""
    image = _effective_image()
    repo, _, tag = image.rpartition(":")
    local = dockerlib.image_digests(image)
    result = {
        "at": now_iso(),
        "image": image,
        "local": local,
        "remote": None,
        "hubUpdated": None,
        "available": None,
        "error": None,
    }
    try:
        url = f"https://hub.docker.com/v2/repositories/{repo}/tags/{tag}"
        req = urllib.request.Request(url, headers={"User-Agent": "pz-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        digest = data.get("digest") or ""
        result["remote"] = (
            digest if digest.startswith("sha256:") else ("sha256:" + digest if digest else None)
        )
        result["hubUpdated"] = data.get("last_updated")
        if local and result["remote"]:
            result["available"] = local != result["remote"]
        elif result["remote"] and not local:
            result["available"] = None
            if docker_ok_cached():
                result["note"] = (
                    "У образа нет repo-digest (собран или загружен без pull) — сравнение по digest невозможно"
                )
            else:
                result["note"] = (
                    "Локальный digest недоступен (пульт вне хоста сервера) — сравнение версий невозможно"
                )
        elif not result["remote"]:
            result["error"] = "не удалось получить актуальный образ из Docker Hub"
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        result["error"] = f"Docker Hub недоступен: {e}"
    _LAST_CHECK.update(result)
    if force_event:
        if result["error"]:
            log_event("update-check", "Проверка обновлений: " + result["error"])
        elif result["available"]:
            log_event("update", "Доступно обновление образа " + config.CFG["pz_image"])
    return dict(result)


def update_state():
    return dict(_LAST_CHECK)


_LOCAL_DIGEST = {"digest": None, "image": None, "at": 0.0}


def local_digest_cached(ttl=60):
    """Локальный digest считается сам по себе (TTL-кэш), а не только по кнопке «Проверить»."""
    now = time.time()
    if now - _LOCAL_DIGEST["at"] > ttl:
        image = _effective_image()
        _LOCAL_DIGEST.update({"digest": dockerlib.image_digests(image), "image": image, "at": now})
    return _LOCAL_DIGEST["digest"]


def _do_apply_update(warn_seconds, reason="Обновление сервера"):
    _set_phase("Проверка актуального образа", "Docker Hub")
    try:
        image = _effective_image()
        before = dockerlib.image_digests(image)
        _set_phase("Скачивание нового образа", image)
        code, out, err = dockerlib.image_pull(image)
        if code != 0:
            raise OpsError(f"Не удалось скачать образ: {(err or out)[:200]}")
        after = dockerlib.image_digests(image)
        if before and after and before == after:
            check_update(force_event=False)
            log_event("update", "Образ уже актуален, обновление не требуется")
            _set_phase("Готово", "Образ уже актуален")
            return
        if not dockerlib.compose_version():
            raise OpsError(
                "docker compose недоступен — без него нельзя заменить контейнер. "
                "Проверьте установку плагина compose в образе пульта"
            )
        if get_settings()["autoUpdate"].get("backupBeforeUpdate", True):
            _set_phase("Страховочный бэкап", "сохранение мира и архива")
            try:
                bk = _do_backup(False)
                log_event(
                    "backup", f"Бэкап перед обновлением: {bk['name']} ({fmt_size(bk['size'])})"
                )
            except OpsError as e:
                raise OpsError(f"Обновление отменено — не удалось сделать бэкап: {e}")
        was_running = is_running()
        if was_running and warn_seconds > 0:
            _set_phase("Предупреждение игроков", f"отсчёт {warn_seconds} с")
            rcon_warn_broadcast(warn_seconds, reason)
        if was_running:
            graceful_stop(lambda m: _set_phase("Остановка сервера", m))
        _set_phase("Пересоздание контейнера", "docker compose up -d")
        code, out, err = dockerlib.compose_up(config.CFG)
        if code != 0:
            # пробуем поднять старый контейнер обратно
            dockerlib.container_start(config.CFG["pz_container"])
            raise OpsError(
                f"docker compose up не удался: {(err or out)[:200]}. "
                "Сервер оставлен остановленным — проверьте конфиг"
            )
        wait_until_running(180)
        if not is_running():
            raise OpsError("Контейнер пересоздан, но не поднялся — проверьте docker logs pzserver")
        check_update(force_event=False)
        log_event("update", f"Сервер обновлён до {image}")
        _set_phase("Готово", "Сервер обновлён и запущен")
    except OpsError:
        raise
    except Exception as e:  # noqa: BLE001
        raise OpsError(f"Обновление не удалось: {e}")


# ─── бэкапы ───

# ─────────────────────────── журнал бэкапов ───────────────────────────

_BJ_LOCK = threading.Lock()
_BJ_FIELDS = ("ts", "trigger", "type", "name", "size", "path", "status", "duration", "error")


def _journal_path():
    return os.path.join(config.CFG["dashboard_dir"], "backups.jsonl")


def _journal_append(entry):
    """Добавить запись о запуске бэкапа: дата, триггер, имя, размер, путь, статус."""
    rec = {
        "ts": now_iso(),
        "trigger": "manual",
        "type": "full",
        "name": "",
        "size": 0,
        "path": "",
        "status": "success",
        "duration": 0,
        "error": "",
    }
    rec.update({k: entry[k] for k in _BJ_FIELDS if k in entry})
    with _BJ_LOCK:
        try:
            os.makedirs(os.path.dirname(_journal_path()), exist_ok=True)
            with open(_journal_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass


def get_backup_journal(limit=50):
    """Последние записи журнала запусков бэкапов — новые сверху."""
    try:
        with open(_journal_path(), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


# ─────────────────────────── бэкапы ───────────────────────────


def _backup_paths():
    return config.CFG["backup_dir"], config.CFG["data_dir"]


def list_backups():
    bdir = config.CFG["backup_dir"]
    items = []
    try:
        for name in os.listdir(bdir):
            path = os.path.join(bdir, name)
            if not name.endswith(".tar.gz") or not os.path.isfile(path):
                continue
            st = os.stat(path)
            items.append(
                {
                    "name": name,
                    "size": st.st_size,
                    "sizeText": fmt_size(st.st_size),
                    "mtime": datetime.fromtimestamp(st.st_mtime)
                    .astimezone()
                    .isoformat(timespec="seconds"),
                }
            )
    except OSError:
        return []
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def _prune_backups(max_keep):
    if max_keep <= 0:
        return 0
    items = list_backups()
    removed = 0
    for old in items[max_keep:]:
        try:
            os.remove(os.path.join(config.CFG["backup_dir"], old["name"]))
            removed += 1
        except OSError:
            pass
    return removed


def _do_backup(stop_server, trigger="manual", started=None):
    bdir, ddir = _backup_paths()
    started = started if started is not None else time.time()
    os.makedirs(bdir, exist_ok=True)
    if not os.path.isdir(ddir) or not os.listdir(ddir):
        raise OpsError("Каталог данных PZ пуст или не смонтирован — бэкап невозможен")
    was_running = is_running()
    if stop_server:
        if not was_running:
            raise OpsError("Сервер не запущен — «бэкап с остановкой» не нужен, снимите флажок")
        _set_phase("Предупреждение игроков", "отсчёт 60 с")
        rcon_warn_broadcast(60, "Бэкап сервера")
        if graceful_stop(lambda m: _set_phase("Остановка для бэкапа", m)) == "resurrected":
            # docker сам поднял контейнер посреди остановки — архивировать
            # полуживой мир нельзя, файлы могут быть в записи
            raise OpsError("Docker сам перезапустил контейнер — бэкап прерван, повторите попытку")
    else:
        if was_running:
            _set_phase("Сохранение мира", "RCON save")
            try:
                rcon("save", quiet=True)
            except rconlib.RCONError:
                log_event("warn", "Не удалось выполнить save через RCON — бэкап без сохранения")
            _set_phase("Ожидание записи", "10 с на сохранение")
            time.sleep(10)
    name = time.strftime("pz-backup-%Y%m%d-%H%M%S.tar.gz")
    dest = os.path.join(bdir, name)
    _set_phase("Создание архива", name)
    try:
        proc = subprocess.run(
            [
                "tar",
                "-czf",
                dest,
                "--exclude=Logs",
                "--exclude=logs",
                "--exclude=*.log",
                "-C",
                ddir,
                ".",
            ],
            capture_output=True,
            text=True,
            timeout=2400,
        )
    except subprocess.TimeoutExpired:
        raise OpsError("Архив не создан: tar не уложился в таймаут (2400 с)")
    if proc.returncode != 0:
        raise OpsError(f"tar не удался: {(proc.stderr or proc.stdout)[:200]}")
    if stop_server and was_running:
        _set_phase("Запуск сервера", "docker start")
        dockerlib.container_start(config.CFG["pz_container"])
        wait_until_running(150)
    size = os.path.getsize(dest)
    pruned = _prune_backups(get_settings()["backup"]["maxBackups"])
    label = "по расписанию" if trigger == "scheduled" else "вручную"
    log_event("backup", f"Бэкап создан ({label}): {name} ({fmt_size(size)})")
    _journal_append(
        {
            "trigger": trigger,
            "name": name,
            "size": size,
            "path": dest,
            "status": "success",
            "duration": round(time.time() - started, 1),
        }
    )
    if pruned:
        log_event("backup-delete", f"Удалено старых бэкапов: {pruned}")
    _set_phase("Готово", f"Бэкап {name} создан")
    return {"name": name, "size": size}


def run_backup_job(trigger, stop_server):
    """Бэкап с журналированием: ручной запуск и запуск по расписанию.

    Сбой пишется в журнал и в событие с контекстом запуска; воркеру уходит
    OpsErrorReported, чтобы не дублировать событие об одной ошибке."""
    started = time.time()
    try:
        return _do_backup(stop_server, trigger=trigger, started=started)
    except OpsError as e:
        label = "по расписанию" if trigger == "scheduled" else "вручную"
        _journal_append(
            {
                "trigger": trigger,
                "status": "error",
                "error": str(e)[:300],
                "duration": round(time.time() - started, 1),
            }
        )
        log_event("error", f"Бэкап ({label}) не удался: {e}")
        raise OpsErrorReported(str(e)) from e


def _validate_backup_name(name):
    if not name or not _NAME_RE.match(name) or not name.endswith(".tar.gz"):
        raise OpsError("Некорректное имя бэкапа")
    path = os.path.join(config.CFG["backup_dir"], name)
    real = os.path.realpath(path)
    if os.path.realpath(config.CFG["backup_dir"]) + os.sep not in real + os.sep:
        raise OpsError("Некорректный путь бэкапа")
    if not os.path.isfile(real):
        raise OpsError("Файл бэкапа не найден")
    return real


def _do_restore(name):
    path = _validate_backup_name(name)
    if not os.path.isdir(config.CFG["data_dir"]):
        raise OpsError("Каталог данных PZ не смонтирован")
    if is_running():
        _set_phase("Предупреждение игроков", "отсчёт 60 с")
        rcon_warn_broadcast(60, "Восстановление из бэкапа")
        if graceful_stop(lambda m: _set_phase("Остановка сервера", m)) == "resurrected":
            # docker сам поднял контейнер посреди остановки — стирать данные
            # под живым сервером нельзя: мир будет в записи
            raise OpsError(
                "Docker сам перезапустил контейнер — восстановление прервано, повторите попытку"
            )
    _set_phase("Очистка каталога данных", "удаление старого мира")
    for entry in os.listdir(config.CFG["data_dir"]):
        full = os.path.join(config.CFG["data_dir"], entry)
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full, ignore_errors=True)
        else:
            try:
                os.remove(full)
            except OSError:
                pass
    _set_phase("Распаковка архива", name)
    try:
        proc = subprocess.run(
            ["tar", "-xzf", path, "-C", config.CFG["data_dir"]],
            capture_output=True,
            text=True,
            timeout=2400,
        )
    except subprocess.TimeoutExpired:
        raise OpsError("Распаковка не удалась: tar не уложился в таймаут (2400 с)")
    if proc.returncode != 0:
        raise OpsError(f"Распаковка не удалась: {(proc.stderr or proc.stdout)[:200]}")
    _set_phase("Запуск сервера", "docker start")
    code, out, err = dockerlib.container_start(config.CFG["pz_container"])
    if code != 0:
        raise OpsError(f"Данные восстановлены, но запуск не удался: {err or out}")
    wait_until_running(180)
    log_event("restore", f"Мир восстановлен из {name}")
    _set_phase("Готово", f"Восстановлено из {name}")


def delete_backup(name):
    path = _validate_backup_name(name)
    os.remove(path)
    log_event("backup-delete", f"Бэкап удалён: {name}")


def backup_download_path(name):
    return _validate_backup_name(name)


def verify_backup(name):
    """Контрольная проверка архива: целостность gzip/tar и распаковка во
    временную папку с подсчётом файлов. Данные сервера не затрагивает."""
    path = _validate_backup_name(name)
    started = time.time()
    _set_phase("Проверка целостности", name)
    try:
        proc = subprocess.run(["tar", "-tzf", path], capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        raise OpsError("Проверка не удалась: tar не уложился в таймаут (900 с)")
    if proc.returncode != 0:
        raise OpsError(f"Архив повреждён: {(proc.stderr or proc.stdout)[:200]}")
    if not any(ln.strip() for ln in proc.stdout.splitlines()):
        raise OpsError("Архив пуст — восстанавливаться из него нечем")
    _set_phase("Распаковка в песочницу", name)
    dest = os.path.join(config.CFG["dashboard_dir"], f"verify-tmp-{int(time.time())}")
    os.makedirs(dest, exist_ok=True)
    try:
        try:
            proc = subprocess.run(
                ["tar", "-xzf", path, "-C", dest], capture_output=True, text=True, timeout=900
            )
        except subprocess.TimeoutExpired:
            raise OpsError("Распаковка не удалась: tar не уложился в таймаут (900 с)")
        if proc.returncode != 0:
            raise OpsError(f"Архив повреждён (распаковка): {(proc.stderr or proc.stdout)[:200]}")
        files = total = 0
        has_ini = has_map = False
        for root, _dirs, fnames in os.walk(dest):
            for fn in fnames:
                fp = os.path.join(root, fn)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    continue
                files += 1
                rel = os.path.relpath(fp, dest).replace(os.sep, "/")
                if rel.startswith("Server/") and fn.lower().endswith(".ini"):
                    has_ini = True
                if rel.startswith("Maps/"):
                    has_map = True
        if not files:
            raise OpsError("Архив распаковался, но файлов внутри нет")
        notes = []
        if not has_ini:
            notes.append("нет Server/*.ini")
        if not has_map:
            notes.append("нет Maps/")
        res = {
            "name": name,
            "files": files,
            "totalSize": total,
            "totalSizeText": fmt_size(total),
            "hasServerIni": has_ini,
            "hasMapData": has_map,
            "duration": round(time.time() - started, 1),
        }
        log_event(
            "backup",
            f"Проверка бэкапа {name}: OK — файлов {files}, {fmt_size(total)}"
            + (", замечания: " + ", ".join(notes) if notes else ""),
        )
        _set_phase("Готово", f"Проверка {name}: OK")
        return res
    finally:
        shutil.rmtree(dest, ignore_errors=True)


# ─────────────────────────── планировщик автообновления ───────────────────────────


def _auto_backup_tick(s, now):
    """Тик автобэкапа: когда наступило nextBackupRun и пульт свободен — запуск."""
    ab = s.get("autoBackup") or {}
    if not ab.get("enabled"):
        return
    t = ab.get("time") or "03:00"
    nxt = s.get("nextBackupRun")
    if not nxt:
        nxt = _next_daily_run(t, now)
        _SETTINGS["nextBackupRun"] = nxt
        _save_settings()
    if now < nxt or op_busy():
        return
    late = (now - nxt) / 60
    note = " (навёрстывание)" if late > 15 else ""
    _SETTINGS["nextBackupRun"] = _next_daily_run(t, now)
    _save_settings()
    log_event("auto", f"Автобэкап по расписанию{note}: запуск ({t})")
    try:
        start_op("backup", lambda: run_backup_job("scheduled", bool(ab.get("stopServer"))))
    except OpsError:
        # гонка: между проверкой и стартом началась другая операция —
        # откатываем время, попытка повторится на следующем тике (20 с)
        _SETTINGS["nextBackupRun"] = nxt
        _save_settings()


def _scheduler_loop():
    while True:
        try:
            s = get_settings()
            au = s["autoUpdate"]
            if au["enabled"]:
                nxt = s.get("nextCheck")
                now = time.time()
                if not nxt:
                    nxt = now + au["intervalHours"] * 3600
                    _SETTINGS["nextCheck"] = nxt
                    _save_settings()
                if now >= nxt and not op_busy():
                    try:
                        check_update(force_event=False)
                        st = update_state()
                        if st.get("available"):
                            log_event("auto", "Автообновление: найдена новая версия")
                            _do_apply_update(au["warnSeconds"], "Автообновление сервера")
                    except OpsError as e:
                        log_event("error", "Автообновление: " + str(e))
                    _SETTINGS["nextCheck"] = time.time() + au["intervalHours"] * 3600
                    _save_settings()
            mu = s.get("modsUpdate") or {}
            if mu.get("enabled"):
                nxt_m = s.get("nextModsCheck")
                now = time.time()
                if not nxt_m:
                    nxt_m = now + mu["intervalHours"] * 3600
                    _SETTINGS["nextModsCheck"] = nxt_m
                    _save_settings()
                if now >= nxt_m and not op_busy():
                    try:
                        if is_running():
                            res = check_mods_update(source="auto")
                            if res["state"] == "needs-update":
                                if mu.get("restartOnUpdate", True):
                                    log_event(
                                        "auto",
                                        "Автообновление модов: рестарт для загрузки обновлений",
                                    )
                                    warn_s = mu.get("warnSeconds", 600)
                                    start_op(
                                        "mods-restart",
                                        lambda w=warn_s: _do_restart(
                                            w, reason="Обновление модов", guard_restarted=True
                                        ),
                                    )
                                else:
                                    log_event(
                                        "auto",
                                        "Автопроверка модов: найдены обновления (рестарт отключён)",
                                    )
                        else:
                            log_event("warn", "Автопроверка модов: сервер не запущен — пропуск")
                    except OpsError as e:
                        log_event("error", "Автопроверка модов: " + str(e))
                    _SETTINGS["nextModsCheck"] = time.time() + mu["intervalHours"] * 3600
                    _save_settings()
            _post_restart_rescan_tick(s)
            _auto_backup_tick(s, time.time())
        except Exception as e:  # noqa: BLE001
            log_event("error", "Планировщик: " + str(e))
        time.sleep(20)


def defer_next_check(interval_hours):
    """Отложить автопроверку образа (после ручного обновления) с сохранением.

    get_settings() возвращает копию — править надо основной словарь."""
    try:
        hours = max(1, min(168, int(interval_hours)))
    except (TypeError, ValueError):
        hours = _DEFAULTS["autoUpdate"]["intervalHours"]
    with _SET_LOCK:
        _SETTINGS["nextCheck"] = time.time() + hours * 3600
        _save_settings()


def auto_backup_state():
    """Сводка расписания автобэкапа для API и интерфейса."""
    s = get_settings()
    ab = s["autoBackup"]
    nxt = s.get("nextBackupRun")
    next_iso = None
    if isinstance(nxt, (int, float)) and not isinstance(nxt, bool):
        try:
            next_iso = datetime.fromtimestamp(nxt).astimezone().isoformat(timespec="seconds")
        except (OSError, OverflowError, ValueError):
            next_iso = None
    return {
        "enabled": bool(ab["enabled"]),
        "time": ab["time"],
        "stopServer": bool(ab["stopServer"]),
        "nextRun": next_iso,
    }


def start_scheduler():
    threading.Thread(target=_scheduler_loop, daemon=True, name="pz-scheduler").start()


# ─────────────────────────── игроки ───────────────────────────


def fetch_players():
    """Список игроков по RCON-команде players."""
    raw = rcon("players")
    names = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if any(k in low for k in ("online", "players", ":", "none")):
            continue
        names.append(line)
    record_players_sample(len(names))
    return {"names": names[:64], "raw": raw[:4000], "count": len(names)}


def fetch_stats():
    st = container_state()
    if not st:
        return {"error": "контейнер не найден"}
    if not st["running"]:
        return {"error": "сервер остановлен"}
    stats = dockerlib.container_stats(config.CFG["pz_container"])
    if not stats:
        return {"error": "docker stats недоступен"}
    record_stats_sample(stats)
    return stats


def full_logs():
    """Полный лог контейнера (для скачивания файлом)."""
    code, out, err = dockerlib.sh(["docker", "logs", config.CFG["pz_container"]], timeout=120)
    return out if code == 0 else None


# ─────────────────────────── моды сервера ───────────────────────────

import re as _re  # noqa: E402

_WS_TITLES = {}  # workshop id -> title
_WS_FAIL = {}  # workshop id -> ts последней неудачи (повтор через 10 мин)
_WS_TTL = 600.0


def list_server_inis():
    """Все .ini из /data/Server (имя файла зависит от имени сервера)."""
    server_dir = os.path.join(config.CFG["data_dir"], "Server")
    try:
        return sorted(f for f in os.listdir(server_dir) if f.lower().endswith(".ini"))
    except OSError:
        return []


def _ini_value(text, key):
    """Значение ключа ini. Если строка заканчивается на ';' и следующая начинается
    с отступа — это перенос длинного значения, доклеиваем."""
    pattern = _re.compile(rf"^\s*{_re.escape(key)}\s*=(.*)$", _re.IGNORECASE)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        value = m.group(1).strip()
        j = i + 1
        while value.endswith(";") and j < len(lines) and lines[j][:1] in (" ", "\t"):
            value += lines[j].strip()
            j += 1
        return value
    return ""


def _split_list(raw):
    return [x.strip() for x in (raw or "").split(";") if x.strip()]


def parse_mods_ini(filename):
    """Читает ini и возвращает два списка: mod ID (Mods=) и Workshop ID (WorkshopItems=).
    Один Workshop-элемент может содержать несколько модов — соответствие не по индексу."""
    if os.path.basename(filename) != filename:
        return None
    path = os.path.join(config.CFG["data_dir"], "Server", filename)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    return {
        "mods": _split_list(_ini_value(text, "Mods")),
        "items": _split_list(_ini_value(text, "WorkshopItems")),
    }


def _workshop_dir():
    """Где скачан Workshop-контент внутри /data (расположение зависит от образа)."""
    for candidate in (
        os.path.join(config.CFG["data_dir"], "steamapps", "workshop", "content", "108600"),
        os.path.join(config.CFG["data_dir"], "workshop", "content", "108600"),
    ):
        if os.path.isdir(candidate):
            return candidate
    return None


_WS_EXEC = {"at": 0.0, "map": {}}  # кэш поиска mod.info внутри контейнера
_WS_EXEC_TTL = 1800.0
_WS_EXEC_RETRY = 300.0  # пауза после пустого результата (find — не дешёвый)


def _workshop_map_via_exec():
    """workshop id -> [modID,...] поиском внутри контейнера (docker exec).

    Путь к Workshop-контенту зависит от образа — если в томе /data его нет,
    находим каталог 108600 по всей ФС контейнера и читаем mod.info оттуда.
    Результат кэшируется на полчаса: find по миру — не самая дешёвая операция."""
    now = time.time()
    if _WS_EXEC["map"]:
        if now - _WS_EXEC["at"] < _WS_EXEC_TTL:
            return _WS_EXEC["map"]
    elif now - _WS_EXEC["at"] < _WS_EXEC_RETRY:
        return {}  # недавний пустой поиск: не гоняем find по всей ФС каждую минуту
    if not docker_ok_cached(ttl=600):
        return {}
    name = config.CFG["pz_container"]
    code, out, _ = dockerlib.container_exec(
        name, "find / -maxdepth 8 -type d -name 108600 2>/dev/null | head -5", timeout=120
    )
    if code != 0:
        _WS_EXEC["at"] = now - _WS_EXEC_TTL + 300.0  # повторить через 5 мин
        return {}
    mapping = {}
    for root in [ln.strip() for ln in out.splitlines() if ln.strip()][:3]:
        cmd = (
            'find "{root}" -maxdepth 4 -name mod.info 2>/dev/null | head -400 | '
            "while IFS= read -r f; do "
            'w=$(basename "$(dirname "$(dirname "$(dirname "$f")")")"); '
            'm=$(basename "$(dirname "$f")"); '
            "id=$(sed -n 's/^[Mm]od[Ii][Dd]=[ \\t\\r]*//p' \"$f\" | head -1); "
            'printf \'%s\\t%s\\t%s\\n\' "$w" "$m" "$id"; done'
        ).format(root=root)
        code, out, _ = dockerlib.container_exec(name, cmd, timeout=180)
        if code != 0:
            continue
        for ln in out.splitlines():
            parts = ln.split("\t")
            if len(parts) != 3:
                continue
            wid, folder, mid = [p.strip() for p in parts]
            if not wid.isdigit():
                continue
            lst = mapping.setdefault(wid, [])
            mod_id = mid or folder
            if mod_id and mod_id not in lst:
                lst.append(mod_id)
    if mapping:
        _WS_EXEC["map"] = mapping
        _WS_EXEC["at"] = now
        return mapping
    _WS_EXEC["map"] = {}
    _WS_EXEC["at"] = now
    return {}


def _mod_info_id(path):
    """modID= из mod.info скачанного Workshop-мода."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.lower().startswith("modid"):
                    return line.partition("=")[2].strip()
    except OSError:
        pass
    return ""


def _workshop_map_local(items):
    """workshop id -> [modID,...] из mod.info скачанных элементов (если контент на диске)."""
    wdir = _workshop_dir()
    mapping = {}
    if not wdir:
        return mapping
    for wid in items:
        mods_dir = os.path.join(wdir, str(wid), "mods")
        found = []
        if os.path.isdir(mods_dir):
            try:
                for entry in sorted(os.listdir(mods_dir)):
                    info = os.path.join(mods_dir, entry, "mod.info")
                    if os.path.isfile(info):
                        found.append(_mod_info_id(info) or entry)
            except OSError:
                pass
        if found:
            mapping[str(wid)] = found
    return mapping


def _workshop_map(items):
    """workshop id -> [modID,...]: сначала том /data, затем поиск внутри контейнера."""
    wmap = _workshop_map_local(items)
    missing = [w for w in items if str(w) not in wmap]
    if missing:
        exec_map = _workshop_map_via_exec()
        for wid in missing:
            got = exec_map.get(str(wid))
            if got:
                wmap[str(wid)] = list(got)
    return wmap


def _ws_titles(ids):
    """Названия Workshop-элементов одним запросом к Steam. Ошибки не ломают ответ."""
    now = time.time()
    need = [i for i in ids if i and i not in _WS_TITLES and now - _WS_FAIL.get(i, 0) > _WS_TTL]
    if need:
        try:
            params = "&".join(f"publishedfileids%5B{i}%5D={wid}" for i, wid in enumerate(need))
            body = f"itemcount={len(need)}&{params}".encode()
            req = urllib.request.Request(
                "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/",
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "pz-dashboard/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            for item in (payload.get("response") or {}).get("publishedfiledetails") or []:
                wid = str(item.get("publishedfileid") or "")
                title = (item.get("title") or "").strip()
                if wid and title:
                    _WS_TITLES[wid] = title
                elif wid:
                    _WS_FAIL[wid] = now
        except Exception:  # noqa: BLE001 — Steam недоступен: показываем только ID
            for wid in need:
                _WS_FAIL[wid] = now
    return _WS_TITLES


def list_mods(filename=None):
    files = list_server_inis()
    if not files:
        return {
            "ok": False,
            "error": "В /data/Server не найдено .ini файлов — проверьте монтирование каталога данных",
            "files": [],
            "file": None,
            "mods": [],
            "workshop": [],
            "pairs": [],
            "paired": False,
            "unbound": [],
            "mappingSource": None,
        }
    if filename not in files:
        filename = files[0]
    parsed = parse_mods_ini(filename)
    if parsed is None:
        return {
            "ok": False,
            "error": "Файл конфигурации не найден",
            "files": files,
            "file": filename,
            "mods": [],
            "workshop": [],
            "pairs": [],
            "paired": False,
            "unbound": [],
            "mappingSource": None,
        }
    mods, items = parsed["mods"], parsed["items"]
    _ws_titles(items)
    local_map = _workshop_map_local(items)
    wmap = dict(local_map)
    exec_used = False
    for wid in items:
        if str(wid) not in wmap:
            got = _workshop_map_via_exec().get(str(wid))
            if got:
                wmap[str(wid)] = list(got)
                exec_used = True

    workshop = []
    for wid in items:
        workshop.append(
            {
                "workshopId": wid,
                "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}"
                if wid.isdigit()
                else "",
                "title": _WS_TITLES.get(wid, ""),
                "mods": wmap.get(wid, []),
            }
        )

    bound = {m for lst in wmap.values() for m in lst}
    unbound = [m for m in mods if m not in bound]
    paired = len(mods) == len(items) and not wmap
    pairs = []
    if paired:
        for i, wid in enumerate(items):
            pairs.append(
                {
                    "mod": mods[i],
                    "workshopId": wid,
                    "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}"
                    if wid.isdigit()
                    else "",
                    "title": _WS_TITLES.get(wid, ""),
                }
            )
    return {
        "ok": True,
        "files": files,
        "file": filename,
        "workshop": workshop,
        "mods": mods,
        "unbound": unbound,
        "pairs": pairs,
        "paired": paired,
        "mappingSource": (
            ("disk" if local_map else ("container" if exec_used else None))
            or ("order" if paired else None)
        ),
    }


# ───────────────────── управление составом модов ─────────────────────

_MODS_LOCK = threading.Lock()


def mods_config_state(filename=None):
    """list_mods + реестр выключенных модов + доступность управления."""
    data = list_mods(filename)
    with _SET_LOCK:
        disabled = json.loads(json.dumps(_SETTINGS.get("modsDisabled") or {}))
    data["disabled"] = [
        {
            "workshopId": wid,
            "title": (rec or {}).get("title") or "",
            "modIds": list((rec or {}).get("modIds") or []),
        }
        for wid, rec in disabled.items()
    ]
    data["canManage"] = bool(data.get("ok") and data.get("file"))
    return data


def _ini_replace_value(text, key, values):
    """Заменяет строку Key=… (с глотанием строк-продолжений) или дописывает в конец.
    Написание ключа в файле сохраняется, разделитель — ';' как в PZ."""
    lines = text.splitlines()
    pat = _re.compile(rf"^\s*{_re.escape(key)}\s*=", _re.IGNORECASE)
    key_written = key
    for ln in lines:
        if pat.match(ln):
            key_written = ln.split("=", 1)[0].strip()
            break
    val = ";".join(values)
    out, replaced, i = [], False, 0
    while i < len(lines):
        ln = lines[i]
        if not replaced and pat.match(ln):
            out.append(f"{key_written}={val}")
            replaced = True
            orig = ln
            i += 1
            # строки-продолжения длинного значения (отступ + предыдущее оканчивалось ';')
            while orig.rstrip().endswith(";") and i < len(lines) and lines[i][:1] in (" ", "\t"):
                orig = lines[i]
                i += 1
            continue
        out.append(ln)
        i += 1
    if not replaced:
        out.append(f"{key}={val}")
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _write_mods_ini(filename, mods, items):
    """Обновляет Mods= и WorkshopItems= с .bak-копией оригинала (храним 5 последних)."""
    path = os.path.join(config.CFG["data_dir"], "Server", filename)
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    new_text = _ini_replace_value(_ini_replace_value(text, "Mods", mods), "WorkshopItems", items)
    try:
        shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        baks = sorted(
            p for p in os.listdir(os.path.dirname(path)) if p.startswith(filename + ".bak-")
        )
        for old in baks[:-5]:
            try:
                os.remove(os.path.join(os.path.dirname(path), old))
            except OSError:
                pass
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, path)


def set_mod_enabled(filename, ws_id, enable):
    """Включить/выключить Workshop-элемент в конфиге сервера.

    Выключение убирает его из WorkshopItems= и связанные modID из Mods=,
    запоминая состав в реестре пульта; включение возвращает всё обратно
    (состав берём из реестра или заново с диска/контейнера).
    Конфиг применяется рестартом сервера — пульт этого не скрывает."""
    ws_id = str(ws_id).strip()
    if not ws_id.isdigit():
        raise OpsError("Workshop ID должен быть числом")
    files = list_server_inis()
    if filename not in files:
        filename = files[0] if files else None
    if not filename:
        raise OpsError("Конфиг сервера не найден — управление модами недоступно")
    parsed = parse_mods_ini(filename)
    if parsed is None:
        raise OpsError("Не удалось прочитать конфиг сервера")
    with _MODS_LOCK:
        items = [str(x) for x in parsed["items"]]
        mods = list(parsed["mods"])
        with _SET_LOCK:
            disabled = dict(_SETTINGS.get("modsDisabled") or {})
        title = _WS_TITLES.get(ws_id) or ws_id
        if enable:
            if ws_id in items:
                disabled.pop(ws_id, None)
                with _SET_LOCK:
                    _SETTINGS["modsDisabled"] = disabled
                    _save_settings()
                return mods_config_state(filename)
            rec = disabled.get(ws_id) or {}
            mod_ids = list(rec.get("modIds") or [])
            if not mod_ids:
                mod_ids = list(_workshop_map([ws_id]).get(ws_id) or [])
            new_items = items + [ws_id]
            new_mods = mods + [m for m in mod_ids if m not in mods]
            if not mod_ids:
                log_event(
                    "warn",
                    f"Мод «{title}»: modID не определён — в конфиг "
                    f"добавлен только Workshop-элемент, проверьте загрузку",
                )
            disabled.pop(ws_id, None)
        else:
            if ws_id not in items:
                raise OpsError("Этого Workshop-элемента нет в конфиге сервера")
            mod_ids = list(_workshop_map([ws_id]).get(ws_id) or []) or list(
                (disabled.get(ws_id) or {}).get("modIds") or []
            )
            if not mod_ids:
                raise OpsError(
                    "Не удалось определить modID элемента (контент не найден) — "
                    "выключение заблокировано, чтобы не потерять состав"
                )
            new_items = [x for x in items if x != ws_id]
            new_mods = [m for m in mods if m not in mod_ids]
            disabled[ws_id] = {"title": title, "modIds": mod_ids, "at": now_iso()}
        _write_mods_ini(filename, new_mods, new_items)
        with _SET_LOCK:
            _SETTINGS["modsDisabled"] = disabled
            _save_settings()
    log_event(
        "mods",
        f"Мод «{title}» {'включён' if enable else 'выключен'} в конфиге — "
        f"применится рестартом сервера",
    )
    return mods_config_state(filename)


# ─────────────────────── обновления модов (RCON) ───────────────────────

# Сервер отвечает на RCON сразу («Checking started…»), а результат пишет
# асинхронно в лог консоли: 'CheckModsNeedUpdate: Checking...', затем либо
# 'Mods updated' (всё актуально), либо строки с 'need update' по каждому
# устаревшему моду. В B42 проверка иногда зависает — фиксируем 'inconclusive'.

_MODS_TAIL = 400
_LAST_MODS_CHECK = {"at": None, "state": None, "items": [], "error": None, "source": None}
_NEED_UPDATE_RE = re.compile(r"needs?\s+update", re.I)
_ALL_UPDATED_RE = re.compile(r"mods?\s+updated", re.I)
_IDS_RE = re.compile(r"\d{6,}")


def mods_check_state():
    state = dict(_LAST_MODS_CHECK)
    state["items"] = list(state.get("items") or [])
    return state


def _fresh_lines(before_counter, text):
    """Новые строки лога (с учётом повторов) в исходном порядке."""
    cnt = Counter(before_counter)
    out = []
    for ln in text.splitlines():
        if cnt.get(ln, 0) > 0:
            cnt[ln] -= 1
        else:
            out.append(ln)
    return out


def _parse_mods_check(lines):
    """('needs-update' | 'up-to-date' | None, строки с 'need update')."""
    need, done = [], False
    for ln in lines:
        if "CheckModsNeedUpdate" not in ln:
            continue
        if _NEED_UPDATE_RE.search(ln):
            need.append(ln)
        elif _ALL_UPDATED_RE.search(ln):
            done = True
    if need:
        return "needs-update", need
    return ("up-to-date" if done else None), []


def _mods_registry():
    """workshop id → title и mod id → workshop id (для подписи результата)."""
    ws, mods = {}, {}
    try:
        data = list_mods(None)
    except Exception:  # noqa: BLE001 — реестр нужен только для подписей
        return ws, mods
    for w in data.get("workshop") or []:
        wid = str(w.get("workshopId") or "")
        if wid:
            ws[wid] = {
                "title": w.get("title") or wid,
                "url": w.get("url")
                or f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}",
            }
            for m in w.get("mods") or []:
                mods.setdefault(str(m).strip().lower(), wid)
    return ws, mods


def _mods_items_from_lines(lines, ws):
    items, seen = [], set()
    for ln in lines:
        text = ln.split("CheckModsNeedUpdate:", 1)[-1].strip()
        wid = next((c for c in _IDS_RE.findall(text) if c in ws), None)
        item = {"raw": text[:160]}
        if wid:
            item["workshopId"] = wid
            item["title"] = ws[wid]["title"]
            item["url"] = ws[wid]["url"]
        key = wid or item["raw"]
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def check_mods_update(source="manual", timeout=45):
    """RCON-команда checkModsNeedUpdate + разбор свежих строк лога контейнера."""
    if not docker_ok_cached():
        raise OpsError("Проверка модов требует запуска пульта на хосте сервера")
    if not is_running():
        raise OpsError("Сервер не запущен — проверять моды некому")
    before_text, err = dockerlib.container_logs(config.CFG["pz_container"], _MODS_TAIL)
    if before_text is None:
        raise OpsError("Логи контейнера недоступны: " + (err or "?"))
    before = Counter(before_text.splitlines())
    rcon("checkModsNeedUpdate", quiet=True)
    deadline = time.time() + timeout
    state, need = None, []
    while time.time() < deadline:
        time.sleep(3)
        text, _ = dockerlib.container_logs(config.CFG["pz_container"], _MODS_TAIL)
        if text is None:
            continue
        state, need = _parse_mods_check(_fresh_lines(before, text))
        if state:
            break
    ws, _mods = _mods_registry()
    result = {
        "at": now_iso(),
        "source": source,
        "state": state or "inconclusive",
        "items": _mods_items_from_lines(need, ws),
        "error": None,
    }
    if not state:
        result["error"] = (
            "Сервер не вернул результат за отведённое время — "
            "известная особенность B42, попробуйте позже"
        )
    _LAST_MODS_CHECK.clear()
    _LAST_MODS_CHECK.update(result)
    if state == "needs-update":
        log_event("mods", f"Моды требуют обновления: {len(result['items'])}")
    elif state == "up-to-date":
        log_event("mods", "Моды актуальны")
    else:
        log_event("warn", "Проверка модов не завершилась — нет ответа сервера")
    return mods_check_state()


def _do_apply_mods_update(warn_seconds):
    """Применение обновлений модов: рестарт — при старте Steam докачает свежие версии."""
    _do_restart(warn_seconds, reason="Обновление модов")
    _SETTINGS["nextModsCheck"] = time.time() + get_settings()["modsUpdate"]["intervalHours"] * 3600
    _save_settings()


# ─── рескан модов после рестарта ───

_POST_RESTART = {"startedAt": None, "dueAt": None, "tries": 0}
_RESCAN_BOOT_DELAY = 120  # после рестарта даём серверу загрузиться до рескана
_RESCAN_RETRIES = 3  # повторы, если RCON ещё не поднялся после старта
_RESCAN_RETRY_DELAY = 90


def _reset_post_restart_state():
    _POST_RESTART.update({"startedAt": None, "dueAt": None, "tries": 0})


def _post_restart_rescan_tick(s):
    """Рескан модов после рестарта сервера.

    Админ мог перезапустить сервер вручную (панель, docker restart) — тогда
    обновления модов применяются этим рестартом, а таймер автопроверки уже
    не нужен. Ловим смену StartedAt контейнера, после загрузки сервера
    перепроверяем моды и, если они актуальны, сбрасываем nextModsCheck —
    авторестарт впустую не сработает."""
    mu = s.get("modsUpdate") or {}
    if not mu.get("enabled"):
        _reset_post_restart_state()
        return
    st = container_state() if docker_ok_cached() else None
    started = (st or {}).get("startedAt")
    if st and st["running"] and started:
        if _POST_RESTART["startedAt"] and started != _POST_RESTART["startedAt"]:
            # контейнер перезапустился — ждём загрузку сервера и проверяем моды
            _POST_RESTART.update({"dueAt": time.time() + _RESCAN_BOOT_DELAY, "tries": 0})
        _POST_RESTART["startedAt"] = started
    due = _POST_RESTART.get("dueAt")
    if not due or time.time() < due or op_busy():
        return
    if not is_running():
        _reset_post_restart_state()
        return
    try:
        res = check_mods_update(source="post-restart")
        _POST_RESTART["dueAt"] = None
        if res["state"] == "up-to-date":
            with _SET_LOCK:
                _SETTINGS["nextModsCheck"] = time.time() + (mu.get("intervalHours") or 6) * 3600
                _save_settings()
            log_event("mods", "После рестарта моды актуальны — таймер автопроверки сброшен")
        elif res["state"] == "needs-update":
            log_event(
                "mods",
                "После рестарта моды всё ещё требуют обновления — таймер автопроверки сохранён",
            )
    except Exception as e:  # noqa: BLE001 — RCON мог ещё не подняться после старта
        _POST_RESTART["tries"] += 1
        if _POST_RESTART["tries"] <= _RESCAN_RETRIES:
            _POST_RESTART["dueAt"] = time.time() + _RESCAN_RETRY_DELAY
        else:
            _reset_post_restart_state()
            log_event("warn", f"Рескан модов после рестарта не удался: {e}")


def overview():
    cfg = config.CFG
    docker_ok = docker_ok_cached()
    st = container_state()
    cont = None
    if st:
        cont = {
            "status": st["status"],
            "running": st["running"],
            "startedAt": _iso_ts(st["startedAt"]),
            "uptimeSec": None,
            "image": st["image"],
        }
        if st["running"] and st["startedAt"]:
            try:
                started = datetime.fromisoformat(st["startedAt"].replace("Z", "+00:00"))
                cont["uptimeSec"] = max(
                    0, int((datetime.now(timezone.utc) - started).total_seconds())
                )
            except ValueError:
                pass
    return {
        "serverName": cfg["server_name"],
        "mode": "local" if docker_ok else "remote",
        "container": cfg["pz_container"],
        "docker": docker_ok,
        "compose": compose_ok_cached() if docker_ok else False,
        "rconConfigured": bool(cfg["rcon_password"]),
        "rcon": dict(_RCON_CACHE),
        "containerInfo": cont,
        "update": {**update_state(), "local": local_digest_cached()},
        "modsCheck": mods_check_state(),
        "settings": get_settings(),
        "watchdog": watchdog_state(),
        "notify": notifylib.state(),
        # фактический образ контейнера (в remote — из конфига); ключ один,
        # без дублей: в литерале ниже его уже не повторять
        "image": _effective_image(),
        "backupsCount": len(list_backups()),
        "now": now_iso(),
    }


# ─────────────────────────── история метрик (1 ч) ───────────────────────────

_STATS_LOCK = threading.Lock()
_STATS = []  # [{"ts": iso, "cpu": float, "mem": float}]
_STATS_INTERVAL = 60


def record_stats_sample(stats):
    """Семплирует CPU/RAM не чаще раза в минуту; отрисовывается последний час."""
    with _STATS_LOCK:
        now = time.time()
        if _STATS and now - _parse_ts(_STATS[-1]["ts"]) < _STATS_INTERVAL:
            return
        _STATS.append(
            {
                "ts": now_iso(),
                "cpu": round(float(stats.get("cpuPct") or 0), 2),
                "mem": round(float(stats.get("memPct") or 0), 2),
            }
        )
        del _STATS[:-120]


def get_stats_history():
    with _STATS_LOCK:
        cutoff = time.time() - 3600
        return [p for p in _STATS if _parse_ts(p["ts"]) >= cutoff]


# ─────────────────────────── история онлайна (24 ч) ───────────────────────────

_PH_LOCK = threading.Lock()
_PH = None  # [{"ts": iso, "count": n}]
_PH_INTERVAL = 240  # семпл не чаще, чем раз в 4 минуты


def _ph_file():
    return os.path.join(config.CFG["dashboard_dir"], "players-history.json")


def _parse_ts(ts):
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return 0


def _ph_load():
    global _PH
    if _PH is None:
        try:
            with open(_ph_file(), encoding="utf-8") as f:
                data = json.load(f)
            _PH = data if isinstance(data, list) else []
        except (OSError, ValueError):
            _PH = []
    return _PH


def record_players_sample(count):
    """Копит точки онлайна: не чаще раза в 4 минуты, окно 24 часа."""
    with _PH_LOCK:
        ph = _ph_load()
        now = time.time()
        if ph and now - _parse_ts(ph[-1]["ts"]) < _PH_INTERVAL:
            return
        ph.append({"ts": now_iso(), "count": max(0, min(256, int(count)))})
        cutoff = now - 86400
        while ph and _parse_ts(ph[0]["ts"]) < cutoff:
            ph.pop(0)
        del ph[:-300]
        try:
            tmp = _ph_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(ph, f, ensure_ascii=False)
            os.replace(tmp, _ph_file())
        except OSError:
            pass


def get_players_history():
    with _PH_LOCK:
        ph = _ph_load()
        cutoff = time.time() - 86400
        return [p for p in ph if _parse_ts(p.get("ts")) >= cutoff]


# ─────────────────────────── watchdog RCON ───────────────────────────

_WD = {
    "lastProbeAt": None,
    "lastResult": None,
    "lastError": None,
    "consecutiveFailures": 0,
    "alerted": False,
    "lastRestartAt": None,
}


def watchdog_state():
    return dict(_WD)


def _watchdog_probe(wd):
    """Одна проба RCON: решает skip/fail/alert/авторестарт, мутирует _WD."""
    _WD["lastProbeAt"] = now_iso()
    st = container_state() if docker_ok_cached(ttl=120) else None
    if op_busy() or not st or not st["running"]:
        # сервер остановлен или идёт операция — это не зависание
        _WD.update(
            {"lastResult": "skipped", "lastError": None, "consecutiveFailures": 0, "alerted": False}
        )
        return
    try:
        rconlib.run_command(
            config.CFG["rcon_host"], config.CFG["rcon_port"], config.CFG["rcon_password"], "players"
        )
        _WD.update(
            {"lastResult": "ok", "lastError": None, "consecutiveFailures": 0, "alerted": False}
        )
    except rconlib.RCONError as e:
        _WD["lastResult"] = "fail"
        _WD["lastError"] = str(e)
        _WD["consecutiveFailures"] += 1
        silent_min = _WD["consecutiveFailures"] * 30 / 60
        if silent_min >= wd["thresholdMin"] and not _WD["alerted"]:
            _WD["alerted"] = True
            log_event("warn", f"Watchdog: RCON не отвечает {silent_min:.0f} мин — {e}")
            cooldown = max(15, int(wd["thresholdMin"]) * 3) * 60
            cooled = not _WD["lastRestartAt"] or time.time() - _WD["lastRestartAt"] >= cooldown
            if wd["autoRestart"] and not cooled:
                log_event("warn", "Watchdog: рестарт недавно был — ждём кулдауна")
            elif wd["autoRestart"] and docker_ok_cached() and is_running() and not op_busy():
                log_event("warn", "Watchdog: авторестарт зависшего сервера")
                _WD["lastRestartAt"] = time.time()
                try:
                    start_op("restart", lambda: _do_restart(0))
                except OpsError:
                    pass


def _watchdog_loop():
    """Раз в 30 с проба RCON (детали в _watchdog_probe)."""
    while True:
        try:
            wd = get_settings()["watchdog"]
            if wd["enabled"]:
                _watchdog_probe(wd)
        except Exception as e:  # noqa: BLE001
            log_event("error", "Watchdog: " + str(e))
        time.sleep(30)


def start_watchdog():
    threading.Thread(target=_watchdog_loop, daemon=True, name="pz-watchdog").start()
