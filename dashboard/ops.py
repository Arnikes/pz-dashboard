#!/usr/bin/env python3
"""Ядро пульта: события, настройки, операции (старт/стоп/бэкапы/обновления),
планировщик автообновления. Всё на стандартной библиотеке."""

import json
import gzip
import math
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zlib
from collections import Counter, deque
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import islice

import config
import i18n
import fileio
import settingsmodel
from settingsmodel import DEFAULTS as _DEFAULTS
from errors import OpsError as OpsError, OpsErrorReported as OpsErrorReported
import dockerlib
import dashboardupdate
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
        _load_events()
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


def _reverse_lines(path, chunk_size=65536):
    """Read a JSONL tail in fixed blocks, newest first, without scanning its prefix."""
    with open(path, "rb") as source:
        position = source.seek(0, os.SEEK_END)
        pending = b""
        while position:
            size = min(chunk_size, position)
            position -= size
            source.seek(position)
            lines = (source.read(size) + pending).split(b"\n")
            pending = lines[0]
            for line in reversed(lines[1:]):
                if line:
                    yield line.decode("utf-8", errors="replace")
        if pending:
            yield pending.decode("utf-8", errors="replace")


def _load_events():
    if not _EV_MEM:
        # Load before the first new event, including the startup event.
        try:
            for line in islice(_reverse_lines(config.CFG["events_file"]), 200):
                try:
                    entry = json.loads(line)
                    if isinstance(entry, dict):
                        _EV_MEM.append(entry)
                except ValueError:
                    pass
        except OSError:
            pass


def get_events(limit=100):
    with _EV_LOCK:
        _load_events()
        return list(islice(_EV_MEM, max(0, limit)))


# ─────────────────────────── настройки ───────────────────────────

_SET_LOCK = threading.RLock()
# рабочая копия настроек: мутируется в рантайме, _DEFAULTS остаётся эталоном
_SETTINGS = deepcopy(_DEFAULTS)
_SETTINGS_VERSION = {"epoch": uuid.uuid4().hex, "revision": 0}


def _next_daily_run(time_str, now=None):
    """Следующий суточный запуск в «ЧЧ:ММ» по локальному времени пульта."""
    hh, mm = (
        settingsmodel.norm_hhmm(time_str).split(":")
        if settingsmodel.valid_hhmm(time_str)
        else ("3", "0")
    )
    base = datetime.fromtimestamp(now if now is not None else time.time())
    run = base.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if run <= base:
        run += timedelta(days=1)
    return run.timestamp()


