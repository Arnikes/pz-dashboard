"""Bounded, non-interactive Telegram MTProxy delivery without session files."""

import asyncio
import ipaddress
import logging
import re
import threading
import time
import warnings

from telegram_proxy_secret import parse_secret

OPERATION_TIMEOUT = 15.0
_CLEANUP_TIMEOUT = 1.0
_LOCK = threading.Lock()
_SESSION = None
_SESSION_KEY = None

_INVALID = "Telegram MTProxy: invalid configuration"
_MISSING = "Telegram MTProxy: bot token or chat ID is missing"
_TIMEOUT = "Telegram MTProxy: connection timed out"
_FAILED = "Telegram MTProxy: connection failed"
_AUTH_FAILED = "Telegram MTProxy: bot authentication failed"
_DELIVERY_FAILED = "Telegram MTProxy: message delivery failed; check chat ID and bot permissions"

# Library messages can contain requests or credentials. Only notify's sanitized
# result is reported; this separate logger never reaches process handlers.
_LIBRARY_LOGGER = logging.getLogger("pz.telegram.mtproxy.transport")
_LIBRARY_LOGGER.addHandler(logging.NullHandler())
_LIBRARY_LOGGER.propagate = False
_LIBRARY_LOGGER.setLevel(logging.CRITICAL + 1)


def normalize_secret(raw):
    """Retain FakeTLS metadata; avoid Telethon's plain/dd prefix heuristic."""
    parsed = parse_secret(raw)
    return parsed.canonical if parsed.mode == "faketls" else parsed.key.hex().upper()


def _valid_host(host):
    if not isinstance(host, str) or not host or host != host.strip():
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        hostname = host.encode("idna").decode("ascii").removesuffix(".")
    except UnicodeError:
        return False
    return len(hostname) <= 253 and all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in hostname.split(".")
    )


def validate_settings(settings):
    """Validate a complete enabled transport; never include supplied values."""
    if not isinstance(settings, dict) or settings.get("mtproxyEnabled") is not True:
        return _INVALID
    if not _valid_host(settings.get("mtproxyHost")):
        return _INVALID
    for key, maximum in (("mtproxyPort", 65535), ("apiId", 2147483647)):
        value = settings.get(key)
        if isinstance(value, str) and len(value) <= 10 and re.fullmatch(r"[0-9]+", value):
            value = int(value)
        if type(value) is not int or not 1 <= value <= maximum:
            return _INVALID
    api_hash = settings.get("apiHash")
    if not isinstance(api_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        return _INVALID
    try:
        normalize_secret(settings.get("mtproxySecret"))
    except ValueError:
        return _INVALID
    return None


def _create_client(settings, session):
    # Direct Bot API users do not import or initialize the MTProto library.
    from telethon import TelegramClient, connection  # noqa: PLC0415
    from telethon.sessions import MemorySession  # noqa: PLC0415

    transport = connection.ConnectionTcpMTProxyRandomizedIntermediate
    if parse_secret(settings["mtproxySecret"]).mode == "faketls":
        from telegram_faketls import ConnectionTcpMTProxyFakeTLS  # noqa: PLC0415

        transport = ConnectionTcpMTProxyFakeTLS

    # Telethon 1.45 warns about missing SOCKS support for every proxy, including
    # its MTProxy transport which uses TCP directly and does not need SOCKS.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="proxy argument will be ignored because python-socks is not installed",
            category=UserWarning,
            module=r"telethon\.client\.telegrambaseclient",
        )
        return TelegramClient(
            session if session is not None else MemorySession(),
            settings["apiId"],
            settings["apiHash"],
            connection=transport,
            proxy=(
                settings["mtproxyHost"],
                settings["mtproxyPort"],
                normalize_secret(settings["mtproxySecret"]),
            ),
            timeout=5,
            request_retries=0,
            connection_retries=0,
            auto_reconnect=False,
            flood_sleep_threshold=0,
            receive_updates=False,
            base_logger=_LIBRARY_LOGGER,
        )


async def _deliver(settings, text, session, holder):
    holder["phase"] = "connect"
    client = _create_client(settings, session)
    holder["client"] = client
    await client.connect()
    holder["phase"] = "authentication"
    if not await client.is_user_authorized():
        await client.sign_in(bot_token=settings["botToken"].strip())
    holder["phase"] = "delivery"
    chat = settings["chatId"].strip()
    destination = int(chat) if re.fullmatch(r"-?[0-9]+", chat) else chat
    await client.send_message(destination, text[:3500], parse_mode=None)


