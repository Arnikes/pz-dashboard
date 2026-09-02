"""Юнит-тесты ядра пульта: парсеры docker, RCON-протокол (с фейковым сервером),
настройки, история онлайна. Запуск: pytest -q tests (или из корня проекта)."""
import json
import struct
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD_DIR))

import config  # noqa: E402
import dockerlib  # noqa: E402
import ops  # noqa: E402
import rcon  # noqa: E402


# ───────────────────────── парсеры ─────────────────────────

def test_parse_bytes():
    assert dockerlib.parse_bytes("123MiB") == 123 * 1024 * 1024
    assert dockerlib.parse_bytes("1.9GiB") == int(1.9 * 1024 ** 3)
    assert dockerlib.parse_bytes("1.20kB") == 1200
    assert dockerlib.parse_bytes("42B") == 42
    assert dockerlib.parse_bytes("мусор") == 0


def test_fmt_size():
    assert ops.fmt_size(0) == "0 Б"
    assert ops.fmt_size(1024) == "1.0 КБ"
    assert ops.fmt_size(684000000).endswith("МБ")


def test_stats_parse(monkeypatch):
    monkeypatch.setattr(
        dockerlib, "sh",
        lambda args, timeout=120: (0, "12.34%|123MiB / 1.9GiB|6.29%|1.20kB / 3.40MB|17", ""),
    )
    s = dockerlib.container_stats("x")
    assert s["cpuPct"] == 12.34
    assert s["memUsed"] == 123 * 1024 * 1024
    assert s["memLimit"] == int(1.9 * 1024 ** 3)
    assert s["pids"] == 17


# ───────────────────────── фейковый RCON-сервер ─────────────────────────

def _pkt(rid, ptype, body: bytes) -> bytes:
    payload = struct.pack("<ii", rid, ptype) + body + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("соединение закрыто")
        buf += chunk
    return buf


def _read_pkt(sock):
    (size,) = struct.unpack("<i", _recv_exact(sock, 4))
    body = _recv_exact(sock, size)
    rid, ptype = struct.unpack("<ii", body[:8])
    return rid, ptype, body[8:-2].decode("utf-8", "replace")


class FakeRcon(threading.Thread):
    """Одноразовый Source RCON сервер: пустые 10-байтовые пакеты авторизации —
    регресс-тест на баг парсинга коротких пакетов."""

    daemon = True

    def __init__(self, password="secret", response="Players connected (0): \n", reject=False):
        super().__init__(daemon=True)
        self.password = password
        self.response = response
        self.reject = reject
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.port = srv.getsockname()[1]
        self._srv = srv

    def run(self):
        self._srv.settimeout(5)
        try:
            conn, _ = self._srv.accept()
        except TimeoutError:
            return
        conn.settimeout(5)
        try:
            rid, ptype, body = _read_pkt(conn)
            if self.reject or (ptype, body) != (3, self.password):
                conn.sendall(_pkt(-1, 2, b""))   # AUTH_RESPONSE с id=-1: отказ
            else:
                # пустой RESPONSE_VALUE + AUTH_RESPONSE — как отвечает PZ
                conn.sendall(_pkt(rid, 0, b""))
                conn.sendall(_pkt(rid, 2, b""))
                rid2, ptype2, cmd = _read_pkt(conn)
                assert ptype2 == 2, "ожидали EXECCOMMAND"
                conn.sendall(_pkt(rid2, 0, self.response.encode()))
        finally:
            conn.close()
            self._srv.close()


import socket  # noqa: E402  (после вспомогательных функций — читается сверху вниз)


def test_rcon_roundtrip():
    srv = FakeRcon()
    srv.start()
    out = rcon.RCON("127.0.0.1", srv.port, "secret", connect_timeout=3).run("players")
    assert "Players connected" in out


def test_rcon_wrong_password():
    srv = FakeRcon(password="right", reject=True)
    srv.start()
    with pytest.raises(rcon.RCONError):
        rcon.RCON("127.0.0.1", srv.port, "wrong", connect_timeout=3).run("players")


# ───────────────────────── настройки ─────────────────────────

def test_settings_validation(tmp_path):
    config.CFG["settings_file"] = str(tmp_path / "settings.json")
    config.CFG["events_file"] = str(tmp_path / "events.jsonl")
    assert ops.patch_settings({"watchdog": {"thresholdMin": 999}}) is None
    assert ops.get_settings()["watchdog"]["thresholdMin"] == 60
    assert ops.patch_settings({"autoUpdate": {"backupBeforeUpdate": False}}) is None
    assert ops.get_settings()["autoUpdate"]["backupBeforeUpdate"] is False
    assert ops.patch_settings({"watchdog": {"enabled": "yes"}}) == "watchdog.enabled должен быть true/false"


# ───────────────────────── история онлайна ─────────────────────────

def test_players_history(tmp_path):
    config.CFG["dashboard_dir"] = str(tmp_path)
    ops._PH = None
    ops.record_players_sample(2)
    ops.record_players_sample(7)          # троттлинг: второй семпл в тот же интервал игнорируется
    assert len(ops._PH) == 1 and ops._PH[0]["count"] == 2

    # уводим семпл на 25 часов назад: новый добавится, старый выпадет из окна 24 ч
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).astimezone().isoformat(timespec="seconds")
    ops._PH[0]["ts"] = old
    ops.record_players_sample(4)
    assert len(ops._PH) == 1 and ops._PH[0]["count"] == 4
    assert ops.get_players_history()[-1]["count"] == 4


def test_history_persisted(tmp_path):
    config.CFG["dashboard_dir"] = str(tmp_path)
    ops._PH = None
    ops.record_players_sample(5)
    ops._PH = None                        # имитация перезапуска: загрузка из файла
    assert ops.get_players_history()[0]["count"] == 5
    assert json.loads((tmp_path / "players-history.json").read_text())[0]["count"] == 5
