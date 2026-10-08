"""Telegram locale is persistent and independent of request/thread presentation."""

import ast
import json
import queue
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

import config
import i18n
import notify
import ops
import settingsmodel
from test_auth import auth_server as auth_server, request, sign_in


@pytest.mark.parametrize("language", ["ru", "en"])
def test_language_persists_without_replacing_credentials(tmp_path, monkeypatch, language):
    settings = deepcopy(settingsmodel.DEFAULTS)
    settings["telegram"].update(botToken="123456:secret-token", chatId="-10042")
    monkeypatch.setattr(ops, "_SETTINGS", settings)
    monkeypatch.setattr(ops, "_SETTINGS_VERSION", {"epoch": "test", "revision": 0})
    monkeypatch.setitem(config.CFG, "settings_file", str(tmp_path / "settings.json"))
    assert ops.patch_settings({"telegram": {"language": language}}) is None
    assert ops.get_settings()["telegram"]["language"] == language
    assert ops.get_settings()["telegram"]["botToken"] == ""
    monkeypatch.setattr(ops, "_SETTINGS", deepcopy(settingsmodel.DEFAULTS))
    ops._load_settings()
    assert ops.telegram_settings_raw()["language"] == language
    assert ops.telegram_settings_raw()["botToken"] == "123456:secret-token"
    assert ops.telegram_settings_raw()["chatId"] == "-10042"


@pytest.mark.parametrize("language,ui_language", [("en", "ru"), ("ru", "en")])
def test_settings_and_test_message_http_contract(
    auth_server, tmp_path, monkeypatch, language, ui_language
):
    monkeypatch.setattr(ops, "_SETTINGS", deepcopy(settingsmodel.DEFAULTS))
    monkeypatch.setattr(ops, "_SETTINGS_VERSION", {"epoch": "test", "revision": 0})
    monkeypatch.setattr(notify, "_LAST", {"at": None, "ok": None, "error": None})
    monkeypatch.setitem(config.CFG, "settings_file", str(tmp_path / "settings.json"))
    send = Mock(return_value=(True, None))
    monkeypatch.setattr(notify, "send_message", send)
    cookie = sign_in(auth_server)
    headers = {"X-PZ-Language": ui_language}
    status, _, body = request(
        auth_server,
        "POST",
        "/api/settings",
        {
            "telegram": {
                "language": language,
                "botToken": "123456:secret",
                "chatId": "42",
            }
        },
        cookie=cookie,
        headers=headers,
    )
    assert status == 200 and body["settings"]["telegram"]["language"] == language
    assert body["settings"]["telegram"]["botToken"] == ""
    status, _, body = request(
        auth_server, "POST", "/api/notify-test", {}, cookie=cookie, headers=headers
    )
    assert status == 200 and body == {"ok": True}
    expected = (
        "✅ PZ Console: connection test — notifications work."
        if language == "en"
        else "✅ PZ Пульт: проверка связи — уведомления работают."
    )
    send.assert_called_once_with(expected)
    status, _, body = request(
        auth_server,
        "POST",
        "/api/settings",
        {"telegram": {"language": "de"}},
        cookie=cookie,
        headers=headers,
    )
    assert status == 400
    assert body["error"] == (
        "telegram.language must be ru or en"
        if ui_language == "en"
        else "telegram.language должен быть ru или en"
    )
    assert ops.telegram_settings_raw()["language"] == language


@pytest.mark.parametrize("value", [None, "", "de", "EN", "en-US", True, 1, [], {}])
def test_invalid_language_rejects_entire_patch(value):
    current = deepcopy(settingsmodel.DEFAULTS)
    patch = {"autoUpdate": {"enabled": True}, "telegram": {"language": value}}
    original = deepcopy(patch)
    candidate, error = settingsmodel.prepare_patch(current, patch, Mock())
    assert candidate is None
    assert error == "telegram.language должен быть ru или en"
    assert current == settingsmodel.DEFAULTS and patch == original