def _load_settings():
    try:
        with open(config.CFG["settings_file"], encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("Настройки должны быть объектом")
        settingsmodel.merge_loaded(_SETTINGS, data)
    except (OSError, ValueError):
        pass
    settingsmodel.normalize_loaded(_SETTINGS)


def _save_settings():
    with _SET_LOCK:
        try:
            fileio.atomic_write(
                config.CFG["settings_file"],
                json.dumps(_SETTINGS, ensure_ascii=False, indent=2).encode("utf-8"),
                prefix=".settings-",
            )
        except OSError as error:
            raise OpsError(
                "Не удалось сохранить настройки пульта. Проверьте место и права записи"
            ) from error


def _set_schedule(key, value):
    with _SET_LOCK:
        previous = _SETTINGS[key]
        _SETTINGS[key] = value
        try:
            _save_settings()
        except OpsError:
            _SETTINGS[key] = previous
            raise


def get_settings():
    with _SET_LOCK:
        data = deepcopy(_SETTINGS)
        data["version"] = dict(_SETTINGS_VERSION)
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
        return deepcopy(_SETTINGS.get("telegram") or {})


def patch_settings(patch):
    """Validate a detached candidate, then publish and persist it under one lock."""
    with _SET_LOCK:
        updated, error = settingsmodel.prepare_patch(_SETTINGS, patch, _next_daily_run)
        if error:
            return error
        previous = _SETTINGS.copy()
        _SETTINGS.update(updated)
        try:
            _save_settings()
        except OpsError:
            _SETTINGS.clear()
            _SETTINGS.update(previous)
            raise
        _SETTINGS_VERSION["revision"] += 1
        return None


# ─────────────────────────── RCON-хелперы ───────────────────────────

_PLAYER_LANGUAGE = ContextVar("player_notification_language", default=None)


def player_notification_language():
    """Use the operation's snapshot, or the persisted preference for direct calls."""
    snapshot = _PLAYER_LANGUAGE.get()
    if snapshot is not None:
        return snapshot
    with _SET_LOCK:
        value = (_SETTINGS.get("playerNotifications") or {}).get("language")
    return "ru" if value == "ru" else "en"


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
        if (
            not quiet
            and not op_busy()
            and not _watchdog_in_grace()
            and (was_ok or now - _RCON_LOG["at"] >= 300)
        ):
            _RCON_LOG["at"] = now
            log_event("rcon-error", "RCON: " + str(e))
        raise


def rcon_warn_broadcast(seconds, reason, abort_check=None, abort_wait=None):
    """Отправляет игрокам отсчёт перед остановкой. Возвращает False при сбое RCON.

    abort_check — вызывается перед каждым шагом отсчёта; истинный результат
    прерывает рассылку (например, сервер уже перезапустили вручную).
    abort_wait — прерываемое ожидание шага, возвращающее True при отмене."""
    if seconds <= 0:
        return True
    with _OP_LOCK:
        _ACTIVE["countdownEndsAt"] = time.time() + seconds
    locale = player_notification_language()
    reason = i18n.translate(reason, locale=locale)
    thresholds = {30, 10}
    thresholds.update(range(60, seconds + 1, 60))
    if seconds < 10:
        thresholds.add(seconds)  # совсем короткий отсчёт всё равно слышен
    last_sent = None
    ok = True
    # шаг 10 с, старт выровнен вниз до кратности: иначе (например 45 с)
    # отсчёт молча пропускает все пороги и сервер останавливается без предупреждения
    start = seconds if seconds < 10 else seconds - (seconds % 10)
    if seconds > start:
        if abort_wait is not None:
            if abort_wait(seconds - start):
                return False
        else:
            time.sleep(seconds - start)
    for left in range(start, 0, -10):
        if abort_check is not None and abort_check():
            return ok
        if ok and left in thresholds and left != last_sent:
            count = left // 60 if left >= 60 else left
            source = "{{0}} через {{1}} минут" if left >= 60 else "{{0}} через {{1}} секунд"
            text = i18n.message(source, reason, count, locale=locale, count=count)
            try:
                rcon(f'servermsg "{text}"', quiet=True)
                last_sent = left
            except rconlib.RCONError:
                ok = False
        if abort_wait is not None:
            if abort_wait(min(10, left)):
                return ok
        else:
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
        if st and st.get("running") is False:
            return "stopped"
        if st and started_at and st.get("startedAt") and st["startedAt"] != started_at:
            return "resurrected"
        time.sleep(5)
        waited += 5
    state = container_state()
    return "stopped" if state and state.get("running") is False else "running"


def wait_until_running(timeout=120):
    waited = 0
    while waited < timeout:
        if is_running():
            return True
        time.sleep(4)
        waited += 4
    return is_running()


def wait_until_ready(timeout=600):
    """Keep lifecycle operations active until the game answers, not just Docker."""
    _set_phase("Загрузка сервера", "Ожидание готовности PZ и RCON; подробности в логах сервера")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running():
            try:
                reply = rcon("players", quiet=True)
                if isinstance(reply, str) and re.search(
                    r"\bPlayers\s+connected\s*\(\d+\)", reply, re.I
                ):
                    return True
            except rconlib.RCONError:
                pass
        time.sleep(3)
    return False


def _require_ready():
    if not wait_until_ready():
        raise OpsError(
            "PZ/RCON не готов после запуска. Проверьте логи; автоматического повторного рестарта не будет"
        )


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


_OP_LOCK = threading.Lock()
_ACTIVE = {
    "op": None,
    "phase": "",
    "message": "",
    "startedAt": None,
    "cancellable": False,
    "cancelRequested": False,
    "countdownEndsAt": None,
    "stages": [],
}
_OP_CANCEL = threading.Event()
_OP_HISTORY = deque(maxlen=10)

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def op_state():
    with _OP_LOCK:
        dashboard_op = dashboardupdate.operation()
        active = {**_ACTIVE, "stages": list(_ACTIVE.get("stages", []))} if _ACTIVE["op"] else None
        history = list(_OP_HISTORY)
        if dashboard_op:
            if dashboard_op.get("active"):
                active = dashboard_op
            elif not any(row["id"] == dashboard_op["id"] for row in history):
                history.append(dashboard_op)
                history.sort(
                    key=lambda row: row.get("finishedAt", row.get("startedAt", "")), reverse=True
                )
        return {
            "active": active,
            "history": history,
        }


def op_busy():
    with _OP_LOCK:
        return _ACTIVE["op"] is not None or bool((dashboardupdate.operation() or {}).get("active"))


def cancel_mods_update():
    """Отменить текущий авторестарт модов до начала остановки сервера."""
    with _OP_LOCK:
        if _ACTIVE["op"] != "mods-restart":
            raise OpsError("Автообновление модов сейчас не выполняется")
        if _ACTIVE["cancelRequested"]:
            return
        if not _ACTIVE["cancellable"]:
            raise OpsError("Сервер уже останавливается — отменить обновление модов нельзя")
        _ACTIVE["cancelRequested"] = True
        _ACTIVE["cancellable"] = False
        _ACTIVE["phase"] = "Отмена"
        _ACTIVE["message"] = "Отмена автообновления модов…"
        _ACTIVE["countdownEndsAt"] = None
        _ACTIVE.setdefault("stages", []).append("Отмена")
        _OP_CANCEL.set()


def _set_phase(phase, message="", *, countdown_seconds=None):
    with _OP_LOCK:
        _ACTIVE["phase"] = phase
        _ACTIVE["message"] = message
        _ACTIVE["countdownEndsAt"] = (
            time.time() + countdown_seconds if countdown_seconds is not None else None
        )
        stages = _ACTIVE.setdefault("stages", [])
        if phase not in {"Подготовка…", "Готово"} and (not stages or stages[-1] != phase):
            stages.append(phase)


def _start_worker(op, fn):
    notification_language = player_notification_language()
    operation_id = uuid.uuid4().hex

    def worker():
        language_token = _PLAYER_LANGUAGE.set(notification_language)
        try:
            result = fn()
            if result == "handoff":
                return
            with _OP_LOCK:
                _OP_HISTORY.appendleft(
                    {
                        "id": operation_id,
                        "op": op,
                        "ok": True,
                        "message": _ACTIVE["message"],
                        "finishedAt": now_iso(),
                        **({"cancelled": True} if result in ("cancelled", "aborted") else {}),
                        **(
                            {"archive": result}
                            if op == "verify-backup" and isinstance(result, dict)
                            else {}
                        ),
                    }
                )
        except OpsErrorReported as e:
            # событие уже записал источник (run_backup_job) — фиксируем только статус
            with _OP_LOCK:
                _ACTIVE["message"] = str(e)
                _OP_HISTORY.appendleft(
                    {
                        "id": operation_id,
                        "op": op,
                        "ok": False,
                        "message": str(e),
                        "finishedAt": now_iso(),
                    }
                )
        except OpsError as e:
            log_event("error", f"Операция «{op}» не удалась: {e}")
            with _OP_LOCK:
                _ACTIVE["message"] = str(e)
                _OP_HISTORY.appendleft(
                    {
                        "id": operation_id,
                        "op": op,
                        "ok": False,
                        "message": str(e),
                        "finishedAt": now_iso(),
                    }
                )
        except Exception as e:  # noqa: BLE001 — не роняем поток
            log_event("error", f"Операция «{op}»: {e}")
            with _OP_LOCK:
                _ACTIVE["message"] = f"Внутренняя ошибка: {e}"
                _OP_HISTORY.appendleft(
                    {
                        "id": operation_id,
                        "op": op,
                        "ok": False,
                        "message": str(e),
                        "finishedAt": now_iso(),
                    }
                )
        finally:
            _PLAYER_LANGUAGE.reset(language_token)
            with _OP_LOCK:
                _ACTIVE["op"] = None
                _ACTIVE["phase"] = ""
                _ACTIVE["startedAt"] = None
                _ACTIVE["cancellable"] = False
                _ACTIVE["cancelRequested"] = False
                _ACTIVE["countdownEndsAt"] = None
                _ACTIVE["stages"] = []
                _OP_CANCEL.clear()

    with _OP_LOCK:
        if _ACTIVE["op"] or (dashboardupdate.operation() or {}).get("active"):
            raise OpsError("Уже выполняется другая операция, подождите")
        _OP_CANCEL.clear()
        _ACTIVE.update(
            {
                "op": op,
                "phase": "Подготовка…",
                "message": "",
                "startedAt": now_iso(),
                "cancellable": op == "mods-restart",
                "cancelRequested": False,
                "countdownEndsAt": None,
                "stages": ["Подготовка…"],
            }
        )
    t = threading.Thread(target=worker, daemon=True, name=f"op-{op}")
    t.start()


def start_op(op, fn):
    """Запустить операцию в фоне. Бросает OpsError, если занято."""
    _start_worker(op, fn)


# ─── старт / стоп / рестарт ───


def _start_container():
    """Give controlled launches time to initialize PZ and RCON."""
    _begin_watchdog_grace()
    return dockerlib.container_start(config.CFG["pz_container"])


def _do_start():
    if is_running():
        raise OpsError("Сервер уже запущен")
    _set_phase("Запуск", "docker start")
    code, out, err = _start_container()
    if code != 0:
        raise OpsError(f"Не удалось запустить: {err or out}")
    if not wait_until_running(150):
        raise OpsError("Сервер не запустился за 150 с — проверьте логи контейнера")
    _require_ready()
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


def _do_restart(
    warn_seconds, reason="Перезапуск сервера", guard_restarted=False, cancellable=False
):
    if not is_running():
        raise OpsError("Сервер не запущен — сначала запустите его")
    # guard_restarted: авторестарт (моды) не должен дублировать ручной рестарт
    # админа — если за время отсчёта контейнер уже перезапустили, отменяемся
    guard_at = (container_state() or {}).get("startedAt") if guard_restarted else None

    def _guard_tripped():
        if cancellable and _OP_CANCEL.is_set():
            return True
        return _restarted_since(guard_at) if guard_restarted else None

    if warn_seconds > 0:
        _set_phase("Предупреждение игроков", f"отсчёт {warn_seconds} с")
        wait_options = {"abort_wait": _OP_CANCEL.wait} if cancellable else {}
        rcon_warn_broadcast(warn_seconds, reason, abort_check=_guard_tripped, **wait_options)
    if cancellable:
        # Запрос отмены и переход к остановке используют один замок:
        # принятая отмена гарантирует, что graceful_stop не будет вызван.
        with _OP_LOCK:
            cancelled = _OP_CANCEL.is_set()
            _ACTIVE["cancellable"] = False
        if cancelled:
            msg = "Автообновление модов отменено администратором"
            log_event("auto", msg)
            if warn_seconds > 0:
                try:
                    rcon(
                        'servermsg "'
                        + i18n.translate(
                            "Обновление модов отменено. Сервер продолжает работу.",
                            locale=player_notification_language(),
                        )
                        + '"',
                        quiet=True,
                    )
                except rconlib.RCONError:
                    pass
            _set_phase("Отменено", msg)
            return "cancelled"
    if guard_restarted:
        verdict = _restarted_since(guard_at)
        if verdict:
            msg = (
                "Сервер уже перезапущен вручную — авторестарт отменён"
                if verdict == "restarted"
                else "Сервер остановлен вручную — авторестарт отменён"
            )
            log_event("auto", msg)
            if verdict == "restarted":
                _require_ready()
            _set_phase("Отменено", msg)
            return "aborted"
    _begin_watchdog_grace()
    if graceful_stop(lambda m: _set_phase("Остановка", m)) == "resurrected":
        # docker сам поднял контейнер (restart policy) — рестарт уже случился,
        # остаётся дождаться запуска сервера
        _begin_watchdog_grace()
        if not wait_until_running(150):
            raise OpsError("Сервер не запустился после рестарта за 150 с")
        _require_ready()
        log_event("restart", f"Сервер перезапущен ({reason})")
        _set_phase("Готово", "Сервер перезапущен")
        return
    _set_phase("Запуск", "docker start")
    code, out, err = _start_container()
    if code != 0:
        raise OpsError(f"Не удалось запустить после рестарта: {err or out}")
    if not wait_until_running(150):
        raise OpsError("Сервер не запустился после рестарта за 150 с")
    _require_ready()
    log_event("restart", f"Сервер перезапущен ({reason})")
    _set_phase("Готово", "Сервер перезапущен")


# ─── обновления ───


def _effective_image():
    """Фактический образ контейнера (если пульт на хосте) или из конфига.
    Реальный образ может отличаться от дефолта — проверяем то, что запущено."""
    return _image_from_state(container_state())


def _image_from_state(st):
    img = (st or {}).get("image") or config.CFG["pz_image"]
    if ":" not in img.rsplit("/", 1)[-1]:
        img = img + ":latest"
    return img


def _installed_digest(image):
    # A tag can already point to a newer pull while the existing container
    # continues using its old image. Compare against that immutable image ID.
    state = container_state()
    return dockerlib.image_digests((state or {}).get("imageId") or image)


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
    local = _installed_digest(image)
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


def _do_check_update():
    _set_phase("Проверка актуального образа", "Docker Hub")
    result = check_update(force_event=True)
    if result.get("error") or result.get("available") is None:
        raise OpsError(
            result.get("error") or result.get("note") or "Не удалось сравнить версии образа"
        )
    _set_phase(
        "Готово", "Доступно обновление образа" if result["available"] else "Образ уже актуален"
    )
    return result


def _do_check_dashboard_update():
    _set_phase("Проверка обновлений пульта")
    result = dashboardupdate.check()
    if result.get("error") or result.get("available") is None:
        raise OpsError(result.get("error") or "Не удалось сравнить версии образа")
    _set_phase(
        "Готово", "Доступна новая версия" if result["available"] else "Образ пульта уже актуален"
    )
    return result


_LOCAL_DIGEST = {"digest": None, "image": None, "imageId": None, "at": 0.0}


def local_digest_cached(ttl=60, image=None, image_id=None):
    """Локальный digest считается сам по себе (TTL-кэш), а не только по кнопке «Проверить»."""
    now = time.time()
    if (
        now - _LOCAL_DIGEST["at"] > ttl
        or image is not None
        and image != _LOCAL_DIGEST["image"]
        or image_id is not None
        and image_id != _LOCAL_DIGEST.get("imageId")
    ):
        if image is None:
            state = container_state()
            image, image_id = _image_from_state(state), (state or {}).get("imageId")
        _LOCAL_DIGEST.update(
            {
                "digest": dockerlib.image_digests(image_id or image),
                "image": image,
                "imageId": image_id,
                "at": now,
            }
        )
    return _LOCAL_DIGEST["digest"]


def _do_apply_update(warn_seconds, reason="Обновление сервера"):
    _set_phase("Проверка актуального образа", "Docker Hub")
    try:
        image = _effective_image()
        before = _installed_digest(image)
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
        _begin_watchdog_grace()
        if was_running:
            graceful_stop(lambda m: _set_phase("Остановка сервера", m))
        _set_phase("Пересоздание контейнера", "docker compose up -d")
        _begin_watchdog_grace()
        code, out, err = dockerlib.compose_up(config.CFG)
        if code != 0:
            # пробуем поднять старый контейнер обратно
            start_code, _, _ = _start_container()
            recovered = start_code == 0 and wait_until_ready()
            recovery_message = (
                "Сервер снова готов к работе, но обновление не подтверждено"
                if recovered
                else "Готовность сервера не подтверждена — проверьте логи и конфиг"
            )
            raise OpsError(f"docker compose up не удался: {(err or out)[:200]}. {recovery_message}")
        if not wait_until_running(180):
            raise OpsError("Контейнер пересоздан, но не поднялся — проверьте docker logs pzserver")
        _require_ready()
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


def get_backup_journal(limit=50, offset=0):
    """Последние записи журнала запусков бэкапов — новые сверху."""
    out = []
    if limit <= 0:
        return out
    skipped = 0
    try:
        with _BJ_LOCK:
            for line in _reverse_lines(_journal_path()):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                out.append(entry)
                if len(out) >= limit:
                    break
    except OSError:
        pass
    return out


# ─────────────────────────── бэкапы ───────────────────────────


def _backup_paths():
    return config.CFG["backup_dir"], config.CFG["data_dir"]


def list_backups():
    bdir = config.CFG["backup_dir"]
    items = []
    try:
        with os.scandir(bdir) as entries:
            for entry in entries:
                if not entry.name.endswith(".tar.gz") or not entry.is_file():
                    continue
                st = entry.stat()
                items.append(
                    (
                        st.st_mtime_ns,
                        {
                            "name": entry.name,
                            "size": st.st_size,
                            "sizeText": fmt_size(st.st_size),
                            "mtime": datetime.fromtimestamp(st.st_mtime)
                            .astimezone()
                            .isoformat(timespec="seconds"),
                        },
                    )
                )
    except OSError:
        return []
    items.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in items]


