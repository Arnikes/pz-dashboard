#!/usr/bin/env python3
"""HTTP-сервер пульта PZ: статика + JSON API. Только стандартная библиотека."""

import json
from datetime import datetime
import mimetypes
import os
import posixpath
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import actions
import config
import configeditor
from configformats import FormatError
import dockerlib
import notify
import ops
import payloads
import rcon

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _read_json(handler):
    if handler.headers.get("Transfer-Encoding"):
        raise ValueError("Transfer-Encoding не поддерживается")
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        raise ValueError("Некорректный Content-Length") from None
    limit = (
        2_500_000
        if urllib.parse.urlparse(getattr(handler, "path", "")).path
        in ("/api/config-draft", "/api/modpack")
        else 65536
    )
    if length < 0 or length > limit:
        raise ValueError(f"Размер JSON должен быть не больше {limit} байт")
    if length == 0:
        return {}
    try:
        data = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Некорректный JSON") from None
    if not isinstance(data, dict):
        raise ValueError("JSON должен быть объектом")
    return data


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
            self.send_header(
                "Content-Disposition", f'attachment; filename="{os.path.basename(path)}"'
            )
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
        except (ConnectionResetError, ConnectionAbortedError):
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
        last = {name: 0.0 for name, _ in payloads.STREAM_PLAN}
        last_beat = time.time()
        try:
            self._sse_write(b"retry: 3000\n\n")
            while True:
                now = time.time()
                for name, interval in payloads.STREAM_PLAN:
                    if now - last[name] < interval:
                        continue
                    try:
                        data = payloads.stream_payload(name)
                    except Exception as e:  # noqa: BLE001
                        data = {"ok": False, "error": str(e)}
                    frame = (
                        f"event: {name}\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"
                    ).encode("utf-8")
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
            self._static_file(path[len("/static/") :])
        elif path == "/favicon.ico":
            self._static_file("favicon.svg")
        elif path == "/api/health":
            self._send_json({"ok": True, "now": ops.now_iso()})
        elif path in payloads.GET_CHANNELS:
            if path == "/api/logs":
                try:
                    tail = min(10000, max(1, int(qs.get("tail", [config.CFG["log_lines"]])[0])))
                except ValueError:
                    tail = config.CFG["log_lines"]
                scope = {}
                try:
                    dates = {}
                    for key in ("since", "until"):
                        if key in qs:
                            value = qs[key][0]
                            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
                            if date.tzinfo is None:
                                raise ValueError("timezone required")
                            dates[key] = date
                            scope[key] = value
                    if "since" in dates and "until" in dates and dates["until"] < dates["since"]:
                        raise ValueError("invalid range")
                except ValueError:
                    self._send_error_json(
                        400,
                        "Период логов: укажите ISO-время с часовым поясом и конец не раньше начала",
                    )
                    return
                self._send_json(payloads.logs_payload(tail, **scope))
            else:
                self._send_json(payloads.stream_payload(payloads.GET_CHANNELS[path]))
        elif path == "/api/logs/full":
            self._send_full_logs()
        elif path == "/api/mods":
            self._editor_request(
                lambda: configeditor.mod_response(
                    qs.get("file", [None])[0],
                    draft_mode=qs.get("draft", [""])[0] == "1",
                    refresh=qs.get("refresh", [""])[0] == "1",
                )
            )
        elif path == "/api/server-configs":
            self._editor_request(configeditor.profiles)
        elif path == "/api/config-draft":
            self._editor_request(lambda: configeditor.draft(qs.get("file", [None])[0]))
        elif path == "/api/config-history":
            self._editor_request(lambda: configeditor.history(qs.get("file", [None])[0]))
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
            self._send_json(payloads.events_payload(limit))
        elif path == "/api/stream":
            self._serve_stream()
        elif path == "/api/backup/download":
            self._serve_backup((qs.get("name", [""])[0]))
        else:
            self._send_error_json(404, "Нет такого маршрута")

    # ── POST ──
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            data = _read_json(self)
        except ValueError as error:
            # Unread bodies must not become the next request on a persistent connection.
            self.close_connection = True
            self._send_error_json(400, str(error))
            return

        if path == "/api/rcon":
            command = data.get("command", "")
            if not isinstance(command, str):
                self._send_error_json(400, "command должен быть строкой")
                return
            command = command.strip()
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
            self._editor_request(lambda: configeditor.legacy_toggle(data))
        elif path == "/api/config-draft":
            self._editor_request(lambda: configeditor.patch(data))
        elif path == "/api/config-validate":
            self._editor_request(
                lambda: configeditor.validate(
                    data.get("file"),
                    prepare=data.get("prepare", False),
                    draft_revision=data.get("draftRevision"),
                )
            )
        elif path == "/api/config-verify":
            self._editor_request(lambda: configeditor.verify_running(data))
        elif path == "/api/config-history":
            self._editor_request(lambda: configeditor.restore_history(data))
        elif path == "/api/workshop-resolve":
            self._editor_request(
                lambda: {
                    "ok": True,
                    **configeditor.workshop.resolve(data.get("input"), with_source=True),
                }
            )
        elif path == "/api/modpack":
            self._editor_request(lambda: configeditor.modpack(data))
        elif path == "/api/action":
            if data.get("op") in ("prepare-workshop", "apply-config"):
                self._editor_request(
                    lambda: configeditor.queue(data, prepare=data["op"] == "prepare-workshop")
                )
            else:
                self._handle_action(data)
        else:
            self._send_error_json(404, "Нет такого маршрута")

    def _editor_request(self, callback):
        try:
            self._send_json(callback())
        except configeditor.EditorError as error:
            self._send_error_json(error.status, str(error))
        except (FormatError, ValueError, TypeError) as error:
            self._send_error_json(400, str(error))
        except ops.OpsError as error:
            self._send_error_json(409, str(error))
        except OSError:
            self._send_error_json(
                503, "Файлы или Steam недоступны; проверьте подключение и права доступа"
            )

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
        try:
            result = actions.dispatch(data)
        except actions.ActionError as error:
            self._send_error_json(error.status, str(error))
            return
        self._send_json(result)


def main():
    cfg = config.CFG
    os.makedirs(cfg["backup_dir"], exist_ok=True)
    os.makedirs(cfg["dashboard_dir"], exist_ok=True)
    ops._load_settings()
    ops.log_event("docker", "Пульт запущен")

    docker_ok = dockerlib.docker_version()
    compose_ok = dockerlib.compose_version() if docker_ok else False
    if not docker_ok:
        ops.log_event(
            "docker",
            "Docker недоступен — пульт в удалённом режиме: активны RCON-консоль, "
            "игроки и сохранение мира; контейнер, бэкапы и обновление — при запуске на хосте сервера",
        )
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
    print(
        f"PZ Dashboard: http://0.0.0.0:{cfg['port']}  "
        f"docker={'ok' if docker_ok else 'FAIL'} compose={'ok' if compose_ok else 'FAIL'}"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
