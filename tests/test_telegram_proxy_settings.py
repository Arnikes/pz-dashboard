"""MTProxy configuration, public boundaries and notification integration."""

import json
import logging
import urllib.parse
from collections import deque
from copy import deepcopy
from unittest.mock import Mock

import pytest

import config
import i18n
import logging_utils
import notify
import ops
import settingsmodel
import telegram_mtproxy
from test_auth import auth_server as auth_server, request, sign_in


PROXY = {
    "enabled": True,
    "botToken": "123456:private-bot-token",
    "chatId": "-10012345",
    "mtproxyEnabled": True,
    "mtproxyHost": "proxy.example.com",
    "mtproxyPort": 443,
    "mtproxySecret": "dd0123456789abcdef0123456789abcdef",
    "apiId": 12345,
    "apiHash": "abcdef0123456789abcdef0123456789",
}
FAKETLS_SECRET = "ee0123456789abcdef0123456789abcdef" + "Example.COM".encode().hex()


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    for name in tuple(config.os.environ):
        if name.startswith("TELEGRAM_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(ops, "_SETTINGS", deepcopy(settingsmodel.DEFAULTS))
    monkeypatch.setattr(ops, "_SETTINGS_VERSION", {"epoch": "proxy-test", "revision": 0})
    monkeypatch.setattr(ops, "_EV_MEM", deque(maxlen=300))
    monkeypatch.setattr(notify, "_LAST", {"at": None, "ok": None, "error": None})
    monkeypatch.setitem(config.CFG, "settings_file", str(tmp_path / "settings.json"))
    monkeypatch.setitem(config.CFG, "events_file", str(tmp_path / "events.jsonl"))


@pytest.mark.parametrize("secret", [PROXY["mtproxySecret"], FAKETLS_SECRET])
def test_env_defaults_and_saved_keys_merge_individually(tmp_path, monkeypatch, secret):
    for key, value in {
        "TELEGRAM_ENABLED": "true",
        "TELEGRAM_BOT_TOKEN": PROXY["botToken"],
        "TELEGRAM_MTPROXY_ENABLED": "1",
        "TELEGRAM_MTPROXY_HOST": "env.example.com",
        "TELEGRAM_MTPROXY_PORT": "8443",
        "TELEGRAM_MTPROXY_SECRET": secret,
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": PROXY["apiHash"],
    }.items():
        monkeypatch.setenv(key, value)
    saved = {"telegram": {"chatId": "42", "mtproxyHost": "saved.example.com"}}
    (tmp_path / "settings.json").write_text(json.dumps(saved), encoding="utf-8")
    ops._load_settings()
    raw = ops.telegram_settings_raw()
    assert raw["botToken"] == PROXY["botToken"]
    assert raw["mtproxyEnabled"] is True
    assert raw["mtproxyHost"] == "saved.example.com"
    assert raw["mtproxyPort"] == "8443"
    assert raw["apiId"] == "12345"
    assert raw["chatId"] == "42"
    assert raw["mtproxySecret"] == secret
    assert ops.patch_settings({"telegram": {"mtproxyEnabled": False}}) is None
    monkeypatch.setattr(ops, "_SETTINGS", deepcopy(settingsmodel.DEFAULTS))
    ops._load_settings()
    assert ops.telegram_settings_raw()["mtproxyEnabled"] is False


@pytest.mark.parametrize("value", ["false", "yes", "", None, 0, [], {}])
def test_invalid_saved_proxy_toggle_never_uses_direct_http(tmp_path, monkeypatch, value):
    (tmp_path / "settings.json").write_text(
        json.dumps({"telegram": {**PROXY, "mtproxyEnabled": value}}), encoding="utf-8"
    )
    ops._load_settings()
    direct = Mock()
    monkeypatch.setattr(notify.urllib.request, "urlopen", direct)
    assert notify.send_message("event")[0] is False
    assert "mtproxyEnabled" in ops.get_events()[0]["text"]
    direct.assert_not_called()


@pytest.mark.parametrize("value", ["yes", "disabled", "", "Falsee"])
def test_invalid_env_toggle_is_not_normalized_to_direct(monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_MTPROXY_ENABLED", value)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", PROXY["botToken"])
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    ops._load_settings()
    assert ops.telegram_settings_raw()["mtproxyEnabled"] == value
    direct = Mock()
    monkeypatch.setattr(notify.urllib.request, "urlopen", direct)
    assert notify.send_message("event")[0] is False
    direct.assert_not_called()


@pytest.mark.parametrize("secret", [PROXY["mtproxySecret"], FAKETLS_SECRET])
def test_secret_masks_blank_preservation_and_persistence(monkeypatch, secret):
    proxy = {**PROXY, "mtproxySecret": secret}
    assert ops.patch_settings({"telegram": proxy}) is None
    public = ops.get_settings()["telegram"]
    assert public["botToken"] == public["apiHash"] == public["mtproxySecret"] == ""
    assert public["hasApiHash"] and public["hasMtproxySecret"]
    assert public["apiHashMasked"] == public["mtproxySecretMasked"] == "•••"
    for key in ("botToken", "apiHash", "mtproxySecret"):
        assert proxy[key] not in json.dumps(public)
    assert (
        ops.patch_settings(
            {"telegram": {"apiHash": "", "mtproxySecret": "•••", "mtproxyHost": "new.example.com"}}
        )
        is None
    )
    monkeypatch.setattr(ops, "_SETTINGS", deepcopy(settingsmodel.DEFAULTS))
    ops._load_settings()
    raw = ops.telegram_settings_raw()
    assert raw["apiHash"] == PROXY["apiHash"] and raw["mtproxySecret"] == secret
    assert raw["mtproxyHost"] == "new.example.com"


@pytest.mark.parametrize(
    "patch",
    [
        {"mtproxyEnabled": "true"},
        {"mtproxyPort": True},
        {"mtproxyPort": 0},
        {"mtproxyPort": 65536},
        {"apiId": 1.2},
        {"apiId": True},
        {"apiId": 2147483648},
        {"apiId": "1" * 5000},
        {"mtproxyHost": "https://proxy.example.com/path"},
        {"mtproxyHost": "host\nname"},
        {"apiHash": "short"},
        {"apiHash": []},
        {"mtproxySecret": "ee" + "01" * 16},
        {"mtproxySecret": "ee" + "01" * 16 + "example.com/path".encode().hex()},
        {"mtproxySecret": "tg://proxy?server=proxy.example.com"},
    ],
)
def test_bad_fields_reject_entire_patch_without_advancing_revision(patch, monkeypatch):
    save = Mock()
    monkeypatch.setattr(ops, "_save_settings", save)
    before = deepcopy(ops._SETTINGS)
    data = {"autoUpdate": {"enabled": True}, "telegram": patch}
    original = deepcopy(data)
    assert ops.patch_settings(data)
    assert ops._SETTINGS == before
    assert data == original
    assert ops.get_settings()["version"]["revision"] == 0
    save.assert_not_called()


@pytest.mark.parametrize(
    "secret", ["dd" + "ab" * 15, "ee" + "ab" * 15, "DD" + "ab" * 16, FAKETLS_SECRET]
)
def test_supported_hex_secret_prefixes_are_saved_without_truncation(secret):
    assert ops.patch_settings({"telegram": {"mtproxySecret": secret}}) is None
    assert ops.telegram_settings_raw()["mtproxySecret"] == secret


def test_incomplete_autosave_is_accepted_but_cannot_connect(monkeypatch):
    assert ops.patch_settings({"telegram": {"mtproxyEnabled": True, "apiId": ""}}) is None
    direct = Mock()
    monkeypatch.setattr(notify.urllib.request, "urlopen", direct)
    assert notify.send_message("event")[0] is False
    assert ops.get_events()[0]["type"] == "error"
    direct.assert_not_called()


@pytest.mark.parametrize("secret", [PROXY["mtproxySecret"], FAKETLS_SECRET])
def test_saved_raw_secrets_reach_proxy_transport_and_no_http(monkeypatch, secret):
    proxy = {**PROXY, "mtproxySecret": secret}
    assert ops.patch_settings({"telegram": proxy}) is None
    send = Mock(return_value=(True, None))
    direct = Mock()
    monkeypatch.setattr(telegram_mtproxy, "send_message", send)
    monkeypatch.setattr(notify.urllib.request, "urlopen", direct)
    assert notify.send_message("event") == (True, None)
    sent = send.call_args.args[0]
    assert all(sent[key] == proxy[key] for key in proxy)
    assert notify.test_message() == (True, None)
    assert notify.state()["ok"] is True
    direct.assert_not_called()


@pytest.mark.parametrize("secret", [PROXY["mtproxySecret"], FAKETLS_SECRET])
def test_errors_are_redacted_in_process_events_and_state_without_recursion(
    monkeypatch, caplog, secret
):
    assert ops.patch_settings({"telegram": {**PROXY, "mtproxySecret": secret}}) is None
    message = " ".join(
        [
            PROXY["botToken"],
            PROXY["apiHash"].upper(),
            secret,
            secret.upper(),
            secret[2:34],
            secret[2:34].upper(),
        ]
    )
    monkeypatch.setattr(telegram_mtproxy, "send_message", Mock(side_effect=OSError(message)))
    enqueue = Mock()
    monkeypatch.setattr(notify, "enqueue", enqueue)
    logger = logging.getLogger("pz.events")
    monkeypatch.setattr(logger, "propagate", False)
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.ERROR, logger="pz.events"):
            ok, error = notify.test_message()
    finally:
        logger.removeHandler(caplog.handler)
    assert ok is False and "•••" in error
    logs = caplog.text + json.dumps(ops.get_events()) + json.dumps(notify.state()) + error
    for secret in message.split():
        assert secret not in logs
    enqueue.assert_not_called()
    assert len(ops.get_events()) == 1
    records = [record for record in caplog.records if record.name == "pz.events"]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR


def test_proxy_failure_emits_once_through_reconfigured_process_handler(monkeypatch, capsys):
    assert ops.patch_settings({"telegram": PROXY}) is None
    message = "failure\n" + urllib.parse.quote(PROXY["botToken"], safe="")
    monkeypatch.setattr(telegram_mtproxy, "send_message", Mock(side_effect=OSError(message)))
    enqueue = Mock()
    monkeypatch.setattr(notify, "enqueue", enqueue)
    logger = logging.getLogger("pz")
    handlers, level, propagate = logger.handlers, logger.level, logger.propagate
    try:
        logger.handlers = []
        logger.propagate = False
        logging_utils.configure("DEBUG")
        logging_utils.configure("DEBUG")
        assert notify.send_message("event")[0] is False
        output = capsys.readouterr().err.splitlines()
        assert len(output) == 1
        assert "ERROR pz.events" in output[0] and "•••" in output[0]
        assert "\\x0a" in output[0]
        assert PROXY["botToken"] not in output[0]
        assert urllib.parse.quote(PROXY["botToken"], safe="") not in output[0]
        assert len(ops.get_events()) == 1
        enqueue.assert_not_called()
    finally:
        for handler in logger.handlers:
            if handler not in handlers:
                handler.close()
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def test_invalid_env_faketls_domain_fails_closed_and_redacts_extracted_key(monkeypatch):
    secret = "ee0123456789abcdef0123456789abcdef" + "invalid/path".encode().hex()
    for name, value in {
        "TELEGRAM_MTPROXY_ENABLED": "true",
        "TELEGRAM_MTPROXY_HOST": PROXY["mtproxyHost"],
        "TELEGRAM_MTPROXY_PORT": str(PROXY["mtproxyPort"]),
        "TELEGRAM_MTPROXY_SECRET": secret,
        "TELEGRAM_API_ID": str(PROXY["apiId"]),
        "TELEGRAM_API_HASH": PROXY["apiHash"],
        "TELEGRAM_BOT_TOKEN": PROXY["botToken"],
        "TELEGRAM_CHAT_ID": PROXY["chatId"],
    }.items():
        monkeypatch.setenv(name, value)
    ops._load_settings()
    direct = Mock()
    monkeypatch.setattr(notify.urllib.request, "urlopen", direct)
    assert notify.send_message("event")[0] is False
    direct.assert_not_called()
    error = notify._safe_error(secret + " " + secret[2:34].upper(), {"mtproxySecret": secret})
    assert secret not in error and secret[2:34].upper() not in error


