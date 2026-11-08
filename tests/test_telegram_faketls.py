"""Independent FakeTLS peer exercises the actual Telethon MTProxy wire codec."""

import asyncio
import collections
import hashlib
import hmac
import logging
import socket
import struct
import threading
import time

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from telegram_faketls import ConnectionTcpMTProxyFakeTLS
import telegram_mtproxy as mtproxy

KEY = bytes(range(16))
DOMAIN = b"Example.COM"
SECRET = (b"\xee" + KEY + DOMAIN).hex()
LOGGERS = collections.defaultdict(lambda: logging.getLogger("test.faketls"))


def record(kind, payload, version=b"\x03\x03"):
    return bytes([kind]) + version + struct.pack(">H", len(payload)) + payload


async def read_record(reader):
    header = await reader.readexactly(5)
    return header[0], header[1:3], await reader.readexactly(int.from_bytes(header[3:], "big"))


async def read_hello(reader, expected_domain=DOMAIN, key=KEY):
    kind, version, body = await read_record(reader)
    assert kind == 0x16 and version == b"\x03\x01"
    assert body[:1] == b"\x01" and int.from_bytes(body[1:4], "big") == len(body) - 4
    assert body[4:6] == b"\x03\x03"
    hello = record(kind, body, version)
    random = body[6:38]
    assert len(random) == 32
    authenticated = hmac.digest(key, hello[:11] + bytes(32) + hello[43:], "sha256")
    assert hmac.compare_digest(random[:28], authenticated[:28])
    timestamp = int.from_bytes(
        bytes(a ^ b for a, b in zip(random[28:], authenticated[28:])), "little"
    )
    assert abs(timestamp - int(time.time())) < 5
    session_length = body[38]
    session = body[39 : 39 + session_length]
    assert session_length == 32
    offset = 39 + session_length
    cipher_length = int.from_bytes(body[offset : offset + 2], "big")
    ciphers = body[offset + 2 : offset + 2 + cipher_length]
    assert b"\x13\x01" in ciphers
    offset += 2 + cipher_length
    offset += 1 + body[offset]
    extension_length = int.from_bytes(body[offset : offset + 2], "big")
    offset += 2
    assert offset + extension_length == len(body)
    extensions = {}
    while offset < len(body):
        kind, length = struct.unpack(">HH", body[offset : offset + 4])
        offset += 4
        extensions[kind] = body[offset : offset + length]
        offset += length
    sni = extensions[0]
    assert int.from_bytes(sni[:2], "big") == len(sni) - 2
    assert sni[2] == 0
    assert int.from_bytes(sni[3:5], "big") == len(expected_domain)
    assert sni[5:] == expected_domain
    assert b"\x03\x04" in extensions[43]
    assert len(extensions[51]) > 6
    return random, session, hello


def server_flight(client_random, session, *, noise_size=7000, corrupt=None):
    versions = b"\x00\x2b\x00\x02\x03\x04"
    # The peer does not negotiate actual TLS; the key share is camouflage.
    share = b"\x00\x33\x00\x24\x00\x1d\x00\x20" + bytes(range(32))
    # An additional legal extension proves the reader does not assume127bytes.
    extension = b"\xfa\xfa\x00\x04test"
    extensions = versions + share + extension
    body = (
        b"\x03\x03"
        + bytes(32)
        + bytes([len(session)])
        + session
        + b"\x13\x01\x00"
        + struct.pack(">H", len(extensions))
        + extensions
    )
    handshake = b"\x02" + len(body).to_bytes(3, "big") + body
    noise = bytes(index % 251 for index in range(noise_size))
    data = bytearray(record(0x16, handshake) + record(0x14, b"\x01") + record(0x17, noise))
    data[11:43] = hmac.digest(KEY, client_random + data, "sha256")
    if corrupt == "digest":
        data[11] ^= 1
    elif corrupt == "noise":
        data[-1] ^= 1
    elif corrupt == "version":
        data[1] = 0x04
    elif corrupt == "type":
        data[0] = 0x15
    elif corrupt == "session":
        data[44] ^= 1
    elif corrupt == "length":
        data[8] ^= 1
    elif corrupt == "ccs":
        data[5 + len(handshake) + 5] = 2
    return bytes(data)