def count_backups():
    """Count fresh archive entries without allocating, formatting or sorting a list."""
    try:
        with os.scandir(config.CFG["backup_dir"]) as entries:
            return sum(entry.name.endswith(".tar.gz") and entry.is_file() for entry in entries)
    except OSError:
        return 0


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


def _create_backup_archive(bdir, ddir):
    """Публиковать архив только после успешного tar, сохраняя предыдущие копии."""
    base = time.strftime("pz-backup-%Y%m%d-%H%M%S")
    name = base + ".tar.gz"
    suffix = 1
    while os.path.lexists(os.path.join(bdir, name)):
        name = f"{base}-{suffix}.tar.gz"
        suffix += 1
    dest = os.path.join(bdir, name)
    temp_path = None
    _set_phase("Создание архива", name)
    try:
        fd, temp_path = tempfile.mkstemp(prefix=".pz-backup-", suffix=".tmp", dir=bdir)
        os.close(fd)
        proc = subprocess.run(
            [
                "tar",
                "-czf",
                temp_path,
                "--exclude=[Ll][Oo][Gg][Ss]",
                "--exclude=*.[Ll][Oo][Gg]",
                "--exclude=*.[Ll][Oo][Gg].*",
                "--exclude=console.txt*",
                "--exclude=*-console.txt*",
                "--exclude=*DebugLog*.txt*",
                "-C",
                ddir,
                ".",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2400,
        )
        if proc.returncode != 0:
            raise OpsError(f"tar не удался: {(proc.stderr or proc.stdout)[:200]}")
        size = os.path.getsize(temp_path)
        os.replace(temp_path, dest)
        return name, dest, size
    except subprocess.TimeoutExpired as error:
        raise OpsError("Архив не создан: tar не уложился в таймаут (2400 с)") from error
    except OSError as error:
        raise OpsError(f"Архив не создан: {error}") from error
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError as error:
                log_event("warn", f"Не удалось удалить временный архив: {error}")


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
        _begin_watchdog_grace()
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
            _set_phase("Ожидание записи", "10 с на сохранение", countdown_seconds=10)
            time.sleep(10)
    archive_error = None
    try:
        name, dest, size = _create_backup_archive(bdir, ddir)
    except OpsError as error:
        archive_error = error
        raise
    finally:
        if stop_server and was_running:
            _set_phase("Запуск сервера", "docker start")
            code, out, err = _start_container()
            restart_error = None
            if code != 0:
                restart_error = f"Не удалось запустить сервер после бэкапа: {err or out}"
            elif not wait_until_running(150):
                restart_error = "Сервер не запустился после бэкапа за 150 с"
            elif not wait_until_ready():
                restart_error = "PZ/RCON не готов после бэкапа. Проверьте логи сервера"
            if restart_error:
                message = f"{archive_error}. {restart_error}" if archive_error else restart_error
                raise OpsError(message) from archive_error
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
    except (OpsError, OSError) as e:
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
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or not name.endswith(".tar.gz"):
        raise OpsError("Некорректное имя бэкапа")
    path = os.path.join(config.CFG["backup_dir"], name)
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(config.CFG["backup_dir"]) + os.sep):
        raise OpsError("Некорректный путь бэкапа")
    if not os.path.isfile(real):
        raise OpsError("Файл бэкапа не найден")
    return real