def test_chat_discovery_does_not_bypass_proxy(monkeypatch):
    assert ops.patch_settings({"telegram": PROXY}) is None
    direct = Mock()
    monkeypatch.setattr(notify.urllib.request, "urlopen", direct)
    chats, error = notify.fetch_recent_chats()
    assert chats is None and "Chat ID" in error
    direct.assert_not_called()


@pytest.mark.parametrize("language", ["ru", "en"])
def test_transport_diagnostics_translate_in_state_and_event_payloads(monkeypatch, language):
    assert ops.patch_settings({"telegram": PROXY}) is None
    monkeypatch.setattr(
        telegram_mtproxy,
        "send_message",
        Mock(return_value=(False, "Telegram MTProxy: connection timed out")),
    )
    assert notify.test_message()[0] is False
    expected = "время ожидания" if language == "ru" else "timed out"
    assert expected in i18n.translate(notify.state()["error"], locale=language)
    assert expected in i18n.translate(ops.get_events()[0]["text"], locale=language)


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("secret", [PROXY["mtproxySecret"], FAKETLS_SECRET])
def test_proxy_http_settings_error_and_mask_contract(auth_server, monkeypatch, language, secret):
    cookie = sign_in(auth_server)
    status, _, body = request(
        auth_server,
        "POST",
        "/api/settings",
        {"telegram": {**PROXY, "mtproxySecret": secret}},
        cookie=cookie,
        headers={"X-PZ-Language": language},
    )
    assert status == 200
    assert body["settings"]["telegram"]["apiHash"] == ""
    assert secret not in json.dumps(body)
    monkeypatch.setattr(
        telegram_mtproxy, "send_message", Mock(return_value=(False, "ошибка отправки"))
    )
    status, _, body = request(
        auth_server,
        "POST",
        "/api/notify-test",
        {},
        cookie=cookie,
        headers={"X-PZ-Language": language},
    )
    assert status == 200 and body["ok"] is False
    assert "Telegram MTProxy" in body["error"]
    assert ("send error" if language == "en" else "ошибка отправки") in body["error"]
    assert i18n.translate("telegram.mtproxyEnabled должен быть true/false", locale=language)