class PeerStream:
    def __init__(self, reader):
        self.reader = reader
        self.buffer = bytearray()
        self.ccs = 0
        self.sizes = []

    async def read(self, size):
        while len(self.buffer) < size:
            kind, version, payload = await read_record(self.reader)
            assert version == b"\x03\x03"
            if kind == 0x14:
                assert not self.ccs and payload == b"\x01"
                self.ccs += 1
                continue
            assert kind == 0x17 and self.ccs == 1
            assert 0 < len(payload) <= 2878
            self.sizes.append(len(payload))
            self.buffer.extend(payload)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    async def obfuscation(self):
        header = await self.read(64)
        reverse = header[8:56][::-1]
        decrypt = Cipher(
            algorithms.AES(hashlib.sha256(header[8:40] + KEY).digest()),
            modes.CTR(header[40:56]),
        ).decryptor()
        plain = decrypt.update(header)
        assert plain[56:60] == b"\xdd" * 4
        assert int.from_bytes(plain[60:62], "little", signed=True) == 2
        encrypt = Cipher(
            algorithms.AES(hashlib.sha256(reverse[:32] + KEY).digest()),
            modes.CTR(reverse[32:]),
        ).encryptor()
        return decrypt, encrypt

    async def packet(self, decrypt):
        size = int.from_bytes(decrypt.update(await self.read(4)), "little", signed=True)
        assert 0 <= size < 200000
        payload = decrypt.update(await self.read(size))
        return payload[: size - size % 4]


async def fragmented_write(writer, data):
    for offset in range(0, len(data), 113):
        writer.write(data[offset : offset + 113])
        await writer.drain()
        await asyncio.sleep(0)


def connection(port, *, secret=SECRET):
    return ConnectionTcpMTProxyFakeTLS(
        "149.154.167.51", 443, 2, loggers=LOGGERS, proxy=("127.0.0.1", port, secret)
    )


def test_actual_wire_authenticates_then_exchanges_large_padded_mtproxy_packets():
    async def run():
        result = asyncio.get_running_loop().create_future()
        requests = [b"first packet", bytes(range(256)) * 100]
        responses = [bytes(range(255, -1, -1)) * 100, b"last response!!!"]

        async def peer(reader, writer):
            try:
                random, session, _ = await read_hello(reader)
                await fragmented_write(writer, server_flight(random, session))
                stream = PeerStream(reader)
                decrypt, encrypt = await stream.obfuscation()
                for expected, response in zip(requests, responses):
                    assert await stream.packet(decrypt) == expected
                    # Split the encrypted length and payload across TLS records,
                    # coalesce those records on the TCP stream, fragment TCP too.
                    wire = encrypt.update(struct.pack("<I", len(response)) + response)
                    framed = record(0x17, wire[:2])
                    for offset in range(2, len(wire), 15000):
                        framed += record(0x17, wire[offset : offset + 15000])
                    await fragmented_write(writer, framed)
                assert stream.ccs == 1 and len(stream.sizes) > 3
                result.set_result(True)
                await reader.read()
            except BaseException as error:
                if not result.done():
                    result.set_exception(error)
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(peer, "127.0.0.1", 0)
        client = connection(server.sockets[0].getsockname()[1])
        try:
            await client.connect(timeout=2)
            for request, expected in zip(requests, responses):
                await client.send(request)
                assert await asyncio.wait_for(client.recv(), 2) == expected
            await asyncio.wait_for(result, 2)
        finally:
            await client.disconnect()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


@pytest.mark.parametrize(
    "corrupt", ["digest", "noise", "version", "type", "session", "length", "ccs"]
)
def test_bad_server_handshake_closes_without_sending_mtproxy_header(corrupt):
    async def run():
        result = asyncio.get_running_loop().create_future()

        async def peer(reader, writer):
            try:
                random, session, _ = await read_hello(reader)
                writer.write(server_flight(random, session, corrupt=corrupt))
                await writer.drain()
                result.set_result(await asyncio.wait_for(reader.read(), 1))
            except BaseException as error:
                if not result.done():
                    result.set_exception(error)
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(peer, "127.0.0.1", 0)
        client = connection(server.sockets[0].getsockname()[1])
        try:
            with pytest.raises(ConnectionError, match="MTProxy FakeTLS"):
                await client.connect(timeout=1)
            assert await result == b"", "unauthenticated peer received MTProxy header"
        finally:
            await client.disconnect()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_each_connection_uses_fresh_authenticated_hello():
    async def run():
        hellos = []

        async def peer(reader, writer):
            random, session, hello = await read_hello(reader)
            hellos.append((random, session, hello))
            writer.write(server_flight(random, session))
            await writer.drain()
            await reader.read()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(peer, "127.0.0.1", 0)
        try:
            for _ in range(2):
                client = connection(server.sockets[0].getsockname()[1])
                await client.connect(timeout=1)
                await client.disconnect()
            assert len(hellos) == 2
            assert hellos[0][0] != hellos[1][0]
            assert hellos[0][1] != hellos[1][1]
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


