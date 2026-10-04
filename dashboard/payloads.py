"""Response data shared by the JSON API and SSE, independent of HTTP I/O."""

import json
import threading
import time

import config
import dockerlib
import ops
import rcon


def players_payload():
    if ops.docker_ok_cached():
        st = ops.container_state()
        if not st:
            return {"ok": False, "error": "Контейнер не найден"}
        if not st["running"]:
            return {"ok": False, "error": "Сервер остановлен"}
    # В удалённом режиме доступность сервера определяется самим RCON.
    try:
        data = ops.fetch_players()
        return {"ok": True, **data}
    except rcon.RCONError as e:
        return {"ok": False, "error": str(e)}


def logs_payload(tail=None, since=None, until=None):
    limit = config.CFG["log_lines"] if tail is None else tail
    scope = {key: value for key, value in (("since", since), ("until", until)) if value}
    text, err = dockerlib.container_logs(config.CFG["pz_container"], limit, **scope)
    if text is None:
        return {"ok": False, "error": err or "логи недоступны"}
    result = {"ok": True, "text": text}
    if scope:
        result.update(truncated=len(text.splitlines()) >= limit, **scope)
    return result


def stats_payload():
    """Кадр метрик: ошибка контейнера отдаётся как ok:false + error, иначе
    интерфейс рисует фиктивные нули вместо состояния ошибки."""
    data = ops.fetch_stats()
    return {"ok": "error" not in data, **data}


def backups_payload():
    s = ops.get_settings()
    return {
        "ok": True,
        "items": ops.list_backups(),
        "maxBackups": s["backup"]["maxBackups"],
        "autoBackup": ops.auto_backup_state(),
        "journal": ops.get_backup_journal(30),
    }


def events_payload(limit=100):
    return {"ok": True, "items": ops.get_events(limit)}


# каналы SSE: имя события → интервал отправки, секунды
STREAM_PLAN = (
    ("ops", 1.0),
    ("overview", 3.0),
    ("players", 5.0),
    ("stats", 5.0),
    ("logs", 5.0),
    ("backups", 10.0),
    ("events", 12.0),
    ("stats-history", 30.0),
    ("players-history", 60.0),
    ("mods", 60.0),
)


class StreamCache:
    """One bounded, serialized snapshot per channel and HTTP server.

    Per-channel locks coalesce simultaneous subscribers without letting a slow
    Docker/Steam request block unrelated channels. HTTP polling stays uncached.
    """

    def __init__(self):
        self._channels = {name: [threading.Lock(), None, 0.0] for name, _ in STREAM_PLAN}

    def frame(self, name, interval):
        channel = self._channels[name]
        with channel[0]:
            if channel[1] is None or time.monotonic() >= channel[2]:
                try:
                    data = stream_payload(name)
                    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                except Exception as error:  # noqa: BLE001 — share failures too
                    encoded = json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False)
                channel[1] = f"event: {name}\ndata: {encoded}\n\n".encode("utf-8")
                # Start TTL after collection: slow calls must not cause a stampede.
                channel[2] = time.monotonic() + interval
            return channel[1]


def overview_payload():
    return {"ok": True, **ops.overview()}


def operation_payload():
    return {"ok": True, **ops.op_state()}


def stats_history_payload():
    return {"ok": True, "points": ops.get_stats_history()}


def players_history_payload():
    return {"ok": True, "points": ops.get_players_history()}


# Shared providers keep polling and streaming responses consistent.
PAYLOADS = {
    "overview": overview_payload,
    "players": players_payload,
    "stats": stats_payload,
    "logs": logs_payload,
    "backups": backups_payload,
    "events": events_payload,
    "ops": operation_payload,
    "stats-history": stats_history_payload,
    "players-history": players_history_payload,
    # SSE sends the registry; GET /api/mods also includes editable settings.
    "mods": lambda: ops.list_mods(None),
}

GET_CHANNELS = {
    "/api/overview": "overview",
    "/api/players": "players",
    "/api/players/history": "players-history",
    "/api/stats/history": "stats-history",
    "/api/stats": "stats",
    "/api/logs": "logs",
    "/api/backups": "backups",
    "/api/ops": "ops",
}


def stream_payload(name):
    provider = PAYLOADS.get(name)
    if provider is None:
        return {"ok": False, "error": "нет такого потока"}
    return provider()