def _validate_backup_gzip(path, timeout=900):
    """Check CRCs and trailers with fixed-size reads, including concatenated gzip."""
    deadline = time.monotonic() + timeout
    with gzip.open(path, "rb") as compressed:
        while compressed.read(64 * 1024):
            if time.monotonic() >= deadline:
                raise OpsError("Проверка не удалась: архив не уложился в таймаут (900 с)")


def _extract_backup(path, dest):
    """Проверить весь gzip и распаковать только безопасные члены архива."""

    def backup_filter(member, target):
        safe = tarfile.data_filter(member, target)
        # Мир должен сохранить числовых владельцев файлов из бэкапа.
        return safe.replace(uid=member.uid, gid=member.gid, uname=None, gname=None)

    try:
        # Validate the entire gzip, including its CRC/trailer, before extraction.
        # A tar listing buffers every filename and some tar implementations stop
        # at the tar end marker without checking the gzip trailer.
        _validate_backup_gzip(path)
        with tarfile.open(path, "r:gz") as archive:
            archive.extractall(dest, filter=backup_filter)
        if not any(files for _, _, files in os.walk(dest)):
            raise OpsError("Архив пуст — восстанавливаться из него нечем")
    except (tarfile.TarError, OSError, EOFError, ValueError, zlib.error) as error:
        raise OpsError(f"Архив повреждён или небезопасен: {error}") from error


