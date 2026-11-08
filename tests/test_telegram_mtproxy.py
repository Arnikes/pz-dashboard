"""MTProxy transport uses bounded operations and never exposes credentials."""

import asyncio
import datetime
import logging
import socket
import threading
import time

import pytest

import telegram_mtproxy as mtproxy


@pytest.fixture
def settings():
    return {
        "mtproxyEnabled": True,
        "mtproxyHost": "proxy.example.test",
        "mtproxyPort": 443,
        "mtproxySecret": "dd" + "0123456789abcdef" * 2,
        "apiId": 123456,
        "apiHash": "abcdef0123456789" * 2,
        "botToken": "123456:fictional-bot-token",
        "chatId": "-1001234567890",
    }


@pytest.fixture(autouse=True)
def isolated_transport(monkeypatch):
    monkeypatch.setattr(mtproxy, "_LOCK", threading.Lock())
    monkeypatch.setattr(mtproxy, "_SESSION", None)
    monkeypatch.setattr(mtproxy, "_SESSION_KEY", None)
    monkeypatch.setattr(mtproxy, "OPERATION_TIMEOUT", 0.5)
    monkeypatch.setattr(mtproxy, "_CLEANUP_TIMEOUT", 0.1)


class FakeClient:
    def __init__(self, session=None):
        self.session = session if session is not None else object()
        self.calls = []

    async def connect(self):
        self.calls.append(("connect",))

    async def is_user_authorized(self):
        return False

    async def sign_in(self, **kwargs):
        self.calls.append(("sign_in", kwargs))

    async def send_message(self, destination, text, **kwargs):
        self.calls.append(("send", destination, text, kwargs))

    async def disconnect(self):
        self.calls.append(("disconnect",))


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("0123456789abcdef" * 2, "0123456789ABCDEF" * 2),
        ("dd" + "0123456789abcdef" * 2, "0123456789ABCDEF" * 2),
        ("DD" + "0123456789abcdef" * 2, "0123456789ABCDEF" * 2),
        ("dd" + "a" * 30, "DD" + "A" * 30),
        ("ee" + "a" * 30, "EE" + "A" * 30),
    ],
)
def test_secret_forms_preserve_plain_prefixes(raw, canonical):
    assert mtproxy.normalize_secret(raw) == canonical


@pytest.mark.parametrize(
    "raw", [None, "", "a" * 31, "a" * 33, "gg" * 16, "https://example.test/proxy", "YWJjZA=="]
)
def test_secret_rejects_unsupported_forms_without_echoing_input(raw):
    with pytest.raises(ValueError) as error:
        mtproxy.normalize_secret(raw)
    assert str(error.value).startswith("MTProxy secret must be")


def test_faketls_rejected_but_plain_secret_starting_ee_is_supported():
    with pytest.raises(ValueError, match="FakeTLS"):
        mtproxy.normalize_secret("ee" + "a" * 32 + "6578616d706c652e74657374")
    assert mtproxy.normalize_secret("ee" + "a" * 30) == "EE" + "A" * 30


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mtproxyEnabled", "true"),
        ("mtproxyEnabled", False),
        ("mtproxyHost", "https://proxy.example.test"),
        ("mtproxyHost", "user@proxy.example.test"),
        ("mtproxyHost", "proxy.example.test/path"),
        ("mtproxyPort", True),
        ("mtproxyPort", 0),
        ("mtproxyPort", 65536),
        ("mtproxyPort", "443.0"),
        ("apiId", False),
        ("apiId", -1),
        ("apiId", "9" * 5000),
        ("apiHash", "wrong"),
        ("mtproxySecret", "ee" + "a" * 32),
    ],
)
def test_invalid_configuration_never_creates_client(settings, monkeypatch, field, value):
    settings[field] = value
    monkeypatch.setattr(
        mtproxy, "_create_client", lambda *_: pytest.fail("invalid config connected")
    )
    assert mtproxy.send_message(settings, "test") == (False, mtproxy._INVALID)


@pytest.mark.parametrize("chat", ["12345", "-12345", "-1001234567890", "@fictional_channel"])
def test_delivery_is_noninteractive_plain_text_and_closes_client(settings, monkeypatch, chat):
    clients = []

    def factory(_settings, session):
        client = FakeClient(session)
        clients.append(client)
        return client

    monkeypatch.setattr(mtproxy, "_create_client", factory)
    settings["chatId"] = chat
    assert mtproxy.send_message(settings, "*plain* " + "x" * 4000) == (True, None)
    calls = clients[0].calls
    expected = chat if chat.startswith("@") else int(chat)
    assert calls[0] == ("connect",)
    assert calls[1] == ("sign_in", {"bot_token": settings["botToken"]})
    assert calls[2] == ("send", expected, ("*plain* " + "x" * 4000)[:3500], {"parse_mode": None})
    assert calls[-1] == ("disconnect",)