@pytest.mark.parametrize("loaded", [{}, {"language": "de"}, {"language": []}, {"language": None}])
def test_legacy_and_invalid_language_default_to_english(loaded):
    current = deepcopy(settingsmodel.DEFAULTS)
    settingsmodel.merge_loaded(current, {"telegram": loaded})
    settingsmodel.normalize_loaded(current)
    assert current["telegram"]["language"] == "en"


EVENTS = [
    ("start", "Сервер запущен", "Server started"),
    ("stop", "Сервер остановлен", "Server stopped"),
    ("restart", "Сервер перезапущен (Перезапуск сервера)", "Server restarted (Restarting server)"),
    ("restart", "Сервер перезапущен (Обновление модов)", "Server restarted (Mod update)"),
    ("docker", "Пульт запущен", "Console started"),
    (
        "backup",
        "Бэкап создан (вручную): Мир.tar.gz (52.3 МБ)",
        "Backup created (manual): Мир.tar.gz (52.3 MB)",
    ),
    (
        "backup",
        "Бэкап создан (по расписанию): Мир.tar.gz (1.2 ГБ)",
        "Backup created (scheduled): Мир.tar.gz (1.2 GB)",
    ),
    (
        "backup",
        "Проверка бэкапа Мир.tar.gz: OK — файлов 2, 5.3 МБ, замечания: нет Server/*.ini, нет Maps/",
        "Backup check Мир.tar.gz: OK — 2 files, 5.3 MB, notes: no Server/*.ini, no Maps/",
    ),
    (
        "backup",
        "Проверка бэкапа Мир.tar.gz: OK — файлов 2, 5.3 МБ, замечания: "
        "нет Server/*.ini, нет данных мира (Maps/ или Saves/Multiplayer/)",
        "Backup check Мир.tar.gz: OK — 2 files, 5.3 MB, notes: "
        "no Server/*.ini, no world data (Maps/ or Saves/Multiplayer/)",
    ),
    ("restore", "Мир восстановлен из Мир.tar.gz", "World restored from Мир.tar.gz"),
    ("backup-delete", "Бэкап удалён: Мир.tar.gz", "Backup deleted: Мир.tar.gz"),
    ("backup-delete", "Удалено старых бэкапов: 3", "Old backups deleted: 3"),
    (
        "update",
        "Доступно обновление образа example/pz:latest",
        "Image update available: example/pz:latest",
    ),
    ("update", "Сервер обновлён до example/pz:latest", "Server updated to example/pz:latest"),
    (
        "update-check",
        "Проверка обновлений: docker CLI не найден в контейнере",
        "Update check: docker CLI not found in the container",
    ),
    (
        "auto",
        "Автобэкап по расписанию: запуск (03:00)",
        "Scheduled automatic backup: starting (03:00)",
    ),
    (
        "auto",
        "Автобэкап по расписанию (навёрстывание): запуск (03:00)",
        "Scheduled automatic backup (catch-up): starting (03:00)",
    ),
    (
        "auto",
        "Автообновление модов отменено администратором",
        "Automatic mod update cancelled by administrator",
    ),
    (
        "mods",
        "Конфигурация Сервер: Неверный логин или пароль",
        "Configuration Сервер: Invalid username or password",
    ),
    ("mods", "Моды требуют обновления: 2", "Mods require updates: 2"),
    (
        "error",
        "Бэкап (по расписанию) не удался: Сервер не запущен",
        "Backup (scheduled) failed: Server is not running",
    ),
    (
        "error",
        "Операция «backup» не удалась: Не удалось запустить: Сервер не запущен",
        "Operation “backup” failed: Could not start: Server is not running",
    ),
    (
        "warn",
        "Watchdog: RCON не отвечает 5 мин — RCON: таймаут соединения",
        "Watchdog: RCON has not responded for 5 min — RCON: connection timed out",
    ),
    (
        "rcon-error",
        "RCON: Отказ RCON: неверный пароль",
        "RCON: RCON rejected authentication: invalid password",
    ),
    ("error", "Планировщик: Сервер не запущен", "Scheduler: Server is not running"),
    (
        "warn",
        "Рескан модов после рестарта не удался: Сервер не запущен",
        "Mod rescan after restart failed: Server is not running",
    ),
]


