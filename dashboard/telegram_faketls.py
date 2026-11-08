"""Authenticated FakeTLS record layer around Telethon's MTProxy transport.

The protocol authenticates TLS-shaped records with the proxy key; it does not
negotiate a TLS session. OpenSSL only generates a fresh, valid ClientHello.
MTProxy encryption and padded-intermediate packets remain Telethon's work.
"""

import asyncio
import hmac
import ssl
import struct
import time

from telethon.network.connection.tcpmtproxy import ConnectionTcpMTProxyRandomizedIntermediate

from telegram_proxy_secret import parse_secret

_CCS = b"\x14\x03\x03\x00\x01\x01"
_MAX_HELLO = 65536
_MAX_READ = 16 * 1024 * 1024
_WRITE_CHUNK = 2878
_BAD_HANDSHAKE = "MTProxy FakeTLS handshake failed"
_BAD_RECORD = "MTProxy FakeTLS record is invalid"


def _hello_fields(hello, domain):
    """Fail closed if an OpenSSL change produces an unexpected Hello layout."""
    if (
        len(hello) < 80
        or len(hello) > _MAX_HELLO
        or hello[:3] != b"\x16\x03\x01"
        or int.from_bytes(hello[3:5], "big") != len(hello) - 5
        or hello[5] != 1
        or int.from_bytes(hello[6:9], "big") != len(hello) - 9
        or hello[9:11] != b"\x03\x03"
        or hello[43] != 32
    ):
        raise ConnectionError(_BAD_HANDSHAKE)
    session_id = bytes(hello[44:76])
    offset = 76
    cipher_length = int.from_bytes(hello[offset : offset + 2], "big")
    offset += 2 + cipher_length
    if not cipher_length or cipher_length % 2 or offset >= len(hello):
        raise ConnectionError(_BAD_HANDSHAKE)
    compression_length = hello[offset]
    offset += 1 + compression_length
    if offset + 2 > len(hello):
        raise ConnectionError(_BAD_HANDSHAKE)
    length = int.from_bytes(hello[offset : offset + 2], "big")
    offset += 2
    if offset + length != len(hello):
        raise ConnectionError(_BAD_HANDSHAKE)
    server_name = b"\x00" + len(domain).to_bytes(2, "big") + domain
    expected_sni = len(server_name).to_bytes(2, "big") + server_name
    found = False
    while offset < len(hello):
        if offset + 4 > len(hello):
            raise ConnectionError(_BAD_HANDSHAKE)
        kind = hello[offset : offset + 2]
        length = int.from_bytes(hello[offset + 2 : offset + 4], "big")
        offset += 4
        if offset + length > len(hello):
            raise ConnectionError(_BAD_HANDSHAKE)
        if kind == b"\x00\x00":
            found = hello[offset : offset + length] == expected_sni
        offset += length
    if not found:
        raise ConnectionError(_BAD_HANDSHAKE)
    return session_id


def _client_hello(secret):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.set_alpn_protocols(["h2", "http/1.1"])
    outgoing = ssl.MemoryBIO()
    tls = context.wrap_bio(ssl.MemoryBIO(), outgoing, server_hostname=secret.domain)
    try:
        tls.do_handshake()
    except ssl.SSLWantReadError:
        pass
    else:
        raise ConnectionError(_BAD_HANDSHAKE)
    hello = bytearray(outgoing.read())
    session_id = _hello_fields(hello, secret.domain.encode("ascii"))
    hello[11:43] = bytes(32)
    digest = bytearray(hmac.digest(secret.key, hello, "sha256"))
    timestamp = struct.pack("<I", int(time.time()) & 0xFFFFFFFF)
    for index in range(4):
        digest[28 + index] ^= timestamp[index]
    hello[11:43] = digest
    return bytes(hello), bytes(digest), session_id


async def _read_record(reader, *, remaining=None):
    header = await reader.readexactly(5)
    length = int.from_bytes(header[3:5], "big")
    if header[1:3] != b"\x03\x03" or (remaining is not None and length + 5 > remaining):
        raise ConnectionError(_BAD_RECORD)
    return header, await reader.readexactly(length)