def test_environment_numeric_strings_are_parsed_without_mutating_input(settings, monkeypatch):
    settings.update(apiId="123456", mtproxyPort="443")
    seen = []

    def factory(values, session):
        seen.append(values)
        return FakeClient(session)

    monkeypatch.setattr(mtproxy, "_create_client", factory)
    assert mtproxy.send_message(settings, "test") == (True, None)
    assert seen[0]["apiId"] == 123456 and seen[0]["mtproxyPort"] == 443
    assert settings["apiId"] == "123456" and settings["mtproxyPort"] == "443"


def test_session_reused_only_for_same_credentials(settings, monkeypatch):
    sessions = []

    def factory(_settings, session):
        client = FakeClient(session)
        sessions.append((session, client.session))
        return client

    monkeypatch.setattr(mtproxy, "_create_client", factory)
    assert mtproxy.send_message(settings, "one")[0]
    assert mtproxy.send_message(settings, "two")[0]
    assert sessions[0][0] is None
    assert sessions[1][0] is sessions[0][1]
    settings["botToken"] = "654321:another-fictional-token"
    assert mtproxy.send_message(settings, "three")[0]
    assert sessions[2][0] is None


def test_failure_is_safe_and_discards_session(settings, monkeypatch, caplog):
    raw = settings["mtproxySecret"]

    class FailingClient(FakeClient):
        async def connect(self):
            raise OSError(f"{settings['botToken']} {settings['apiHash']} {raw} {raw.upper()}")

    client = FailingClient()
    monkeypatch.setattr(mtproxy, "_create_client", lambda *_: client)
    with caplog.at_level(logging.DEBUG):
        result = mtproxy.send_message(settings, "test")
    assert result == (False, mtproxy._FAILED)
    assert client.calls == [("disconnect",)]
    assert mtproxy._SESSION is None and mtproxy._SESSION_KEY is None
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    for secret in (settings["botToken"], settings["apiHash"], raw, raw.upper()):
        assert secret not in caplog.text
    assert mtproxy._LOCK.acquire(blocking=False)
    mtproxy._LOCK.release()


def test_lock_wait_is_part_of_total_deadline(settings, monkeypatch):
    monkeypatch.setattr(mtproxy, "OPERATION_TIMEOUT", 0.05)
    mtproxy._LOCK.acquire()
    start = time.monotonic()
    try:
        assert mtproxy.send_message(settings, "test") == (False, mtproxy._TIMEOUT)
    finally:
        mtproxy._LOCK.release()
    assert time.monotonic() - start < 0.3


async def ignore_cancellation():
    while True:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            continue


@pytest.mark.parametrize("phase", ["connect", "disconnect"])
def test_cancel_resistant_work_and_cleanup_are_bounded(settings, monkeypatch, caplog, phase):
    client = FakeClient()
    monkeypatch.setattr(client, phase, ignore_cancellation)
    monkeypatch.setattr(mtproxy, "_create_client", lambda *_: client)
    monkeypatch.setattr(mtproxy, "OPERATION_TIMEOUT", 0.15)
    start = time.monotonic()
    assert mtproxy.send_message(settings, "test") == (False, mtproxy._TIMEOUT)
    assert time.monotonic() - start < 0.5
    assert not caplog.records
    assert mtproxy._LOCK.acquire(blocking=False)
    mtproxy._LOCK.release()


def test_timeout_closes_real_tcp_socket_even_when_disconnect_hangs(settings, monkeypatch, caplog):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(1)
    peers = []
    closed = threading.Event()

    def accept():
        peer, _address = listener.accept()
        peers.append(peer)
        peer.settimeout(1)
        if peer.recv(1) == b"":
            closed.set()

    server = threading.Thread(target=accept, daemon=True)
    server.start()

    class ConnectedClient(FakeClient):
        async def connect(self):
            self.reader, self.writer = await asyncio.open_connection(*listener.getsockname())
            await ignore_cancellation()

        async def disconnect(self):
            await ignore_cancellation()

    monkeypatch.setattr(mtproxy, "_create_client", lambda *_: ConnectedClient())
    monkeypatch.setattr(mtproxy, "OPERATION_TIMEOUT", 0.2)
    try:
        assert mtproxy.send_message(settings, "test") == (False, mtproxy._TIMEOUT)
        assert closed.wait(1), "transport left the connected socket open"
        server.join(timeout=1)
        assert not server.is_alive()
        assert not caplog.records
    finally:
        listener.close()
        for peer in peers:
            peer.close()


def test_real_library_factory_uses_memory_session_and_proxy_only(settings):
    from telethon import connection
    from telethon.sessions import MemorySession

    async def create():
        return mtproxy._create_client(settings, None)

    client = asyncio.run(create())
    assert isinstance(client.session, MemorySession)
    assert client._connection is connection.ConnectionTcpMTProxyRandomizedIntermediate
    assert client._proxy == ("proxy.example.test", 443, "0123456789ABCDEF" * 2)
    assert client._no_updates is True
    assert client._request_retries == 0 and client._connection_retries == 0
    assert client._auto_reconnect is False and client.flood_sleep_threshold == 0
    assert mtproxy._LIBRARY_LOGGER.propagate is False
    assert mtproxy._LIBRARY_LOGGER.getEffectiveLevel() > logging.CRITICAL
    transport = client._connection(
        "149.154.167.51",
        443,
        2,
        loggers=client._log,
        proxy=client._proxy,
    )
    assert transport._ip == "proxy.example.test" and transport._port == 443


