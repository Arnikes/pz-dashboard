#!/usr/bin/env python3
"""Клиент Source RCON (протокол Valve) для Project Zomboid.

RCON PZ слушает TCP-порт 27015 по умолчанию. Каждое подключение
создаётся заново — на LAN это надёжнее, чем держать соединение
(сервер может упасть/перезапуститься в любой момент).
"""

import socket
import struct
import time

SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0
MAX_PACKET = 65535


class RCONError(Exception):
    """Ошибка протокола RCON с человекочитаемым текстом."""


def _pack(rid, ptype, body):
    payload = struct.pack("<ii", rid, ptype) + body.encode("utf-8", "replace") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


class RCON:
    def __init__(
        self, host, port, password, connect_timeout=4.0, idle_timeout=0.35, total_timeout=8.0
    ):
        self.host = host
        self.port = port
        self.password = password
        self.connect_timeout = connect_timeout
        self.idle_timeout = idle_timeout
        self.total_timeout = total_timeout
        self.sock = None
        self._buf = b""

    def _connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        self.sock.settimeout(self.idle_timeout)
        self._buf = b""

    def _close(self):
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self.sock = None

    def _read_packets(self, deadline, wait_first=True):
        out = []
        while time.monotonic() < deadline:
            # Разбираем всё, что уже накопилось в буфере.
            while len(self._buf) >= 4:
                size = struct.unpack("<i", self._buf[:4])[0]
                if size <= 0 or size > MAX_PACKET:  # мусор — сдвигаемся
                    self._buf = self._buf[4:]
                    continue
                if len(self._buf) < 4 + size:
                    break
                pkt = self._buf[4 : 4 + size]
                self._buf = self._buf[4 + size :]
                if len(pkt) < 10:  # минимальный валидный пакет: id+type+2 нуля = 10 байт
                    continue
                rid, typ = struct.unpack("<ii", pkt[:8])
                body = pkt[8:-2].decode("utf-8", "replace") if len(pkt) >= 10 else ""
                out.append((rid, typ, body))
            try:
                self.sock.settimeout(
                    min(self.idle_timeout, max(0.001, deadline - time.monotonic()))
                )
                chunk = self.sock.recv(4096)
            except socket.timeout:
                # Idle time delimits a completed response, not the wait for its
                # first packet or a packet split across TCP reads.
                if (out or not wait_first) and not self._buf:
                    break
                continue
            if not chunk:
                break
            self._buf += chunk
        return out

    def _auth(self):
        self.sock.sendall(_pack(1, SERVERDATA_AUTH, self.password))
        deadline = time.monotonic() + self.connect_timeout + 2.0
        while time.monotonic() < deadline:
            packets = self._read_packets(deadline)
            if not packets:
                break
            for rid, typ, body in packets:
                if typ == SERVERDATA_EXECCOMMAND and rid == 1:
                    # SERVERDATA_AUTH_RESPONSE приходит с rid==1 при успехе
                    return True
                if rid == -1:
                    raise RCONError("Отказ RCON: неверный пароль")
        raise RCONError("RCON не ответил на авторизацию")

    def run(self, command):
        """Выполнить команду, вернуть текст ответа."""
        if not self.password:
            raise RCONError("Пароль RCON не задан (переменная RCON_PASSWORD)")
        try:
            self._connect()
            self._auth()
            rid = 2
            self.sock.sendall(_pack(rid, SERVERDATA_EXECCOMMAND, command))
            deadline = time.monotonic() + self.total_timeout
            parts = []
            while time.monotonic() < deadline:
                packets = self._read_packets(deadline, wait_first=not parts)
                if not packets:
                    break
                for p_rid, p_typ, body in packets:
                    if p_rid == rid and p_typ in (
                        SERVERDATA_RESPONSE_VALUE,
                        SERVERDATA_EXECCOMMAND,
                    ):
                        parts.append(body)
            if self._buf:
                raise RCONError("Неполный ответ RCON")
            if not parts and command.strip().lower() != "quit":
                raise RCONError("RCON не ответил на команду")
            text = "".join(parts)
            if not text.strip():
                # Некоторые команды PZ отвечают пустотой — это нормально.
                return ""
            return text
        except socket.timeout:
            raise RCONError("RCON: таймаут соединения")
        except OSError as e:
            raise RCONError(f"RCON недоступен: {e}")
        finally:
            self._close()


def run_command(host, port, password, command):
    return RCON(host, port, password).run(command)
