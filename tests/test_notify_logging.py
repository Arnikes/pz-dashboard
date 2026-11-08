"""Direct Telegram diagnostics stay bounded, useful, and credential-free."""

import io
import json
import urllib.error
import urllib.parse
from unittest.mock import Mock

import pytest

import i18n
import notify
import ops
from test_auth import auth_server as auth_server, request, sign_in


@pytest.fixture
def telegram(monkeypatch):
    settings = {
        "botToken": "123456:private-bot-token",
        "chatId": "-10042",
        "apiHash": "abcdef0123456789abcdef0123456789",
        "mtproxySecret": "dd0123456789abcdef0123456789abcdef",
    }
    monkeypatch.setattr(ops, "telegram_settings_raw", lambda: settings)
    monkeypatch.setattr(notify, "_LAST", {"at": None, "ok": None, "error": None})
    return settings


def _diagnostic(settings):
    secrets = [settings[key] for key in ("botToken", "apiHash", "mtproxySecret")]
    secrets.extend((settings["apiHash"].upper(), settings["mtproxySecret"][2:34].upper()))
    secrets.extend(urllib.parse.quote(value, safe="") for value in list(secrets))
    return "Bad Request: chat not found " + " ".join(secrets), secrets


@pytest.mark.parametrize(
    "operation", [lambda: notify.send_message("message"), notify.fetch_recent_chats]
)
@pytest.mark.parametrize("failure", ["api", "http", "http-fallback", "network"])
def test_all_direct_api_errors_redact_credentials(monkeypatch, telegram, operation, failure):
    diagnostic, secrets = _diagnostic(telegram)

    def urlopen(req, timeout):
        assert timeout == 10
        payload = json.dumps({"ok": False, "description": diagnostic}).encode()
        if failure == "http":
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(payload))
        if failure == "http-fallback":
            raise urllib.error.HTTPError(req.full_url, 500, diagnostic, {}, io.BytesIO(b"invalid"))
        if failure == "network":
            raise OSError(diagnostic)
        return io.BytesIO(payload)

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    result, error = operation()
    assert not result
    assert "chat not found" in error and "•••" in error
    assert all(secret not in error for secret in secrets)


@pytest.mark.parametrize(
    "operation", [lambda: notify.send_message("message"), notify.fetch_recent_chats]
)
@pytest.mark.parametrize("body", [b"[]", b"null", b"invalid JSON", b"\xff", b'{"ok": false}'])
def test_malformed_bot_api_envelopes_return_translatable_errors(
    monkeypatch, telegram, operation, body
):
    response = io.BytesIO(body)
    monkeypatch.setattr(notify.urllib.request, "urlopen", Mock(return_value=response))
    result, error = operation()
    assert not result and error == "Telegram вернул ошибку"
    assert i18n.translate(error, locale="en") == "Telegram returned an error"
    assert response.closed


@pytest.mark.parametrize(
    "operation", [lambda: notify.send_message("message"), notify.fetch_recent_chats]
)
@pytest.mark.parametrize("http_error", [False, True])
def test_success_and_error_response_reads_are_bounded(monkeypatch, telegram, operation, http_error):
    monkeypatch.setattr(notify, "_API_RESPONSE_LIMIT", 64)
    reads = []

    class Response(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            assert 0 < size <= 65
            return super().read(size)

    response = Response(b"x" * 1000)

    def urlopen(req, timeout):
        if http_error:
            raise urllib.error.HTTPError(req.full_url, 500, "Internal Error", {}, response)
        return response

    monkeypatch.setattr(notify.urllib.request, "urlopen", urlopen)
    result, error = operation()
    assert not result and error
    assert reads == [65] and response.closed


def test_malformed_chat_updates_do_not_escape_to_http_handler(monkeypatch, telegram):
    payload = {"ok": True, "result": [None, 42, "invalid", {"message": {"chat": {"id": 42}}}]}
    monkeypatch.setattr(
        notify.urllib.request,
        "urlopen",
        Mock(return_value=io.BytesIO(json.dumps(payload).encode())),
    )
    assert notify.fetch_recent_chats() == ([{"id": "42", "title": "42", "type": ""}], None)
    monkeypatch.setattr(
        notify.urllib.request,
        "urlopen",
        Mock(return_value=io.BytesIO(b'{"ok":true,"result":{"invalid":true}}')),
    )
    assert notify.fetch_recent_chats() == (None, "Telegram вернул ошибку")


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("path", ["/api/notify-test", "/api/telegram-chats"])
def test_direct_api_diagnostics_are_safe_through_real_handlers(
    auth_server, monkeypatch, telegram, language, path, caplog
):
    cookie = sign_in(auth_server)
    diagnostic, secrets = _diagnostic(telegram)
    monkeypatch.setattr(
        notify.urllib.request,
        "urlopen",
        Mock(
            return_value=io.BytesIO(json.dumps({"ok": False, "description": diagnostic}).encode())
        ),
    )
    status, _, body = request(
        auth_server,
        "POST" if path == "/api/notify-test" else "GET",
        path,
        {} if path == "/api/notify-test" else None,
        cookie=cookie,
        headers={"X-PZ-Language": language},
    )
    assert status == 200 and body["ok"] is False
    evidence = json.dumps(body) + json.dumps(notify.state()) + caplog.text
    assert all(secret not in evidence for secret in secrets)
    assert "chat not found" in body["error"]
    if path == "/api/notify-test":
        assert "chat id: -10042" in body["error"]