def _restore_files(dest):
    """Stage on the data volume and retain the original world until publication succeeds."""
    data = config.CFG["data_dir"]
    staged = tempfile.mkdtemp(prefix=".pz-restore-", dir=data)
    previous = None
    moved, installed = [], []
    retain = False
    try:
        shutil.copytree(dest, staged, symlinks=True, dirs_exist_ok=True)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            for root, dirs, files in os.walk(dest):
                for entry in [".", *dirs, *files]:
                    source = os.path.join(root, entry)
                    target = os.path.join(staged, os.path.relpath(source, dest))
                    stat = os.stat(source, follow_symlinks=False)
                    os.chown(target, stat.st_uid, stat.st_gid, follow_symlinks=False)
        previous = tempfile.mkdtemp(prefix=".pz-previous-", dir=data)
        reserved = {os.path.basename(staged), os.path.basename(previous)}
        try:
            for entry in os.listdir(data):
                if entry not in reserved:
                    os.replace(os.path.join(data, entry), os.path.join(previous, entry))
                    moved.append(entry)
            for entry in os.listdir(staged):
                os.replace(os.path.join(staged, entry), os.path.join(data, entry))
                installed.append(entry)
        except OSError as error:
            try:
                for entry in reversed(installed):
                    os.replace(os.path.join(data, entry), os.path.join(staged, entry))
                for entry in reversed(moved):
                    os.replace(os.path.join(previous, entry), os.path.join(data, entry))
            except OSError as rollback_error:
                retain = True
                raise OpsError(
                    f"Восстановление прервано: {error}. Откат не завершён: {rollback_error}. "
                    f"Исходные данные сохранены в {previous}; новые — в {staged}. "
                    "Сервер оставлен остановленным"
                ) from error
            raise OpsError("Запись бэкапа не удалась. Исходный мир восстановлен") from error
    finally:
        if not retain:
            for path in (staged, previous):
                if path is not None:
                    try:
                        shutil.rmtree(path)
                    except OSError as error:
                        log_event("warn", f"Не удалось удалить временные данные {path}: {error}")


