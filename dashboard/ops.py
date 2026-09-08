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
                "rcon-error", "error", "docker", "backup-delete", "mods"}


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
    "modsUpdate": {"enabled": False, "intervalHours": 6, "restartOnUpdate": True},
    "backup": {"stopServer": False, "maxBackups": 10},
    "watchdog": {"enabled": False, "thresholdMin": 5, "autoRestart": False},
    "nextCheck": None,
    "nextModsCheck": None,
}


def _load_settings():
    try:
        with open(config.CFG["settings_file"], encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k in _SETTINGS and isinstance(v, dict):
                _SETTINGS[k].update(v)
            elif k in ("nextCheck", "nextModsCheck"):
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
        mu = patch.get("modsUpdate")
        if mu is not None:
            if not isinstance(mu, dict):
                return "неверный формат modsUpdate"
            if "enabled" in mu and not isinstance(mu["enabled"], bool):
                return "enabled должен быть true/false"
            if "intervalHours" in mu:
                mu["intervalHours"] = max(1, min(168, int(mu["intervalHours"])))
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
            log_event("warn", "Не удалось временно отключить restart policy — "
                              "docker может сам перезапустить контейнер при остановке")
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
    if graceful_stop(lambda m: _set_phase("Остановка", m)) == "resurrected":
        raise OpsError("Docker сам перезапустил контейнер — остановка не удалась, "
                       "проверьте restart policy и повторите")
    _set_phase("Готово", "Сервер остановлен")


def _do_restart(warn_seconds, reason="Перезапуск сервера"):
    if not is_running():
        raise OpsError("Сервер не запущен — сначала запустите его")
    if warn_seconds > 0:
        _set_phase("Предупреждение игроков", f"отсчёт {warn_seconds} с")
        rcon_warn_broadcast(warn_seconds, reason)
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


_LAST_CHECK = {"at": None, "image": None, "local": None, "remote": None,
               "hubUpdated": None, "available": None, "error": None}


def check_update(force_event=False):
    """Сравнить локальный и актуальный digest образа, который реально запущен."""
    image = _effective_image()
    repo, _, tag = image.rpartition(":")
    local = dockerlib.image_digests(image)
    result = {"at": now_iso(), "image": image, "local": local, "remote": None,
              "hubUpdated": None, "available": None, "error": None}
    try:
        url = f"https://hub.docker.com/v2/repositories/{repo}/tags/{tag}"
        req = urllib.request.Request(url, headers={"User-Agent": "pz-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        digest = data.get("digest") or ""
        result["remote"] = digest if digest.startswith("sha256:") else ("sha256:" + digest if digest else None)
        result["hubUpdated"] = data.get("last_updated")
        if local and result["remote"]:
            result["available"] = local != result["remote"]
        elif result["remote"] and not local:
            result["available"] = None
            if docker_ok_cached():
                result["note"] = "У образа нет repo-digest (собран или загружен без pull) — сравнение по digest невозможно"
            else:
                result["note"] = "Локальный digest недоступен (пульт вне хоста сервера) — сравнение версий невозможно"
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
        log_event("update", f"Сервер обновлён до {image}")
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
                                    log_event("auto", "Автообновление модов: рестарт для загрузки обновлений")
                                    _do_restart(au.get("warnSeconds", 300), reason="Обновление модов")
                                else:
                                    log_event("auto", "Автопроверка модов: найдены обновления (рестарт отключён)")
                        else:
                            log_event("warn", "Автопроверка модов: сервер не запущен — пропуск")
                    except OpsError as e:
                        log_event("error", "Автопроверка модов: " + str(e))
                    _SETTINGS["nextModsCheck"] = time.time() + mu["intervalHours"] * 3600
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
    record_stats_sample(stats)
    return stats


def full_logs():
    """Полный лог контейнера (для скачивания файлом)."""
    code, out, err = dockerlib.sh(["docker", "logs", config.CFG["pz_container"]], timeout=120)
    return out if code == 0 else None


# ─────────────────────────── моды сервера ───────────────────────────

import re as _re  # noqa: E402

_WS_TITLES = {}      # workshop id -> title
_WS_FAIL = {}        # workshop id -> ts последней неудачи (повтор через 10 мин)
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


def _workshop_map(items):
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
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "User-Agent": "pz-dashboard/1.0"},
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
        return {"ok": False,
                "error": "В /data/Server не найдено .ini файлов — проверьте монтирование каталога данных",
                "files": [], "file": None, "mods": [], "workshop": [], "pairs": [],
                "paired": False, "unbound": [], "mappingSource": None}
    if filename not in files:
        filename = files[0]
    parsed = parse_mods_ini(filename)
    if parsed is None:
        return {"ok": False, "error": "Файл конфигурации не найден",
                "files": files, "file": filename, "mods": [], "workshop": [],
                "pairs": [], "paired": False, "unbound": [], "mappingSource": None}
    mods, items = parsed["mods"], parsed["items"]
    _ws_titles(items)
    wmap = _workshop_map(items)

    workshop = []
    for wid in items:
        workshop.append({
            "workshopId": wid,
            "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}" if wid.isdigit() else "",
            "title": _WS_TITLES.get(wid, ""),
            "mods": wmap.get(wid, []),
        })

    bound = {m for lst in wmap.values() for m in lst}
    unbound = [m for m in mods if m not in bound]
    paired = len(mods) == len(items) and not wmap
    pairs = []
    if paired:
        for i, wid in enumerate(items):
            pairs.append({
                "mod": mods[i],
                "workshopId": wid,
                "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}" if wid.isdigit() else "",
                "title": _WS_TITLES.get(wid, ""),
            })
    return {"ok": True, "files": files, "file": filename,
            "workshop": workshop, "mods": mods, "unbound": unbound,
            "pairs": pairs, "paired": paired,
            "mappingSource": "disk" if wmap else ("order" if paired else None)}


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
            ws[wid] = {"title": w.get("title") or wid,
                       "url": w.get("url") or f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}"}
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
    result = {"at": now_iso(), "source": source,
              "state": state or "inconclusive",
              "items": _mods_items_from_lines(need, ws),
              "error": None}
    if not state:
        result["error"] = ("Сервер не вернул результат за отведённое время — "
                           "известная особенность B42, попробуйте позже")
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
        "update": {**update_state(), "local": local_digest_cached()},
        "modsCheck": mods_check_state(),
        "settings": get_settings(),
        "watchdog": watchdog_state(),
        "image": _effective_image(),
        "backupsCount": len(list_backups()),
        "now": now_iso(),
    }


# ─────────────────────────── история метрик (1 ч) ───────────────────────────

_STATS_LOCK = threading.Lock()
_STATS = []            # [{"ts": iso, "cpu": float, "mem": float}]
_STATS_INTERVAL = 60


def record_stats_sample(stats):
    """Семплирует CPU/RAM не чаще раза в минуту; отрисовывается последний час."""
    with _STATS_LOCK:
        now = time.time()
        if _STATS and now - _parse_ts(_STATS[-1]["ts"]) < _STATS_INTERVAL:
            return
        _STATS.append({"ts": now_iso(),
                       "cpu": round(float(stats.get("cpuPct") or 0), 2),
                       "mem": round(float(stats.get("memPct") or 0), 2)})
        del _STATS[:-120]


def get_stats_history():
    with _STATS_LOCK:
        cutoff = time.time() - 3600
        return [p for p in _STATS if _parse_ts(p["ts"]) >= cutoff]


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
