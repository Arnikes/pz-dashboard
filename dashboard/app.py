#!/usr/bin/env python3
"""HTTP-сервер пульта PZ: авторизация, статика и JSON API."""

import json
from datetime import datetime
import mimetypes
import os
import posixpath
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import actions
import auth
import config
import configeditor
from configformats import FormatError
import dockerlib
import notify
import ops
import payloads
import pwa
import rcon

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_STREAM_CACHE_LOCK = threading.Lock()


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
    body = handler.rfile.read(length)
    handler.body_read = True
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Некорректный JSON") from None
    if not isinstance(data, dict):
        raise ValueError("JSON должен быть объектом")
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "PZDashboard/1.0"
    protocol_version = "HTTP/1.1"

    # ── helpers ──
    def _send_json(self, obj, code=200, *, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)
        if self.close_connection:
            self._discard_request_body()

    def _discard_request_body(self):
        # Closing a socket with an unread POST body can reset the connection before
        # a browser sees the 401/403 response. Drain bounded bodies after replying.
        self.wfile.flush()
        if getattr(self, "body_read", False):
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if 0 < length <= 2_500_000:
                self.connection.settimeout(1)
                self.rfile.read(length)
        except (ValueError, OSError):
            pass

    def _send_error_json(self, code, message):
        self._send_json({"ok": False, "error": message}, code)

    def _redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _require_auth(self):
        manager = getattr(self.server, "auth", None)
        self.auth_session = manager.session(self.headers.get("Cookie")) if manager else None
        if self.auth_session:
            return True
        self.close_connection = True  # Do not interpret unread request bodies as requests.
        if not manager:
            self._send_error_json(503, "Авторизация не настроена")
        elif urllib.parse.urlparse(self.path).path in ("/", "/index.html"):
            self._redirect("/login")
        else:
            self._send_error_json(401, "Требуется вход администратора")
        return False

    def _require_local_request(self):
        # A custom header cannot be sent by cross-origin forms or fetch without a
        # CORS preflight. This server deliberately does not grant CORS permissions.
        origin = self.headers.get("Origin")
        parsed = urllib.parse.urlparse(origin or "")
        if (
            self.headers.get("X-PZ-Request") != "1"
            or self.headers.get("Sec-Fetch-Site") == "cross-site"
            or (
                origin
                and (
                    parsed.scheme not in ("http", "https")
                    or parsed.netloc != self.headers.get("Host")
                )
            )
        ):
            self.close_connection = True
            self._send_error_json(403, "Запрос должен быть отправлен из пульта")
            return False
        return True

    def _static_file(self, rel_path):
        safe = posixpath.normpath(urllib.parse.unquote(rel_path)).lstrip("/\\")
        if safe.rstrip(". ").casefold() == "index.html":
            if not self._require_auth():
                return
        full = os.path.realpath(os.path.join(STATIC_DIR, safe))
        if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep):
            self._send_error_json(403, "Запрещено")
            return
        if not os.path.isfile(full):
            self._send_error_json(404, "Файл не найден")
            return
        ctype = (
            "application/manifest+json"
            if safe == "manifest.webmanifest"
            else mimetypes.guess_type(full)[0] or "application/octet-stream"
        )
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Cache-Control", "no-store" if safe in ("index.html", "login.html") else "no-cache"
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_full_logs(self):
        if not ops.docker_ok_cached():
            self._send_error_json(400, "Логи доступны только при запуске пульта на хосте сервера")
            return
        # Docker writes directly to disk; RAM usage is independent of log size.
        with tempfile.TemporaryFile() as logfile:
            if not ops.full_logs(logfile):
                self._send_error_json(500, "Не удалось получить логи контейнера")
                return
            size = logfile.tell()
            logfile.seek(0)
            name = time.strftime("pzserver-%Y%m%d-%H%M%S.log")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            shutil.copyfileobj(logfile, self.wfile, length=256 * 1024)

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
            self.body_read = False
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    # ── SSE-поток: живые данные одним соединением вместо серии опросов ──

    def _sse_write(self, chunk: bytes):
        # HTTP/1.1 без Content-Length требует chunked-кодирование
        self.wfile.write(("%x\r\n" % len(chunk)).encode("ascii") + chunk + b"\r\n")
        self.wfile.flush()

    def _serve_stream(self):
        with _STREAM_CACHE_LOCK:
            if not hasattr(self.server, "stream_cache"):
                self.server.stream_cache = payloads.StreamCache()
        cache = self.server.stream_cache
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # кадры кодируем чанками вручную (длина заранее неизвестна) —
        # без этого заголовка браузеры декодируют поток «по привычке»,
        # а строгие клиенты видят сырые hex-строки между кадрами
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.connection.settimeout(75)
        last = {name: float("-inf") for name, _ in payloads.STREAM_PLAN}
        last_beat = time.monotonic()
        try:
            self._sse_write(b"retry: 3000\n\n")
            while True:
                if not self.server.auth.is_active(self.auth_session):
                    self._sse_write(b"event: auth-expired\ndata: {}\n\n")
                    break
                now = time.monotonic()
                for name, interval in payloads.STREAM_PLAN:
                    if now - last[name] < interval:
                        continue
                    frame = cache.frame(name, interval)
                    if not self.server.auth.is_active(self.auth_session):
                        self._sse_write(b"event: auth-expired\ndata: {}\n\n")
                        return
                    self._sse_write(frame)
                    last[name] = time.monotonic()
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

        public = path in (
            "/login",
            "/api/health",
            "/favicon.ico",
            "/manifest.webmanifest",
            "/sw.js",
        ) or path.startswith("/static/")
        # The HTML dashboard itself is protected, including its static alias.
        if path == "/static/index.html":
            public = False
        if not public and not self._require_auth():
            return

        if path in ("/", "/index.html"):
            self._static_file("index.html")
        elif path == "/login":
            manager = getattr(self.server, "auth", None)
            if manager and manager.session(self.headers.get("Cookie")):
                self._redirect("/")
            else:
                self._static_file("login.html")
        elif path == "/api/auth/session":
            self._send_json({"ok": True, "login": self.server.auth.login})
        elif path == "/manifest.webmanifest":
            self._static_file("manifest.webmanifest")
        elif path == "/sw.js":
            # Generated per request: any shipped asset/HTML change updates the worker.
            body = pwa.service_worker(STATIC_DIR)
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Service-Worker-Allowed", "/")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
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
        elif path == "/api/backups/journal":
            try:
                limit = min(100, max(1, int(qs.get("limit", [25])[0])))
                offset = max(0, int(qs.get("offset", [0])[0]))
            except ValueError:
                self._send_error_json(400, "Неверные параметры страницы журнала")
                return
            self._send_json(payloads.backup_journal_payload(limit, offset))
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
        if path != "/api/auth/login" and not self._require_auth():
            return
        if not self._require_local_request():
            return
        if path == "/api/auth/login" and not getattr(self.server, "auth", None):
            self.close_connection = True
            self._send_error_json(503, "Авторизация не настроена")
            return
        try:
            data = _read_json(self)
        except ValueError as error:
            # Unread bodies must not become the next request on a persistent connection.
            self.close_connection = True
            self._send_error_json(400, str(error))
            return

        if path == "/api/auth/login":
            remember = data.get("remember", False)
            if type(remember) is not bool:
                self._send_error_json(400, "remember должен быть true или false")
                return
            try:
                token = self.server.auth.sign_in(
                    data.get("login"),
                    data.get("password"),
                    self.client_address[0],
                    remember=remember,
                )
            except auth.RateLimited as error:
                self._send_error_json(429, str(error))
            except auth.SessionStorageError as error:
                self._send_error_json(503, str(error))
            except auth.AuthError as error:
                self._send_error_json(401, str(error))
            else:
                self._send_json(
                    {"ok": True}, cookie=self.server.auth.cookie(token, remember=remember)
                )
        elif path == "/api/auth/logout":
            try:
                self.server.auth.sign_out(self.auth_session)
            except auth.SessionStorageError as error:
                self._send_error_json(503, str(error))
                return
            self._send_json({"ok": True}, cookie=self.server.auth.cookie(clear=True))
        elif path == "/api/rcon":
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
            try:
                err = ops.patch_settings(data)
            except ops.OpsError as error:
                self._send_error_json(503, str(error))
                return
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
        if not self._require_auth() or not self._require_local_request():
            return
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
    try:
        manager = auth.Auth.from_env()
    except auth.AuthError as error:
        raise SystemExit(f"Ошибка авторизации: {error}") from None
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
    server.auth = manager
    server.daemon_threads = True
    print(
        f"PZ Dashboard: http://0.0.0.0:{cfg['port']}  "
        f"docker={'ok' if docker_ok else 'FAIL'} compose={'ok' if compose_ok else 'FAIL'}"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