def _restore_prepared(dest, name):
    if is_running():
        _set_phase("Предупреждение игроков", "отсчёт 60 с")
        rcon_warn_broadcast(60, "Восстановление из бэкапа")
        _begin_watchdog_grace()
        if graceful_stop(lambda m: _set_phase("Остановка сервера", m)) == "resurrected":
            # docker сам поднял контейнер посреди остановки — стирать данные
            # под живым сервером нельзя: мир будет в записи
            raise OpsError(
                "Docker сам перезапустил контейнер — восстановление прервано, повторите попытку"
            )
    state = container_state()
    if not state or state.get("running") is not False:
        raise OpsError("Остановка сервера не подтверждена Docker — восстановление отменено")
    _set_phase("Восстановление файлов", name)
    _restore_files(dest)
    _set_phase("Запуск сервера", "docker start")
    code, out, err = _start_container()
    if code != 0:
        raise OpsError(f"Данные восстановлены, но запуск не удался: {err or out}")
    if not wait_until_running(180):
        raise OpsError("Данные восстановлены, но сервер не запустился за 180 с")
    _require_ready()
    log_event("restore", f"Мир восстановлен из {name}")
    _set_phase("Готово", f"Восстановлено из {name}")


def _do_restore(name):
    path = _validate_backup_name(name)
    if not os.path.isdir(config.CFG["data_dir"]):
        raise OpsError("Каталог данных PZ не смонтирован")
    os.makedirs(config.CFG["dashboard_dir"], exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(
            prefix="restore-tmp-", dir=config.CFG["dashboard_dir"]
        ) as dest:
            _set_phase("Проверка архива перед восстановлением", name)
            _extract_backup(path, dest)
            _restore_prepared(dest, name)
    except OSError as error:
        raise OpsError(f"Восстановление не удалось: {error}") from error


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
    _set_phase("Проверка и распаковка в песочницу", name)
    os.makedirs(config.CFG["dashboard_dir"], exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="verify-tmp-", dir=config.CFG["dashboard_dir"]) as dest:
        _extract_backup(path, dest)
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
                if rel.startswith("Maps/") or (
                    rel.startswith("Saves/Multiplayer/")
                    and (fn == "map.bin" or fn.startswith("map_"))
                    and fn.endswith(".bin")
                ):
                    has_map = True
        if not files:
            raise OpsError("Архив распаковался, но файлов внутри нет")
        notes = []
        if not has_ini:
            notes.append("нет Server/*.ini")
        if not has_map:
            notes.append("нет данных мира (Maps/ или Saves/Multiplayer/)")
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
        _set_schedule("nextBackupRun", nxt)
    if now < nxt or op_busy():
        return
    late = (now - nxt) / 60
    note = " (навёрстывание)" if late > 15 else ""
    _set_schedule("nextBackupRun", _next_daily_run(t, now))
    log_event("auto", f"Автобэкап по расписанию{note}: запуск ({t})")
    try:
        start_op("backup", lambda: run_backup_job("scheduled", bool(ab.get("stopServer"))))
    except OpsError:
        # гонка: между проверкой и стартом началась другая операция —
        # откатываем время, попытка повторится на следующем тике (20 с)
        _set_schedule("nextBackupRun", nxt)


def _scheduler_loop():
    # Local import avoids the editor/operations module initialization cycle.
    import configeditor

    while True:
        try:
            configeditor.auto_verify_running()
            s = get_settings()
            au = s["autoUpdate"]
            if au["enabled"]:
                nxt = s.get("nextCheck")
                now = time.time()
                if not nxt:
                    nxt = now + au["intervalHours"] * 3600
                    _set_schedule("nextCheck", nxt)
                if now >= nxt and not op_busy():
                    try:
                        check_update(force_event=False)
                        st = update_state()
                        if st.get("available"):
                            log_event("auto", "Автообновление: найдена новая версия")
                            start_op(
                                "apply-update",
                                lambda w=au["warnSeconds"]: _do_apply_update(
                                    w, "Автообновление сервера"
                                ),
                            )
                    except OpsError as e:
                        log_event("error", "Автообновление: " + str(e))
                    _set_schedule("nextCheck", time.time() + au["intervalHours"] * 3600)
            mu = s.get("modsUpdate") or {}
            if mu.get("enabled"):
                nxt_m = s.get("nextModsCheck")
                now = time.time()
                if not nxt_m:
                    nxt_m = now + mu["intervalHours"] * 3600
                    _set_schedule("nextModsCheck", nxt_m)
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
                                            w,
                                            reason="Обновление модов",
                                            guard_restarted=True,
                                            cancellable=True,
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
                    _set_schedule("nextModsCheck", time.time() + mu["intervalHours"] * 3600)
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
    _set_schedule("nextCheck", time.time() + hours * 3600)


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
        "timeZone": datetime.now().astimezone().isoformat()[-6:],
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
        if line.startswith("-"):
            # RCON prefixes player entries with one list marker, not part of the name.
            name = line[1:]
            if name:
                names.append(name)
            continue
        low = line.lower()
        if any(k in low for k in ("online", "players", ":", "none")):
            continue
        names.append(line)
    record_players_sample(len(names))
    return {"names": names[:64], "raw": raw[:4000], "count": len(names)}


def fetch_stats(*, state_provider=None):
    st = (state_provider or container_state)()
    if not st:
        return {"error": "контейнер не найден"}
    if not st["running"]:
        return {"error": "сервер остановлен"}
    stats = dockerlib.container_stats(config.CFG["pz_container"])
    if not stats:
        return {"error": "docker stats недоступен"}
    record_stats_sample(stats)
    return stats


def full_logs(destination):
    """Полный лог контейнера (для скачивания файлом)."""
    return dockerlib.container_logs_to_file(config.CFG["pz_container"], destination)


# ─────────────────────────── моды сервера ───────────────────────────

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
    # Lazy import avoids a cycle: editor operations use the shared operation worker.
    import configeditor

    try:
        return configeditor.mod_state(filename)
    except (configeditor.EditorError, ValueError, OSError) as error:
        return {
            "ok": False,
            "error": str(error),
            "files": list_server_inis(),
            "file": filename,
            "workshop": [],
            "mods": [],
            "unbound": [],
        }


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
    """Workshop IDs, titles and links for update results."""
    ws = {}
    try:
        data = list_mods(None)
    except Exception:  # noqa: BLE001 — реестр нужен только для подписей
        return ws
    for w in data.get("workshop") or []:
        wid = str(w.get("workshopId") or "")
        if wid:
            ws[wid] = {
                "title": w.get("title") or wid,
                "url": w.get("url")
                or f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}",
            }
    return ws


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
    if source == "manual":
        _set_phase("Проверка модов", "RCON checkModsNeedUpdate")
    if not docker_ok_cached():
        raise OpsError("Проверка модов требует запуска пульта на хосте сервера")
    if not is_running():
        raise OpsError("Сервер не запущен — проверять моды некому")
    before_text, err = dockerlib.container_logs(config.CFG["pz_container"], _MODS_TAIL)
    if before_text is None:
        raise OpsError("Логи контейнера недоступны: " + (err or "?"))
    before = Counter(before_text.splitlines())
    rcon("checkModsNeedUpdate", quiet=True)
    if source == "manual":
        _set_phase(
            "Ожидание",
            "Ожидаем ответ RCON; отмена этой проверки не поддерживается.",
            countdown_seconds=timeout,
        )
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
    ws = _mods_registry()
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


