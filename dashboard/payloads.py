"""Response data shared by the JSON API and SSE, independent of HTTP I/O."""

import json
import i18n
import threading
import time

import config
import dockerlib
import ops
import rcon


def players_payload(*, state_provider=None):
    if ops.docker_ok_cached():
        st = (state_provider or ops.container_state)()
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


def stats_payload(*, state_provider=None):
    """Кадр метрик: ошибка контейнера отдаётся как ok:false + error, иначе
    интерфейс рисует фиктивные нули вместо состояния ошибки."""
    data = ops.fetch_stats(state_provider=state_provider) if state_provider else ops.fetch_stats()
    return {"ok": "error" not in data, **data}


def backup_journal_payload(limit=25, offset=0):
    entries = ops.get_backup_journal(limit + 1, offset)
    return {"ok": True, "items": entries[:limit], "hasMore": len(entries) > limit}


def backups_payload():
    s = ops.get_settings()
    journal = backup_journal_payload()
    return {
        "ok": True,
        "items": ops.list_backups(),
        "maxBackups": s["backup"]["maxBackups"],
        "settingsVersion": s.get("version"),
        "autoBackup": ops.auto_backup_state(),
        "journal": journal["items"],
        "journalHasMore": journal["hasMore"],
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
    """Bounded monitoring snapshots and localized frames per HTTP server.

    Locale-neutral collectors are shared across EN/RU subscribers; Workshop
    metadata keeps its language-specific collection. Control actions and direct
    HTTP reads never use the short-lived container inspection snapshot.
    """

    def __init__(self):
        self._channels = {
            (name, language): [threading.Lock(), None, 0.0, {}]
            for name, _ in STREAM_PLAN
            for language in (("en", "ru") if name == "mods" else (None,))
        }
        self._state_lock = threading.Lock()
        self._state = None
        self._state_container = None
        self._state_until = 0.0

    def container_state(self):
        # Only telemetry uses this snapshot. Lifecycle waits, watchdog probes and
        # actions continue calling ops.container_state() directly.
        with self._state_lock:
            container = config.CFG["pz_container"]
            if container != self._state_container or time.monotonic() >= self._state_until:
                self._state = ops.container_state()
                self._state_container = container
                self._state_until = time.monotonic() + 3.0
            return self._state

    def frame(self, name, interval):
        language = i18n.language()
        channel = self._channels[name, language if name == "mods" else None]
        with channel[0]:
            refreshed = channel[1] is None or time.monotonic() >= channel[2]
            if refreshed:
                try:
                    channel[1] = stream_payload(name, telemetry=self)
                except Exception as error:  # noqa: BLE001 — share failures too
                    channel[1] = {"ok": False, "error": str(error)}
                channel[3] = {}
            if language not in channel[3]:
                try:
                    encoded = json.dumps(
                        i18n.present(channel[1]), ensure_ascii=False, separators=(",", ":")
                    )
                except Exception as error:  # noqa: BLE001 — share encoding failures too
                    encoded = json.dumps(
                        i18n.present({"ok": False, "error": str(error)}), ensure_ascii=False
                    )
                channel[3][language] = f"event: {name}\ndata: {encoded}\n\n".encode("utf-8")
            if refreshed:
                # Start TTL after collection/encoding: slow calls must not stampede.
                channel[2] = time.monotonic() + interval
            return channel[3][language]


def overview_payload(*, state_provider=None):
    data = ops.overview(state_provider=state_provider) if state_provider else ops.overview()
    return {"ok": True, **data}


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


def stream_payload(name, *, telemetry=None):
    if telemetry is not None:
        monitoring = {
            "overview": overview_payload,
            "players": players_payload,
            "stats": stats_payload,
        }
        if name in monitoring:
            return monitoring[name](state_provider=telemetry.container_state)
    provider = PAYLOADS.get(name)
    if provider is None:
        return {"ok": False, "error": "нет такого потока"}
    return provider()
