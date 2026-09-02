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
from collections import deque
from datetime import datetime, timezone

import config
import dockerlib
import rcon as rconlib

# ─────────────────────────── утилиты времени ───────────────────────────

def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _iso_ts(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
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

_EVENT_TYPES = {"start", "stop", "restart", "backup", "restore", "update",
                "update-check", "auto", "console", "warn", "delete",
                "rcon-error", "error", "docker", "backup-delete"}


def log_event(kind, text, detail=None):
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
        except OSError as e:
            pass  # события в памяти всё равно работают


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
_SETTINGS = {
    "autoUpdate": {"enabled": False, "intervalHours": 6, "warnSeconds": 300, "backupBeforeUpdate": True},
    "backup": {"stopServer": False, "maxBackups": 10},
    "watchdog": {"enabled": False, "thresholdMin": 5, "autoRestart": False},
    "nextCheck": None,
}


def _load_settings():
    try:
        with open(config.CFG["settings_file"], encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k in _SETTINGS and isinstance(v, dict):
                _SETTINGS[k].update(v)
            elif k in ("nextCheck",):
                _SETTINGS[k] = v
    except (OSError, ValueError):
        pass


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
        return json.loads(json.dumps(_SETTINGS))


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
                au["intervalHours"] = max(1, min(168, int(au["intervalHours"])))
            if "warnSeconds" in au:
                au["warnSeconds"] = max(0, min(3600, int(au["warnSeconds"])))
            if "backupBeforeUpdate" in au and not isinstance(au["backupBeforeUpdate"], bool):
                return "backupBeforeUpdate должен быть true/false"
            _SETTINGS["autoUpdate"].update(au)
        wd = patch.get("watchdog")
        if wd is not None:
            if not isinstance(wd, dict):
                return "неверный формат watchdog"
            if "enabled" in wd and not isinstance(wd["enabled"], bool):
                return "watchdog.enabled должен быть true/false"
            if "thresholdMin" in wd:
                wd["thresholdMin"] = max(1, min(60, int(wd["thresholdMin"])))
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
                bk["maxBackups"] = max(0, min(200, int(bk["maxBackups"])))
            _SETTINGS["backup"].update(bk)
        _save_settings()
        return None


# ─────────────────────────── RCON-хелперы ───────────────────────────

_RCON_CACHE = {"state": "unknown", "error": None, "at": None}


def rcon(command, quiet=False):
    """Выполнить RCON-команду. Бросает RCONError при недоступности."""
    cfg = config.CFG
    try:
        text = rconlib.run_command(cfg["rcon_host"], cfg["rcon_port"], cfg["rcon_password"], command)
        _RCON_CACHE.update({"state": "ok", "error": None, "at": now_iso()})
        return text
    except rconlib.RCONError as e:
        _RCON_CACHE.update({"state": "error", "error": str(e), "at": now_iso()})
        if not quiet:
            log_event("rcon-error", "RCON: " + str(e))
        raise


def rcon_warn_broadcast(seconds, reason):
    """Отправляет игрокам отсчёт перед остановкой. Возвращает False при сбое RCON."""
    if seconds <= 0:
        return True
    thresholds = {30, 10}
    thresholds.update(range(60, seconds + 1, 60))
    last_sent = None
    ok = True
    for left in range(seconds, 0, -10):
        if left in thresholds and left != last_sent:
            text = (f"{reason} через {left // 60} мин" if left >= 60
                    else f"{reason} через {left} сек")
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


def docker_ok_cached(ttl=60):
    """Доступность docker — с кэшем: опрос каждые 3 с не должен дёргать CLI."""
    now = time.time()
    if _DOCKER_CACHE["ok"] is None or now - _DOCKER_CACHE["at"] > ttl:
        _DOCKER_CACHE["ok"] = dockerlib.docker_version()
        _DOCKER_CACHE["at"] = now
    return _DOCKER_CACHE["ok"]


def container_state():
    return dockerlib.inspect_container(config.CFG["pz_container"])


def is_running():
    st = container_state()
    return bool(st and st["running"])


def wait_until_stopped(timeout=240):
    waited = 0
    while waited < timeout:
        st = container_state()
        if not st or not st["running"]:
            return True
        time.sleep(5)
        waited += 5
    return not is_running()


def wait_until_running(timeout=120):
    waited = 0
    while waited < timeout:
        if is_running():
            return True
        time.sleep(4)
        waited += 4
    return is_running()


def graceful_stop(phase_hook=None):
    """Правильная остановка PZ: RCON quit (сохранение мира), затем docker stop."""
    def hook(msg):
        if phase_hook:
            phase_hook(msg)
    if not is_running():
        return
    hook("Команда quit через RCON (сохранение мира)")
    try:
        rcon("quit", quiet=True)
    except rconlib.RCONError:
        pass
    hook("Ожидание остановки контейнера")
    if not wait_until_stopped(240):
        hook("Принудительная остановка контейнера")
        dockerlib.container_stop(config.CFG["pz_container"], seconds=180)
        wait_until_stopped(60)
    log_event("stop", "Сервер остановлен")


# ─────────────────────────── операции ───────────────────────────

class OpsError(Exception):
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
                _OP_HISTORY.appendleft({"op": op, "ok": True, "message": _ACTIVE["message"],
                                        "finishedAt": now_iso()})
        except OpsError as e:
            log_event("error", f"Операция «{op}» не удалась: {e}")
            with _OP_LOCK:
                _ACTIVE["message"] = str(e)
                _OP_HISTORY.appendleft({"op": op, "ok": False, "message": str(e),
                                        "finishedAt": now_iso()})
        except Exception as e:  # noqa: BLE001 — не роняем поток
            log_event("error", f"Операция «{op}»: {e}")
            with _OP_LOCK:
                _ACTIVE["message"] = f"Внутренняя ошибка: {e}"
                _OP_HISTORY.appendleft({"op": op, "ok": False, "message": str(e),
                                        "finishedAt": now_iso()})
        finally:
            with _OP_LOCK:
                _ACTIVE["op"] = None
                _ACTIVE["phase"] = ""
                _ACTIVE["startedAt"] = None

    with _OP_LOCK:
        if _ACTIVE["op"]:
            raise OpsError("Уже выполняется другая операция, подождите")
        _ACTIVE.update({"op": op, "phase": "Подготовка…", "message": "",
                        "startedAt": now_iso()})
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
    graceful_stop(lambda m: _set_phase("Остановка", m))
    _set_phase("Готово", "Сервер остановлен")


def _do_restart(warn_seconds):
    if not is_running():
        raise OpsError("Сервер не запущен — сначала запустите его")
    if warn_seconds > 0:
        _set_phase("Предупреждение игроков", f"отсчёт {warn_seconds} с")
        rcon_warn_broadcast(warn_seconds, "Перезапуск сервера")
    graceful_stop(lambda m: _set_phase("Остановка", m))
    _set_phase("Запуск", "docker start")
    code, out, err = dockerlib.container_start(config.CFG["pz_container"])
    if code != 0:
        raise OpsError(f"Не удалось запустить после рестарта: {err or out}")
    wait_until_running(150)
    log_event("restart", "Сервер перезапущен")
    _set_phase("Готово", "Сервер перезапущен")


# ─── обновления ───

def _fetch_remote_digest():
    repo = config.CFG["image_repo"]
    tag = config.CFG["image_tag"]
    url = f"https://hub.docker.com/v2/repositories/{repo}/tags/{tag}"
    req = urllib.request.Request(url, headers={"User-Agent": "pz-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    digest = data.get("digest") or ""
    return digest if digest.startswith("sha256:") else "sha256:" + digest if digest else None


_LAST_CHECK = {"at": None, "local": None, "remote": None, "available": None, "error": None}


def check_update(force_event=False):
    """Сравнить локальный и актуальный digest образа."""
    local = dockerlib.image_digests(config.CFG["pz_image"])
    result = {"at": now_iso(), "local": local, "remote": None, "available": None, "error": None}
    try:
        remote = _fetch_remote_digest()
        result["remote"] = remote
        if local and remote:
            result["available"] = local != remote
        elif remote and not local:
            result["available"] = None
            result["note"] = "Локальный digest недоступен (пульт вне хоста сервера) — сравнение версий невозможно"
        elif not remote:
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


def _do_apply_update(warn_seconds, reason="Обновление сервера"):
    _set_phase("Проверка актуального образа", "Docker Hub")
    try:
        before = dockerlib.image_digests(config.CFG["pz_image"])
        _set_phase("Скачивание нового образа", config.CFG["pz_image"])
        code, out, err = dockerlib.image_pull(config.CFG["pz_image"])
        if code != 0:
            raise OpsError(f"Не удалось скачать образ: {(err or out)[:200]}")
        after = dockerlib.image_digests(config.CFG["pz_image"])
        if before and after and before == after:
            check_update(force_event=False)
            log_event("update", "Образ уже актуален, обновление не требуется")
            _set_phase("Готово", "Образ уже актуален")
            return
        if not dockerlib.compose_version():
            raise OpsError("docker compose недоступен — без него нельзя заменить контейнер. "
                           "Проверьте установку плагина compose в образе пульта")
        if get_settings()["autoUpdate"].get("backupBeforeUpdate", True):
            _set_phase("Страховочный бэкап", "сохранение мира и архива")
            try:
                bk = _do_backup(False)
                log_event("backup", f"Бэкап перед обновлением: {bk['name']} ({fmt_size(bk['size'])})")
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
            raise OpsError(f"docker compose up не удался: {(err or out)[:200]}. "
                           "Сервер оставлен остановленным — проверьте конфиг")
        wait_until_running(180)
        if not is_running():
            raise OpsError("Контейнер пересоздан, но не поднялся — проверьте docker logs pzserver")
        check_update(force_event=False)
        log_event("update", f"Сервер обновлён до {config.CFG['pz_image']}")
        _set_phase("Готово", "Сервер обновлён и запущен")
    except OpsError:
        raise
    except Exception as e:  # noqa: BLE001
        raise OpsError(f"Обновление не удалось: {e}")


# ─── бэкапы ───

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
            items.append({"name": name, "size": st.st_size, "sizeText": fmt_size(st.st_size),
                          "mtime": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds")})
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


def _do_backup(stop_server):
    bdir, ddir = _backup_paths()
    os.makedirs(bdir, exist_ok=True)
    if not os.path.isdir(ddir) or not os.listdir(ddir):
        raise OpsError("Каталог данных PZ пуст или не смонтирован — бэкап невозможен")
    was_running = is_running()
    if stop_server:
        if not was_running:
            raise OpsError("Сервер не запущен — «бэкап с остановкой» не нужен, "
                           "снимите флажок")
        _set_phase("Предупреждение игроков", "отсчёт 60 с")
        rcon_warn_broadcast(60, "Бэкап сервера")
        graceful_stop(lambda m: _set_phase("Остановка для бэкапа", m))
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
    proc = subprocess.run(
        ["tar", "-czf", dest, "--exclude=Logs", "--exclude=logs", "--exclude=*.log",
         "-C", ddir, "."],
        capture_output=True, text=True, timeout=2400)
    if proc.returncode != 0:
        raise OpsError(f"tar не удался: {(proc.stderr or proc.stdout)[:200]}")
    if stop_server and was_running:
        _set_phase("Запуск сервера", "docker start")
        dockerlib.container_start(config.CFG["pz_container"])
        wait_until_running(150)
    size = os.path.getsize(dest)
    pruned = _prune_backups(get_settings()["backup"]["maxBackups"])
    log_event("backup", f"Бэкап создан: {name} ({fmt_size(size)})")
    if pruned:
        log_event("backup-delete", f"Удалено старых бэкапов: {pruned}")
    _set_phase("Готово", f"Бэкап {name} создан")
    return {"name": name, "size": size}


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
        graceful_stop(lambda m: _set_phase("Остановка сервера", m))
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
    proc = subprocess.run(["tar", "-xzf", path, "-C", config.CFG["data_dir"]],
                          capture_output=True, text=True, timeout=2400)
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


# ─────────────────────────── планировщик автообновления ───────────────────────────

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
        except Exception as e:  # noqa: BLE001
            log_event("error", "Планировщик: " + str(e))
        time.sleep(20)


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
    stats.pop("error", None)
    return stats


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
                cont["uptimeSec"] = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
            except ValueError:
                pass
    return {
        "serverName": cfg["server_name"],
        "mode": "local" if docker_ok else "remote",
        "container": cfg["pz_container"],
        "image": cfg["pz_image"],
        "docker": docker_ok,
        "compose": dockerlib.compose_version() if docker_ok else False,
        "rconConfigured": bool(cfg["rcon_password"]),
        "rcon": dict(_RCON_CACHE),
        "containerInfo": cont,
        "update": update_state(),
        "settings": get_settings(),
        "watchdog": watchdog_state(),
        "backupsCount": len(list_backups()),
        "now": now_iso(),
    }


# ─────────────────────────── история онлайна (24 ч) ───────────────────────────

_PH_LOCK = threading.Lock()
_PH = None                       # [{"ts": iso, "count": n}]
_PH_INTERVAL = 240               # семпл не чаще, чем раз в 4 минуты


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

_WD = {"lastProbeAt": None, "lastResult": None, "lastError": None,
       "consecutiveFailures": 0, "alerted": False}


def watchdog_state():
    return dict(_WD)


def _watchdog_loop():
    """Раз в 30 с пробует RCON. При молчании дольше порога — событие,
    а на хосте сервера — опциональный авторестарт (без предупреждения:
    предупреждать некому — RCON мёртв)."""
    while True:
        try:
            wd = get_settings()["watchdog"]
            if wd["enabled"]:
                _WD["lastProbeAt"] = now_iso()
                try:
                    rconlib.run_command(config.CFG["rcon_host"], config.CFG["rcon_port"],
                                        config.CFG["rcon_password"], "players")
                    _WD.update({"lastResult": "ok", "lastError": None,
                                "consecutiveFailures": 0, "alerted": False})
                except rconlib.RCONError as e:
                    _WD["lastResult"] = "fail"
                    _WD["lastError"] = str(e)
                    _WD["consecutiveFailures"] += 1
                    silent_min = _WD["consecutiveFailures"] * 30 / 60
                    if silent_min >= wd["thresholdMin"] and not _WD["alerted"]:
                        _WD["alerted"] = True
                        log_event("warn", f"Watchdog: RCON не отвечает {silent_min:.0f} мин — {e}")
                        if wd["autoRestart"] and docker_ok_cached() and is_running() and not op_busy():
                            log_event("warn", "Watchdog: авторестарт зависшего сервера")
                            try:
                                start_op("restart", lambda: _do_restart(0))
                            except OpsError:
                                pass
        except Exception as e:  # noqa: BLE001
            log_event("error", "Watchdog: " + str(e))
        time.sleep(30)


def start_watchdog():
    threading.Thread(target=_watchdog_loop, daemon=True, name="pz-watchdog").start()