def _do_check_mods_update():
    result = check_mods_update(source="manual")
    if result.get("state") == "inconclusive":
        raise OpsError(result["error"])
    message = (
        f"Моды требуют обновления: {len(result.get('items', []))}"
        if result.get("state") == "needs-update"
        else "Моды актуальны"
    )
    _set_phase("Готово", message)
    return result


def _do_apply_mods_update(warn_seconds):
    """Применение обновлений модов: рестарт — при старте Steam докачает свежие версии."""
    _do_restart(warn_seconds, reason="Обновление модов")
    _set_schedule(
        "nextModsCheck", time.time() + get_settings()["modsUpdate"]["intervalHours"] * 3600
    )


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
            _set_schedule("nextModsCheck", time.time() + (mu.get("intervalHours") or 6) * 3600)
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


def overview(*, state_provider=None):
    cfg = config.CFG
    docker_ok = docker_ok_cached()
    st = (state_provider or container_state)()
    image = _image_from_state(st)
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
        "update": {
            **update_state(),
            "local": local_digest_cached(image=image, image_id=(st or {}).get("imageId")),
        },
        "dashboardUpdate": dashboardupdate.state() if docker_ok else {"supported": False},
        "modsCheck": mods_check_state(),
        "settings": get_settings(),
        "watchdog": watchdog_state(),
        "notify": notifylib.state(),
        # фактический образ контейнера (в remote — из конфига); ключ один,
        # без дублей: в литерале ниже его уже не повторять
        "image": image,
        "backupsCount": count_backups(),
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
    except (ValueError, TypeError, OSError, OverflowError):
        return 0


