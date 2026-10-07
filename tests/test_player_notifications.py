"""Player warnings use a persistent locale independent of UI and Telegram."""

from copy import deepcopy
from collections import deque
from unittest.mock import Mock

import pytest

import config
import i18n
import notify
import ops
import settingsmodel
from test_auth import auth_server as auth_server, request, sign_in


@pytest.fixture
def warnings(monkeypatch):
    settings = deepcopy(settingsmodel.DEFAULTS)
    sent = []
    monkeypatch.setattr(ops, "_SETTINGS", settings)
    monkeypatch.setattr(ops, "rcon", lambda command, **kwargs: sent.append(command))
    monkeypatch.setattr(ops.time, "sleep", lambda seconds: None)
    return settings, sent


@pytest.mark.parametrize("locale", ["en", "ru"])
@pytest.mark.parametrize(
    "reason,english",
    [
        ("Остановка сервера", "Stopping server"),
        ("Перезапуск сервера", "Restarting server"),
        ("Обновление сервера", "Server update"),
        ("Автообновление сервера", "Automatic server updates"),
        ("Обновление модов", "Mod update"),
        ("Бэкап сервера", "Server backup"),
        ("Восстановление из бэкапа", "Restore from backup"),
        ("Изменение конфигурации", "Configuration change"),
    ],
)
def test_all_warning_reasons_in_selected_locale(warnings, locale, reason, english):
    settings, sent = warnings
    settings["playerNotifications"]["language"] = locale
    settings["telegram"]["language"] = "ru" if locale == "en" else "en"
    token = i18n.LANGUAGE.set(settings["telegram"]["language"])
    try:
        assert ops.rcon_warn_broadcast(60, reason)
        expected = f"{english} in 1 minute" if locale == "en" else f"{reason} через 1 минуту"
        assert sent[0] == f'servermsg "{expected}"'
        assert len(sent) == 3
    finally:
        i18n.LANGUAGE.reset(token)


@pytest.mark.parametrize(
    "minutes,russian",
    [(1, "минуту"), (2, "минуты"), (5, "минут"), (11, "минут"), (21, "минуту"), (22, "минуты")],
)
@pytest.mark.parametrize("locale", ["en", "ru"])
def test_countdown_plural_forms(warnings, minutes, russian, locale):
    settings, sent = warnings
    settings["playerNotifications"]["language"] = locale
    assert ops.rcon_warn_broadcast(minutes * 60, "Обновление модов")
    duration = (
        f"in {minutes} minute" + ("s" if minutes != 1 else "")
        if locale == "en"
        else f"через {minutes} {russian}"
    )
    assert duration in sent[0]
    assert ("in 30 seconds" if locale == "en" else "через 30 секунд") in sent[-2]
    assert ("in 10 seconds" if locale == "en" else "через 10 секунд") in sent[-1]


@pytest.mark.parametrize("locale,expected", [("en", "in 1 second"), ("ru", "через 1 секунду")])
def test_short_countdown_and_zero_warning(warnings, locale, expected):
    settings, sent = warnings
    settings["playerNotifications"]["language"] = locale
    assert ops.rcon_warn_broadcast(0, "Обновление модов") and not sent
    assert ops.rcon_warn_broadcast(1, "Обновление модов")
    assert expected in sent[0]


@pytest.mark.parametrize("locale", ["en", "ru"])
def test_operation_freezes_countdown_and_cancellation_language(warnings, monkeypatch, locale):
    settings, sent = warnings
    settings["playerNotifications"]["language"] = locale
    monkeypatch.setattr(ops, "_ACTIVE", dict(ops._ACTIVE, op=None))
    monkeypatch.setattr(ops, "_OP_HISTORY", deque())
    monkeypatch.setattr(ops, "is_running", lambda: True)
    monkeypatch.setattr(ops, "log_event", Mock())
    stop = Mock()
    monkeypatch.setattr(ops, "graceful_stop", stop)

    def wait(seconds):
        settings["playerNotifications"]["language"] = "ru" if locale == "en" else "en"
        ops._OP_CANCEL.set()
        return True

    monkeypatch.setattr(ops._OP_CANCEL, "wait", wait)
    # Run the worker synchronously to inspect its context without timing races.
    monkeypatch.setattr(ops.threading, "Thread", lambda target, **kwargs: Mock(start=target))
    ops.start_op("mods-restart", lambda: ops._do_restart(60, "Обновление модов", cancellable=True))
    expected = (
        "Mod update cancelled. The server continues running."
        if locale == "en"
        else "Обновление модов отменено. Сервер продолжает работу."
    )
    assert sent[-1] == f'servermsg "{expected}"'
    assert ("in 1 minute" if locale == "en" else "через 1 минуту") in sent[0]
    stop.assert_not_called()
    assert ops._OP_HISTORY[0]["cancelled"] is True
    assert ops._PLAYER_LANGUAGE.get() is None


@pytest.mark.parametrize("locale", ["en", "ru"])
def test_preference_http_and_disk_round_trip(auth_server, tmp_path, monkeypatch, locale):
    monkeypatch.setattr(ops, "_SETTINGS", deepcopy(settingsmodel.DEFAULTS))
    monkeypatch.setitem(config.CFG, "settings_file", str(tmp_path / "settings.json"))
    cookie = sign_in(auth_server)
    status, _, body = request(
        auth_server,
        "POST",
        "/api/settings",
        {"playerNotifications": {"language": locale}},
        cookie=cookie,
        headers={"X-PZ-Language": "ru" if locale == "en" else "en"},
    )
    assert status == 200 and body["settings"]["playerNotifications"]["language"] == locale
    assert body["settings"]["telegram"]["language"] == "en"
    monkeypatch.setattr(ops, "_SETTINGS", deepcopy(settingsmodel.DEFAULTS))
    ops._load_settings()
    assert ops.player_notification_language() == locale


@pytest.mark.parametrize("value", [None, "", "de", "EN", True, 1, [], {}])
def test_invalid_preference_rejects_entire_patch(value):
    current = deepcopy(settingsmodel.DEFAULTS)
    candidate, error = settingsmodel.prepare_patch(
        current,
        {"autoUpdate": {"enabled": True}, "playerNotifications": {"language": value}},
        Mock(),
    )
    assert candidate is None and error == "playerNotifications.language должен быть ru или en"
    assert current == settingsmodel.DEFAULTS


@pytest.mark.parametrize("section", ["telegram", "playerNotifications"])
@pytest.mark.parametrize("loaded", [{}, {"language": "de"}, {"language": []}, {"language": None}])
def test_legacy_invalid_preferences_default_to_english(section, loaded):
    current = deepcopy(settingsmodel.DEFAULTS)
    settingsmodel.merge_loaded(current, {section: loaded})
    settingsmodel.normalize_loaded(current)
    assert current[section]["language"] == "en"


def test_default_telegram_queue_and_test_message_are_english(monkeypatch):
    import queue

    tg = {"enabled": True, "botToken": "123456:secret", "chatId": "42"}
    monkeypatch.setattr(notify, "_telegram_settings", lambda: tg)
    monkeypatch.setattr(notify, "_QUEUE", queue.Queue())
    notify.enqueue("stop", "Сервер остановлен")
    assert "Server stopped" in notify._QUEUE.get_nowait()
    send = Mock(return_value=(True, None))
    monkeypatch.setattr(notify, "send_message", send)
    assert notify.test_message() == (True, None)
    send.assert_called_once_with("✅ PZ Console: connection test — notifications work.")
