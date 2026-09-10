#!/usr/bin/env python3
"""HTTP-сервер пульта PZ: статика + JSON API. Только стандартная библиотека."""
import json
import mimetypes
import os
import posixpath
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import dockerlib
import notify
import ops
import rcon

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _read_json(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0 or length > 65536:
        return {}
    try:
        return json.loads(handler.rfile.read(length).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def players_payload():
    if not ops.docker_ok_cached():
        # удалённый режим: статус сервера узнаём самим RCON
        try:
            data = ops.fetch_players()
            return {"ok": True, **data}
        except rcon.RCONError as e:
            return {"ok": False, "error": str(e)}
    st = ops.container_state()
    if not st:
        return {"ok": False, "error": "Контейнер не найден"}
    if not st["running"]:
        return {"ok": False, "error": "Сервер остановлен"}
    try:
        data = ops.fetch_players()
        return {"ok": True, **data}
    except rcon.RCONError as e:
        return {"ok": False, "error": str(e)}


def logs_payload():
    text, err = dockerlib.container_logs(config.CFG["pz_container"], config.CFG["log_lines"])
    if text is None:
        return {"ok": False, "error": err or "логи недоступны"}
    return {"ok": True, "text": text}


def stats_payload():
    """Кадр метрик: ошибка контейнера отдаётся как ok:false + error, иначе
    интерфейс рисует фиктивные нули вместо состояния ошибки."""
    data = ops.fetch_stats()
    return {"ok": "error" not in data, **data}


def backups_payload():
    s = ops.get_settings()
    return {"ok": True, "items": ops.list_backups(),
            "maxBackups": s["backup"]["maxBackups"],
            "autoBackup": ops.auto_backup_state(),
            "journal": ops.get_backup_journal(30)}


def events_payload(limit=100):
    return {"ok": True, "items": ops.get_events(limit)}


# каналы SSE: имя события → интервал отправки, секунды
STREAM_PLAN = (
    ("ops", 1.0),
    ("overview", 3.0),
    ("players", 5.0),
    ("stats", 5.0),
    ("logs", 5.0),
    ("backups", 10.0),
    ("events", 12.0),
    ("stats-history", 30.0),
    ("players-history", 60.0),
    ("mods", 60.0),
)


def stream_payload(name):
    if name == "overview":
        return {"ok": True, **ops.overview()}
    if name == "players":
        return players_payload()
    if name == "stats":
        return stats_payload()
    if name == "logs":
        return logs_payload()
    if name == "backups":
        return backups_payload()
    if name == "events":
        return events_payload()
    if name == "ops":
        return {"ok": True, **ops.op_state()}
    if name == "stats-history":
        return {"ok": True, "points": ops.get_stats_history()}
    if name == "players-history":
        return {"ok": True, "points": ops.get_players_history()}
    if name == "mods":
        return ops.list_mods(None)
    return {"ok": False, "error": "нет такого потока"}


class Handler(BaseHTTPRequestHandler):
    server_version = "PZDashboard/1.0"
    protocol_version = "HTTP/1.1"

    # ── helpers ──
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, code, message):
        self._send_json({"ok": False, "error": message}, code)

    def _static_file(self, rel_path):
        safe = posixpath.normpath(urllib.parse.unquote(rel_path)).lstrip("/\\")
        full = os.path.realpath(os.path.join(STATIC_DIR, safe))
        if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep):
            self._send_error_json(403, "Запрещено")
            return
        if not os.path.isfile(full):
            self._send_error_json(404, "Файл не найден")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_full_logs(self):
        if not ops.docker_ok_cached():
            self._send_error_json(400, "Логи доступны только при запуске пульта на хосте сервера")
            return
        text = ops.full_logs()
        if text is None:
            self._send_error_json(500, "Не удалось получить логи контейнера")
            return
        body = text.encode("utf-8", "replace")
        name = time.strftime("pzserver-%Y%m%d-%H%M%S.log")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_backup(self, name):
        try:
            path = ops.backup_download_path(name)
        except ops.OpsError as e:
            self._send_error_json(400, str(e))
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            # архив исчез между проверкой и чтением (например, удалил prune)
            self._send_error_json(404, "Файл бэкапа не найден")
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(path)}"')
            self.end_headers()
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 256)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except OSError:
            # заголовки уже ушли (файл пропал mid-stream или клиент отвалился) —
            # остаётся честно закрыть соединение
            self.close_connection = True

    def log_message(self, fmt, *args):  # тише в логах
        sys.stderr.write("[http] %s\n" % (fmt % args))

    def handle_one_request(self):
        # браузер может резко сбросить keep-alive при закрытии вкладки —
        # это не ошибка сервера, тихо закрываем вместо трейсбека в лог
        try:
            super().handle_one_request()
        except ConnectionResetError:
            self.close_connection = True

    # ── SSE-поток: живые данные одним соединением вместо серии опросов ──

    def _sse_write(self, chunk: bytes):
        # HTTP/1.1 без Content-Length требует chunked-кодирование
        self.wfile.write(("%x\r\n" % len(chunk)).encode("ascii") + chunk + b"\r\n")
        self.wfile.flush()

    def _serve_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # кадры кодируем чанками вручную (длина заранее неизвестна) —
        # без этого заголовка браузеры декодируют поток «по привычке»,
        # а строгие клиенты видят сырые hex-строки между кадрами
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.connection.settimeout(75)
        last = {name: 0.0 for name, _ in STREAM_PLAN}
        last_beat = time.time()
        try:
            self._sse_write(b"retry: 3000\n\n")
            while True:
                now = time.time()
                for name, interval in STREAM_PLAN:
                    if now - last[name] < interval:
                        continue
                    try:
                        data = stream_payload(name)
                    except Exception as e:  # noqa: BLE001
                        data = {"ok": False, "error": str(e)}
                    frame = (f"event: {name}\ndata: "
                             + json.dumps(data, ensure_ascii=False) + "\n\n").encode("utf-8")
                    self._sse_write(frame)
                    last[name] = now
                if now - last_beat >= 15.0:
                    self._sse_write(b": heartbeat\n\n")
                    last_beat = now
                time.sleep(0.4)
        except OSError:
            pass  # клиент отключился
        finally:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass

    # ── GET ──
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._static_file("index.html")
        elif path.startswith("/static/"):
            self._static_file(path[len("/static/"):])
        elif path == "/favicon.ico":
            self._static_file("favicon.svg")
        elif path == "/api/health":
            self._send_json({"ok": True, "now": ops.now_iso()})
        elif path == "/api/overview":
            self._send_json({"ok": True, **ops.overview()})
        elif path == "/api/players":
            self._send_json(players_payload())
        elif path == "/api/players/history":
            self._send_json({"ok": True, "points": ops.get_players_history()})
        elif path == "/api/stats/history":
            self._send_json({"ok": True, "points": ops.get_stats_history()})
        elif path == "/api/logs/full":
            self._send_full_logs()
        elif path == "/api/mods":
            self._send_json(ops.mods_config_state((qs.get("file", [None])[0])))
        elif path == "/api/stats":
            self._send_json(stats_payload())
        elif path == "/api/logs":
            self._send_json(logs_payload())
        elif path == "/api/backups":
            self._send_json(backups_payload())
        elif path == "/api/telegram-chats":
            chats, err = notify.fetch_recent_chats()
            if err:
                self._send_json({"ok": False, "error": err})
            else:
                self._send_json({"ok": True, "chats": chats or []})
        elif path == "/api/events":
            try:
                limit = min(200, max(1, int(qs.get("limit", [100])[0])))
            except ValueError:
                limit = 100
            self._send_json(events_payload(limit))
        elif path == "/api/stream":
            self._serve_stream()
        elif path == "/api/ops":
            self._send_json({"ok": True, **ops.op_state()})
        elif path == "/api/backup/download":
            self._serve_backup((qs.get("name", [""])[0]))
        else:
            self._send_error_json(404, "Нет такого маршрута")

    # ── POST ──
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        data = _read_json(self)

        if path == "/api/rcon":
            command = (data.get("command") or "").strip()
            if not command or len(command) > 500:
                self._send_error_json(400, "Пустая или слишком длинная команда")
                return
            if ops.docker_ok_cached():
                st = ops.container_state()
                if not st or not st["running"]:
                    self._send_error_json(409, "Сервер остановлен — RCON недоступен")
                    return
            # в удалённом режиме живость сервера покажет сам RCON-запрос
            try:
                out = ops.rcon(command)
                ops.log_event("console", command[:200])
                self._send_json({"ok": True, "output": out or "(без ответа)"})
            except rcon.RCONError as e:
                self._send_json({"ok": False, "error": str(e)})
        elif path == "/api/settings":
            err = ops.patch_settings(data)
            if err:
                self._send_error_json(400, err)
            else:
                self._send_json({"ok": True, "settings": ops.get_settings()})
        elif path == "/api/notify-test":
            ok, err = notify.test_message()
            if ok:
                self._send_json({"ok": True})
            else:
                self._send_json({"ok": False, "error": err})
        elif path == "/api/mods-config":
            try:
                result = ops.set_mod_enabled(
                    data.get("file"), str(data.get("workshopId") or ""),
                    bool(data.get("enable")))
                self._send_json({"ok": True, **result})
            except ops.OpsError as e:
                self._send_json({"ok": False, "error": str(e)})
        elif path == "/api/action":
            self._handle_action(data)
        else:
            self._send_error_json(404, "Нет такого маршрута")

    # ── DELETE ──
    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/backup":
            try:
                ops.delete_backup(qs.get("name", [""])[0])
                self._send_json({"ok": True})
            except ops.OpsError as e:
                self._send_error_json(400, str(e))
        else:
            self._send_error_json(404, "Нет такого маршрута")

    # ── операции ──
    def _handle_action(self, data):
        action = data.get("op")
        settings = ops.get_settings()
        warn_default = settings["autoUpdate"]["warnSeconds"]
        try:
            warn = max(0, min(3600, int(data.get("warnSeconds", warn_default))))
        except (TypeError, ValueError):
            warn = warn_default

        known = {"start", "stop", "restart", "check-update", "apply-update",
                 "check-mods-update", "apply-mods-update", "backup", "restore",
                 "verify-backup"}
        if action not in known:
            self._send_error_json(400, "Неизвестная операция")
            return
        if ops.op_busy():
            self._send_error_json(409, "Уже выполняется другая операция")
            return

        try:
            if action == "start":
                ops.start_op("start", ops._do_start)
            elif action == "stop":
                ops.start_op("stop", lambda: ops._do_stop(warn))
            elif action == "restart":
                ops.start_op("restart", lambda: ops._do_restart(warn))
            elif action == "check-update":
                self._send_json({"ok": True, "check": ops.check_update(force_event=True)})
                return
            elif action == "apply-update":
                # после ручного обновления откладываем автопроверку на интервал:
                # get_settings() возвращает копию, мутация копии не сохраняется
                ops.defer_next_check(settings["autoUpdate"]["intervalHours"])
                ops.start_op("apply-update",
                             lambda: ops._do_apply_update(warn, "Обновление сервера"))
            elif action == "check-mods-update":
                ops.start_op("check-mods-update",
                             lambda: ops.check_mods_update(source="manual"))
            elif action == "apply-mods-update":
                ops.start_op("apply-mods-update",
                             lambda: ops._do_apply_mods_update(warn))
            elif action == "backup":
                stop_flag = bool(data.get("stopServer", False))
                ops.start_op("backup", lambda: ops.run_backup_job("manual", stop_flag))
            elif action == "verify-backup":
                vname = data.get("name") or ""
                ops.start_op("verify-backup", lambda: ops.verify_backup(vname))
            elif action == "restore":
                name = data.get("name") or ""
                ops.start_op("restore", lambda: ops._do_restore(name))
        except ops.OpsError as e:
            self._send_error_json(409, str(e))
            return
        self._send_json({"ok": True, "started": action})