def test_library_child_logs_do_not_propagate(settings, caplog):
    with caplog.at_level(logging.DEBUG):
        mtproxy._LIBRARY_LOGGER.getChild("network").error(settings["botToken"])
    assert not caplog.records


def test_cleanup_failure_is_safe_and_does_not_cache_session(settings, monkeypatch):
    class FailingCleanup(FakeClient):
        async def disconnect(self):
            raise OSError(settings["mtproxySecret"])

    monkeypatch.setattr(mtproxy, "_create_client", lambda *_: FailingCleanup())
    assert mtproxy.send_message(settings, "test") == (False, mtproxy._FAILED)
    assert mtproxy._SESSION is None and mtproxy._SESSION_KEY is None


@pytest.mark.parametrize(
    ("method", "error_name", "expected"),
    [
        ("connect", None, mtproxy._FAILED),
        ("sign_in", "AccessTokenInvalidError", mtproxy._AUTH_FAILED),
        ("send_message", "ChatWriteForbiddenError", mtproxy._DELIVERY_FAILED),
        ("send_message", "PeerIdInvalidError", mtproxy._DELIVERY_FAILED),
    ],
)
def test_rpc_failures_distinguish_authentication_and_chat_from_connection(
    settings, monkeypatch, method, error_name, expected
):
    from telethon import errors

    client = FakeClient()

    async def fail(*_args, **_kwargs):
        if error_name is None:
            raise OSError("fictional connection failure")
        raise getattr(errors, error_name)(request=None)

    monkeypatch.setattr(client, method, fail)
    monkeypatch.setattr(mtproxy, "_create_client", lambda *_: client)
    assert mtproxy.send_message(settings, "test") == (False, expected)
    assert client.calls[-1] == ("disconnect",)
    assert mtproxy._SESSION is None and mtproxy._SESSION_KEY is None


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("is_user_authorized", mtproxy._AUTH_FAILED),
        ("sign_in", mtproxy._AUTH_FAILED),
        ("send_message", mtproxy._DELIVERY_FAILED),
    ],
)
def test_authentication_and_delivery_diagnostics_do_not_expose_exception_details(
    settings, monkeypatch, caplog, method, expected
):
    client = FakeClient()
    sensitive = " ".join(
        [
            settings["botToken"],
            settings["apiHash"],
            settings["mtproxySecret"],
            mtproxy.normalize_secret(settings["mtproxySecret"]),
        ]
    )

    async def fail(*_args, **_kwargs):
        raise ValueError(sensitive)

    monkeypatch.setattr(client, method, fail)
    monkeypatch.setattr(mtproxy, "_create_client", lambda *_: client)
    with caplog.at_level(logging.DEBUG):
        result = mtproxy.send_message(settings, "test")
    assert result == (False, expected)
    assert sensitive not in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert mtproxy._LOCK.acquire(blocking=False)
    mtproxy._LOCK.release()


@pytest.mark.parametrize("kind", ["user", "group", "channel"])
def test_real_library_resolves_numeric_peer_without_cached_access_hash(settings, monkeypatch, kind):
    from telethon import functions, types

    user_id, group_id, channel_id = 12345, 23456, 34567
    requests = []

    async def resolve():
        client = mtproxy._create_client(settings, None)

        async def request(_self, value, **_kwargs):
            requests.append(value)
            if isinstance(value, functions.users.GetUsersRequest):
                return [types.User(id=user_id, access_hash=54321)]
            assert isinstance(value, functions.channels.GetChannelsRequest)
            return types.messages.Chats(
                chats=[
                    types.Channel(
                        id=channel_id,
                        title="Fictional group",
                        photo=types.ChatPhotoEmpty(),
                        date=datetime.datetime.now(datetime.UTC),
                        access_hash=76543,
                    )
                ]
            )

        monkeypatch.setattr(type(client), "__call__", request)
        peer_id = {"user": user_id, "group": -group_id, "channel": -1000000000000 - channel_id}[
            kind
        ]
        return await client.get_input_entity(peer_id)

    entity = asyncio.run(resolve())
    if kind == "user":
        assert entity == types.InputPeerUser(user_id=user_id, access_hash=54321)
        assert requests[0].id == [types.InputUser(user_id=user_id, access_hash=0)]
    elif kind == "group":
        assert entity == types.InputPeerChat(chat_id=group_id)
        assert requests == []
    else:
        assert entity == types.InputPeerChannel(channel_id=channel_id, access_hash=76543)
        assert requests[0].id == [types.InputChannel(channel_id=channel_id, access_hash=0)]