@pytest.mark.parametrize("reply", [b"", b"\x16\x03", b"\x16\x03\x03\xff\xff"])
def test_truncated_or_oversized_server_handshake_fails_closed(reply):
    async def run():
        closed = asyncio.Event()

        async def peer(reader, writer):
            await read_hello(reader)
            writer.write(reply)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            closed.set()

        server = await asyncio.start_server(peer, "127.0.0.1", 0)
        client = connection(server.sockets[0].getsockname()[1])
        try:
            with pytest.raises((ConnectionError, asyncio.IncompleteReadError)):
                await client.connect(timeout=1)
            await asyncio.wait_for(closed.wait(), 1)
        finally:
            await client.disconnect()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


@pytest.mark.parametrize(
    "reply",
    [
        record(0x15, b"\x02\x28"),
        record(0x17, b"x", b"\x03\x01"),
        record(0x14, b"\x02"),
        record(0x17, b"") * 33,
    ],
)
def test_malformed_application_records_are_not_accepted_as_mtproto(reply):
    async def run():
        async def peer(reader, writer):
            random, session, _ = await read_hello(reader)
            writer.write(server_flight(random, session))
            await writer.drain()
            stream = PeerStream(reader)
            await stream.obfuscation()
            writer.write(reply)
            await writer.drain()
            await reader.read()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(peer, "127.0.0.1", 0)
        client = connection(server.sockets[0].getsockname()[1])
        try:
            await client.connect(timeout=1)
            with pytest.raises(ConnectionError):
                await asyncio.wait_for(client.recv(), 1)
        finally:
            await client.disconnect()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


@pytest.mark.parametrize("authenticate", [False, True])
def test_real_client_timeout_closes_socket_before_or_after_faketls_authentication(
    monkeypatch, caplog, authenticate
):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(2)
    closed = threading.Event()
    failures = []

    def peer():
        try:
            with listener.accept()[0] as sock:
                sock.settimeout(2)

                def read(size):
                    data = b""
                    while len(data) < size:
                        part = sock.recv(size - len(data))
                        if not part:
                            raise EOFError
                        data += part
                    return data

                header = read(5)
                hello = header + read(int.from_bytes(header[3:], "big"))
                if authenticate:
                    sock.sendall(server_flight(hello[11:43], hello[44:76]))
                while sock.recv(65536):
                    pass
                closed.set()
        except BaseException as error:
            failures.append(error)

    server = threading.Thread(target=peer, daemon=True)
    server.start()
    settings = {
        "mtproxyEnabled": True,
        "mtproxyHost": "127.0.0.1",
        "mtproxyPort": listener.getsockname()[1],
        "mtproxySecret": SECRET,
        "apiId": 123456,
        "apiHash": "abcdef0123456789" * 2,
        "botToken": "123456:fictional-bot-token",
        "chatId": "-1001234567890",
    }
    monkeypatch.setattr(mtproxy, "_LOCK", threading.Lock())
    monkeypatch.setattr(mtproxy, "_SESSION", None)
    monkeypatch.setattr(mtproxy, "_SESSION_KEY", None)
    monkeypatch.setattr(mtproxy, "OPERATION_TIMEOUT", 0.3)
    monkeypatch.setattr(mtproxy, "_CLEANUP_TIMEOUT", 0.1)
    try:
        start = time.monotonic()
        assert mtproxy.send_message(settings, "test") == (False, mtproxy._TIMEOUT)
        assert time.monotonic() - start < 1
        assert closed.wait(1), "FakeTLS failure left its real TCP socket open"
        server.join(timeout=1)
        assert not server.is_alive() and failures == []
        assert not caplog.records
        for sensitive in (SECRET, SECRET.upper(), KEY.hex(), KEY.hex().upper()):
            assert sensitive not in caplog.text
        assert mtproxy._LOCK.acquire(blocking=False)
        mtproxy._LOCK.release()
    finally:
        listener.close()


def test_application_eof_preserves_buffered_partial_bytes():
    from telegram_faketls import _TLSReader

    async def run():
        upstream = asyncio.StreamReader()
        upstream.feed_data(record(0x17, b"abc"))
        upstream.feed_eof()
        reader = _TLSReader(upstream)
        assert await reader.readexactly(1) == b"a"
        with pytest.raises(asyncio.IncompleteReadError) as error:
            await reader.readexactly(4)
        assert error.value.partial == b"bc" and error.value.expected == 4
        assert reader.at_eof()

    asyncio.run(run())


def test_application_read_size_is_bounded_before_network_access():
    from telegram_faketls import _TLSReader

    async def run():
        reader = _TLSReader(asyncio.StreamReader())
        with pytest.raises(ConnectionError, match="record"):
            await reader.readexactly(16 * 1024 * 1024 + 1)

    asyncio.run(run())