def main():
    cfg = config.CFG
    os.makedirs(cfg["backup_dir"], exist_ok=True)
    os.makedirs(cfg["dashboard_dir"], exist_ok=True)
    ops._load_settings()
    ops.log_event("docker", "Пульт запущен")

    docker_ok = dockerlib.docker_version()
    compose_ok = dockerlib.compose_version() if docker_ok else False
    if not docker_ok:
        ops.log_event("docker", "Docker недоступен — пульт в удалённом режиме: активны RCON-консоль, "
                                "игроки и сохранение мира; контейнер, бэкапы и обновление — при запуске на хосте сервера")
    elif not compose_ok:
        ops.log_event("docker", "Плагин docker compose не найден — автообновление не сработает")

    ops.start_scheduler()
    ops.start_watchdog()
    notify.start_worker()

    # первая проверка обновлений — сама, без кнопки: карточка заполняется на загрузке
    def _startup_check():
        time.sleep(2)
        try:
            ops.check_update(force_event=True)
        except Exception as e:  # noqa: BLE001
            ops.log_event("error", "Стартовая проверка обновлений: " + str(e))

    threading.Thread(target=_startup_check, daemon=True, name="pz-startup-check").start()

    # первичная проба RCON, чтобы статус сразу показал живость сервера
    try:
        ops.rcon("players", quiet=True)
    except rcon.RCONError:
        pass

    server = ThreadingHTTPServer(("0.0.0.0", cfg["port"]), Handler)
    server.daemon_threads = True
    print(f"PZ Dashboard: http://0.0.0.0:{cfg['port']}  "
          f"docker={'ok' if docker_ok else 'FAIL'} compose={'ok' if compose_ok else 'FAIL'}")
    server.serve_forever()


if __name__ == "__main__":
    main()