def _ph_load():
    global _PH
    if _PH is None:
        try:
            with open(_ph_file(), encoding="utf-8") as f:
                data = json.load(f)
            _PH = (
                [
                    entry
                    for entry in data
                    if isinstance(entry, dict)
                    and isinstance(entry.get("ts"), str)
                    and _parse_ts(entry["ts"]) > 0
                    and type(entry.get("count")) in (int, float)
                    and 0 <= entry["count"] <= 256
                ]
                if isinstance(data, list)
                else []
            )
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

_WD_LOCK = threading.RLock()
_WD_GRACE = {"until": 0.0, "generation": 0}

_WD = {
    "lastProbeAt": None,
    "lastResult": None,
    "lastError": None,
    "consecutiveFailures": 0,
    "alerted": False,
    "lastRestartAt": None,
    "graceUntil": None,
}


def _begin_watchdog_grace():
    """Reset failures at a controlled stop/start; use a monotonic deadline."""
    seconds = get_settings()["watchdog"]["gracePeriodMin"] * 60
    with _WD_LOCK:
        _WD_GRACE["until"] = time.monotonic() + seconds
        _WD_GRACE["generation"] += 1
        _WD.update(
            lastResult="grace" if seconds else "skipped",
            lastError=None,
            consecutiveFailures=0,
            alerted=False,
            graceUntil=time.time() + seconds if seconds else None,
        )


def _watchdog_in_grace():
    with _WD_LOCK:
        return time.monotonic() < _WD_GRACE["until"]


def watchdog_state():
    with _WD_LOCK:
        remaining = max(0, _WD_GRACE["until"] - time.monotonic())
        return {
            **_WD,
            "graceUntil": _WD["graceUntil"] if remaining else None,
            "graceRemainingSec": math.ceil(remaining),
        }


def _watchdog_skip(result="skipped"):
    _WD.update(lastResult=result, lastError=None, consecutiveFailures=0, alerted=False)


def _watchdog_probe(wd):
    """Одна проба RCON: решает skip/fail/alert/авторестарт, мутирует _WD."""
    with _WD_LOCK:
        _WD["lastProbeAt"] = now_iso()
        generation = _WD_GRACE["generation"]
    st = container_state() if docker_ok_cached(ttl=120) else None
    with _WD_LOCK:
        if _watchdog_in_grace():
            _watchdog_skip("grace")
            return
        if op_busy() or not st or not st["running"]:
            # сервер остановлен или идёт операция — это не зависание
            _watchdog_skip()
            return
    try:
        rconlib.run_command(
            config.CFG["rcon_host"], config.CFG["rcon_port"], config.CFG["rcon_password"], "players"
        )
    except rconlib.RCONError as e:
        error = str(e)
    else:
        error = None
    with _WD_LOCK:
        # A probe started before a controlled restart must not publish a stale failure.
        if _watchdog_in_grace():
            _watchdog_skip("grace")
            return
        if generation != _WD_GRACE["generation"] or op_busy():
            _watchdog_skip()
            return
        if error is None:
            _WD.update(lastResult="ok", lastError=None, consecutiveFailures=0, alerted=False)
            return
        _WD["lastResult"] = "fail"
        _WD["lastError"] = error
        _WD["consecutiveFailures"] += 1
        silent_min = _WD["consecutiveFailures"] * 30 / 60
        if silent_min >= wd["thresholdMin"] and not _WD["alerted"]:
            _WD["alerted"] = True
            log_event("warn", f"Watchdog: RCON не отвечает {silent_min:.0f} мин — {error}")
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