@pytest.mark.parametrize("kind,source,expected", EVENTS)
def test_event_texts_and_nested_parameters(kind, source, expected, monkeypatch):
    monkeypatch.setitem(config.CFG, "server_name", "Сервер {{0}}")
    token = i18n.LANGUAGE.set("en")
    try:
        prefix = f"{notify.KIND_ICON[kind]} [Сервер {{{{0}}}}] "
        assert notify._format(kind, source, "ru") == prefix + source
        assert notify._format(kind, source, "en") == prefix + expected
        assert i18n.language() == "en"
    finally:
        i18n.LANGUAGE.reset(token)


def test_enqueue_localizes_only_notification_and_captures_saved_language(tmp_path, monkeypatch):
    tg = {"enabled": True, "language": "en", "botToken": "test", "chatId": "42"}
    monkeypatch.setattr(ops, "telegram_settings_raw", lambda: tg)
    monkeypatch.setattr(notify, "_QUEUE", queue.Queue(maxsize=50))
    monkeypatch.setitem(config.CFG, "server_name", "")
    monkeypatch.setitem(config.CFG, "events_file", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(ops, "_EV_MEM", deque(maxlen=200))
    ops.log_event("start", "Сервер запущен")
    tg["language"] = "ru"
    assert notify._QUEUE.get_nowait() == "▶️ Server started"
    assert ops.get_events()[0]["text"] == "Сервер запущен"
    event = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8"))
    assert event["text"] == "Сервер запущен"
    notify.enqueue("start", "Сервер запущен")
    assert notify._QUEUE.get_nowait() == "▶️ Сервер запущен"


@pytest.mark.parametrize("language,request_language", [("en", "ru"), ("ru", "en")])
def test_test_button_sends_saved_locale_through_telegram(monkeypatch, language, request_language):
    monkeypatch.setattr(
        ops,
        "telegram_settings_raw",
        lambda: {
            "language": language,
            "botToken": "123456:secret",
            "chatId": "42",
        },
    )
    monkeypatch.setattr(notify, "_LAST", {"at": None, "ok": None, "error": None})
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'{"ok":true}'
    urlopen = Mock(return_value=response)
    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    token = i18n.LANGUAGE.set(request_language)
    try:
        assert notify.test_message() == (True, None)
        assert i18n.language() == request_language
    finally:
        i18n.LANGUAGE.reset(token)
    body = json.loads(urlopen.call_args.args[0].data)
    expected = (
        "✅ PZ Console: connection test — notifications work."
        if language == "en"
        else "✅ PZ Пульт: проверка связи — уведомления работают."
    )
    assert body == {"chat_id": "42", "text": expected}


def test_explicit_translation_is_thread_safe():
    def render(language):
        token = i18n.LANGUAGE.set("ru" if language == "en" else "en")
        try:
            return i18n.translate("Сервер перезапущен (Обновление модов)", locale=language)
        finally:
            i18n.LANGUAGE.reset(token)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(render, ["en", "ru"] * 20))
    assert (
        results == ["Server restarted (Mod update)", "Сервер перезапущен (Обновление модов)"] * 20
    )


def test_all_authored_event_templates_have_complete_english_translations():
    def render(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(
                part.value if isinstance(part, ast.Constant) else "VALUE" for part in node.values
            )
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return render(node.left) + render(node.right)
        return "VALUE"  # Opaque runtime values are covered by the event cases above.

    for path in Path(ops.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            if name != "log_event":
                continue
            translated = i18n.translate(render(node.args[1]), locale="en")
            assert not any("\u0400" <= char <= "\u04ff" for char in translated), (
                path.name,
                node.lineno,
                translated,
            )