async def _authenticate(reader, writer, secret):
    hello, digest, session_id = _client_hello(secret)
    writer.write(hello)
    await writer.drain()
    flight = bytearray()
    records = []
    for expected in (0x16, 0x14, 0x17):
        header, payload = await _read_record(reader, remaining=_MAX_HELLO - len(flight))
        if header[0] != expected:
            raise ConnectionError(_BAD_HANDSHAKE)
        flight.extend(header)
        flight.extend(payload)
        records.append(payload)
    server, ccs, _noise = records
    if (
        len(server) < 72
        or server[:1] != b"\x02"
        or int.from_bytes(server[1:4], "big") != len(server) - 4
        or server[4:6] != b"\x03\x03"
        or server[38] != len(session_id)
        or server[39 : 39 + len(session_id)] != session_id
        or ccs != b"\x01"
    ):
        raise ConnectionError(_BAD_HANDSHAKE)
    received = bytes(flight[11:43])
    flight[11:43] = bytes(32)
    expected = hmac.digest(secret.key, digest + flight, "sha256")
    if not hmac.compare_digest(received, expected):
        raise ConnectionError(_BAD_HANDSHAKE)


class _TLSReader:
    def __init__(self, upstream):
        self._upstream = upstream
        self._buffer = bytearray()

    async def readexactly(self, size):
        if size < 0 or size > _MAX_READ:
            raise ConnectionError(_BAD_RECORD)
        empty_records = 0
        while len(self._buffer) < size:
            try:
                header, payload = await _read_record(self._upstream)
            except asyncio.IncompleteReadError:
                partial = bytes(self._buffer)
                self._buffer.clear()
                raise asyncio.IncompleteReadError(partial, size) from None
            if header[0] == 0x14 and payload == b"\x01":
                payload = b""
            elif header[0] != 0x17:
                raise ConnectionError(_BAD_RECORD)
            if not payload:
                empty_records += 1
                if empty_records > 32:
                    raise ConnectionError(_BAD_RECORD)
            self._buffer.extend(payload)
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def at_eof(self):
        return not self._buffer and self._upstream.at_eof()


class _TLSWriter:
    def __init__(self, upstream):
        self._upstream = upstream
        self._sent_ccs = False

    def write(self, data):
        if not data:
            return
        if not self._sent_ccs:
            self._upstream.write(_CCS)
            self._sent_ccs = True
        for offset in range(0, len(data), _WRITE_CHUNK):
            part = data[offset : offset + _WRITE_CHUNK]
            self._upstream.write(b"\x17\x03\x03" + len(part).to_bytes(2, "big") + part)

    async def drain(self):
        await self._upstream.drain()

    def close(self):
        self._upstream.close()

    async def wait_closed(self):
        await self._upstream.wait_closed()

    def is_closing(self):
        return self._upstream.is_closing()

    def get_extra_info(self, name, default=None):
        return self._upstream.get_extra_info(name, default)

    @property
    def transport(self):
        return self._upstream.transport


class ConnectionTcpMTProxyFakeTLS(ConnectionTcpMTProxyRandomizedIntermediate):
    """Authenticate FakeTLS before Telethon initializes its MTProxy stream."""

    def __init__(self, ip, port, dc_id, *, loggers, proxy=None, local_addr=None):
        if not isinstance(proxy, (tuple, list)) or len(proxy) != 3:
            raise ValueError(_BAD_HANDSHAKE)
        self._tls_secret = parse_secret(proxy[2])
        if self._tls_secret.mode != "faketls":
            raise ValueError(_BAD_HANDSHAKE)
        super().__init__(
            ip,
            port,
            dc_id,
            loggers=loggers,
            proxy=(proxy[0], proxy[1], self._tls_secret.key.hex().upper()),
            local_addr=local_addr,
        )
        self._local_addr = local_addr

    async def _connect(self, timeout=None, ssl=None):
        if ssl is not None:
            raise ValueError(_BAD_HANDSHAKE)
        local_addr = self._local_addr
        if isinstance(local_addr, str):
            local_addr = (local_addr, 0)
        elif local_addr is not None and not (
            isinstance(local_addr, tuple) and len(local_addr) == 2
        ):
            raise ValueError(_BAD_HANDSHAKE)
        try:
            async with asyncio.timeout(timeout):
                # Assign the raw writer before authentication, so every failure
                # path can close it even before Connection.connect completes.
                self._reader, self._writer = await asyncio.open_connection(
                    self._ip, self._port, local_addr=local_addr
                )
                await _authenticate(self._reader, self._writer, self._tls_secret)
                self._reader = _TLSReader(self._reader)
                self._writer = _TLSWriter(self._writer)
                self._codec = self.packet_codec(self)
                self._init_conn()
                await self._writer.drain()
        except BaseException:
            if self._writer is not None:
                self._writer.close()
            raise