def _wait_task(loop, task, timeout):
    """Unlike wait_for, wait never awaits cancellation-resistant tasks forever."""
    done, _ = loop.run_until_complete(asyncio.wait({task}, timeout=max(0, timeout)))
    if task not in done:
        raise TimeoutError
    return task.result()


def _shutdown(loop, holder, transports, deadline):
    """Bound disconnect, cancel remaining work, and close tracked TCP sockets."""
    error = None
    for task in asyncio.all_tasks(loop):
        task.cancel()
    client = holder.get("client")
    if client is not None:
        try:
            # Calling inside the running loop also supports Telethon's hybrid
            # disconnect method, which otherwise drives its own event loop.
            async def disconnect():
                await client.disconnect()

            task = loop.create_task(disconnect())
            _wait_task(loop, task, min(_CLEANUP_TIMEOUT / 2, deadline - time.monotonic()))
        except TimeoutError:
            error = _TIMEOUT
        except (Exception, asyncio.CancelledError):
            error = _FAILED
    for task in asyncio.all_tasks(loop):
        task.cancel()
    for transport in transports:
        transport.abort()
    pending = asyncio.all_tasks(loop)
    if pending:
        loop.run_until_complete(
            asyncio.wait(pending, timeout=max(0, min(0.1, deadline - time.monotonic())))
        )
    for task in asyncio.all_tasks(loop):
        task.cancel()
    for transport in transports:
        transport.abort()
    # abort schedules connection_lost, which closes sockets on the next turn.
    loop.run_until_complete(asyncio.sleep(0))
    return error


def send_message(settings, text):
    """Return (ok, safe error) within one budget including lock and cleanup.

    A client lives on a single operation loop. Its MemorySession is reused only
    after successful cleanup and only for the exact bot/API/proxy credentials.
    """
    global _SESSION, _SESSION_KEY

    deadline = time.monotonic() + OPERATION_TIMEOUT
    error = validate_settings(settings)
    if error:
        return False, error
    if any(
        not isinstance(settings.get(key), str) or not settings[key].strip()
        for key in ("botToken", "chatId")
    ):
        return False, _MISSING
    settings = dict(
        settings, apiId=int(settings["apiId"]), mtproxyPort=int(settings["mtproxyPort"])
    )
    if not _LOCK.acquire(timeout=max(0, deadline - time.monotonic())):
        return False, _TIMEOUT
    loop = None
    try:
        key = tuple(
            settings[name]
            for name in (
                "botToken",
                "apiId",
                "apiHash",
                "mtproxyHost",
                "mtproxyPort",
                "mtproxySecret",
            )
        )
        session = _SESSION if key == _SESSION_KEY else None
        _SESSION = _SESSION_KEY = None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, _TIMEOUT
        loop = asyncio.new_event_loop()
        # Default asyncio diagnostics include exception/task repr. The caller
        # reports our safe result instead, including errors during shutdown.
        loop.set_exception_handler(lambda _loop, _context: None)
        transports = set()
        create_connection = loop.create_connection

        async def tracked_connection(*args, **kwargs):
            transport, protocol = await create_connection(*args, **kwargs)
            transports.add(transport)
            return transport, protocol

        loop.create_connection = tracked_connection
        holder = {}
        task = loop.create_task(_deliver(settings, text, session, holder))
        error = None
        try:
            reserve = min(_CLEANUP_TIMEOUT, remaining / 3)
            _wait_task(loop, task, deadline - time.monotonic() - reserve)
        except TimeoutError:
            error = _TIMEOUT
        except (Exception, asyncio.CancelledError):
            error = {
                "authentication": _AUTH_FAILED,
                "delivery": _DELIVERY_FAILED,
            }.get(holder.get("phase"), _FAILED)
        cleanup_error = _shutdown(loop, holder, transports, deadline)
        error = error or cleanup_error
        if error is None:
            _SESSION, _SESSION_KEY = holder["client"].session, key
            return True, None
        return False, error
    except Exception:
        return False, _FAILED
    finally:
        if loop is not None:
            loop.close()
        _LOCK.release()
