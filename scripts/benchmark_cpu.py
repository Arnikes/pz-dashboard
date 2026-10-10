"""Compare SSE localization CPU cost with the previous subscriber-level work.

Synthetic snapshots and an advancing clock isolate Python CPU work; this does not
measure Docker, browsers, game-server load, or production CPU percentages.
"""

import gc
import json
import statistics
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))

import i18n  # noqa: E402
import dockerlib  # noqa: E402
import ops  # noqa: E402
import payloads  # noqa: E402

SNAPSHOTS = 40
REPEATS = 3
DATA = {
    "ok": True,
    "items": [
        {
            "ts": "2026-10-06T12:00:00Z",
            "type": "backup",
            "text": f"Бэкап создан (manual): world-{index}.tar.gz (12.5 МБ)",
        }
        for index in range(100)
    ],
}


class PreviousStreamCache:
    """Previous per-locale serialized cache, retained only for this comparison."""

    def __init__(self):
        self.channels = {
            (name, language): [threading.Lock(), None, 0.0]
            for name, _ in payloads.STREAM_PLAN
            for language in ("en", "ru")
        }

    def frame(self, name, interval):
        channel = self.channels[name, i18n.language()]
        with channel[0]:
            if channel[1] is None or time.monotonic() >= channel[2]:
                encoded = json.dumps(
                    payloads.stream_payload(name), ensure_ascii=False, separators=(",", ":")
                )
                channel[1] = f"event: {name}\ndata: {encoded}\n\n".encode("utf-8")
                channel[2] = time.monotonic() + interval
            return channel[1]


def previous_stream_frame(frame):
    """Historical subscriber-local translation, kept outside runtime code."""
    if i18n.language() != "en" and not i18n.RU_CATALOG:
        return frame
    event, payload = frame.decode("utf-8").split("\ndata: ", 1)
    translated = i18n.present(json.loads(payload))
    return (
        event
        + "\ndata: "
        + json.dumps(translated, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    ).encode("utf-8")


def snapshots(subscribers, *, optimized):
    clock = [0.0]
    cache = payloads.StreamCache() if optimized else PreviousStreamCache()
    size, last = 0, None
    with (
        patch.object(payloads, "stream_payload", lambda _name, **_kwargs: DATA),
        patch.object(payloads.time, "monotonic", lambda: clock[0]),
    ):
        for _ in range(SNAPSHOTS):
            for _ in range(subscribers):
                last = cache.frame("events", 12)
                if not optimized:
                    # Previously every subscriber parsed/localized/encoded
                    # the raw frame after retrieving it from the shared cache.
                    last = previous_stream_frame(last)
                size += len(last)
            clock[0] += 13
    return size, last


def previous(subscribers):
    return snapshots(subscribers, optimized=False)


def current(subscribers):
    return snapshots(subscribers, optimized=True)


def measure(function, subscribers):
    samples = []
    for _ in range(REPEATS):
        gc.collect()
        started, iterations = time.process_time(), 0
        # Windows process CPU timers can have coarse resolution. Accumulate
        # enough work to avoid reporting zero or one-tick measurements.
        while time.process_time() - started < 0.25:
            result = function(subscribers)
            iterations += 1
        samples.append((time.process_time() - started) / iterations)
    return result, statistics.median(samples)


def monitoring_calls(locales, optimized):
    """Sixty synthetic seconds, eight clients; no Docker/RCON is contacted."""
    clock = [0.0]
    state = Mock(
        return_value={"status": "running", "running": True, "startedAt": "", "image": "pz"}
    )
    stats = Mock(return_value={"cpuPct": 12, "memPct": 30})
    players = Mock(return_value={"names": ["Player"], "count": 1})
    logs = Mock(return_value=("INFO server ready", None))
    cache = payloads.StreamCache() if optimized else PreviousStreamCache()
    with ExitStack() as stack:
        for target, name, value in (
            (payloads.time, "monotonic", lambda: clock[0]),
            (ops, "container_state", state),
            (dockerlib, "container_stats", stats),
            (dockerlib, "container_logs", logs),
            (ops, "fetch_players", players),
            (ops, "docker_ok_cached", lambda: True),
            (ops, "compose_ok_cached", lambda: True),
            (ops, "local_digest_cached", lambda **_kwargs: None),
            (ops, "count_backups", lambda: 0),
            (ops, "record_stats_sample", lambda _stats: None),
        ):
            stack.enter_context(patch.object(target, name, value))
        for second in range(60):
            clock[0] = float(second)
            for subscriber in range(8):
                locale = locales[subscriber % len(locales)]
                token = i18n.LANGUAGE.set(locale)
                try:
                    for name, interval in (
                        ("overview", 3),
                        ("players", 5),
                        ("stats", 5),
                        ("logs", 5),
                    ):
                        if optimized:
                            if name != "logs":  # Browser's non-console stream excludes logs.
                                cache.frame(name, interval)
                        else:
                            cache.frame(name, interval)
                finally:
                    i18n.LANGUAGE.reset(token)
    return {
        "docker_inspect": state.call_count,
        "docker_stats": stats.call_count,
        "docker_logs": logs.call_count,
        "docker_cli_total": state.call_count + stats.call_count + logs.call_count,
        "rcon_players": players.call_count,
    }


def main():
    results = {"snapshots": SNAPSHOTS, "events_per_snapshot": 100, "median_of_runs": REPEATS}
    results["monitoring_calls_per_minute_outside_console"] = {
        "eight_ru_clients": {
            "before": monitoring_calls(("ru",), False),
            "after": monitoring_calls(("ru",), True),
        },
        "eight_mixed_language_clients": {
            "before": monitoring_calls(("ru", "en"), False),
            "after": monitoring_calls(("ru", "en"), True),
        },
    }
    for locale in ("ru", "en"):
        token = i18n.LANGUAGE.set(locale)
        try:
            for subscribers in (1, 8):
                # Warm imports/translation regexes before collecting CPU samples.
                assert previous(subscribers) == current(subscribers)
                old_result, before = measure(previous, subscribers)
                new_result, after = measure(current, subscribers)
                assert old_result == new_result
                results[f"{locale}_{subscribers}_subscribers"] = {
                    "before_cpu_seconds": round(before, 5),
                    "after_cpu_seconds": round(after, 5),
                    "cpu_reduction_pct": round(100 * (1 - after / before), 1),
                    "presentation_passes_before": SNAPSHOTS * subscribers,
                    "presentation_passes_after": SNAPSHOTS,
                    "output_bytes": new_result[0],
                }
        finally:
            i18n.LANGUAGE.reset(token)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
