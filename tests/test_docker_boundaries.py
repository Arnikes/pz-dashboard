"""Malformed Docker telemetry must not become successful data or history."""

import json

import pytest

import dockerlib
import ops
import payloads


def stats_output(**replacements):
    fields = {
        "cpu": "12.34%",
        "mem_used": "123MiB",
        "mem_limit": "1.9GiB",
        "mem_pct": "6.29%",
        "net_in": "1.20kB",
        "net_out": "3.40MB",
        "pids": "17",
    }
    fields.update(replacements)
    return (
        f"{fields['cpu']}|{fields['mem_used']} / {fields['mem_limit']}|"
        f"{fields['mem_pct']}|{fields['net_in']} / {fields['net_out']}|{fields['pids']}"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        (field, value)
        for field in ("cpu", "mem_pct")
        for value in ("NaN%", "Infinity%", "-Infinity%", "-0.01%", "1e400%", "")
    ]
    + [
        (field, value)
        for field in ("mem_used", "mem_limit", "net_in", "net_out")
        for value in ("unknown", "-1B", "9" * 400 + "GiB", "")
    ]
    + [("pids", value) for value in ("NaN", "Infinity", "-1", "9" * 400, "")],
)
def test_invalid_snapshot_reports_failure_without_recording_history(monkeypatch, field, value):
    monkeypatch.setattr(ops, "_STATS", [])
    monkeypatch.setattr(
        dockerlib,
        "sh",
        lambda *args, **kwargs: (0, stats_output(**{field: value}), ""),
    )

    result = payloads.stats_payload(state_provider=lambda: {"running": True})

    assert result == {"ok": False, "error": "docker stats недоступен"}
    assert ops.get_stats_history() == []
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "fields,expected",
    [
        (
            {"cpu": "250.5%", "mem_pct": "125%"},
            {
                "cpuPct": 250.5,
                "memUsed": 123 * 1024**2,
                "memLimit": int(1.9 * 1024**3),
                "memPct": 125.0,
                "netIn": 1200,
                "netOut": 3_400_000,
                "pids": 17,
            },
        ),
        (
            {
                "cpu": "0%",
                "mem_used": "0B",
                "mem_limit": "0B",
                "mem_pct": "0%",
                "net_in": "0B",
                "net_out": "0B",
                "pids": "0",
            },
            dict.fromkeys(
                ("cpuPct", "memUsed", "memLimit", "memPct", "netIn", "netOut", "pids"), 0
            ),
        ),
    ],
)
def test_valid_snapshot_keeps_units_zeros_and_multicore_cpu(monkeypatch, fields, expected):
    monkeypatch.setattr(ops, "_STATS", [])
    monkeypatch.setattr(dockerlib, "sh", lambda *args, **kwargs: (0, stats_output(**fields), ""))

    result = payloads.stats_payload(state_provider=lambda: {"running": True})

    assert result == {"ok": True, **expected}
    history = ops.get_stats_history()
    assert len(history) == 1
    assert history[0]["cpu"] == expected["cpuPct"]
    assert history[0]["mem"] == expected["memPct"]
    json.dumps({"result": result, "history": history}, allow_nan=False)


@pytest.mark.parametrize("raw", ["мусор", "", None, "1..2B", "-1B", "9" * 400 + "GiB"])
def test_public_byte_parser_keeps_unknown_size_fallback(raw):
    assert dockerlib.parse_bytes(raw) == 0


@pytest.mark.parametrize(
    "inspect_output, expected",
    [
        ("on-failure|3", "on-failure:3"),
        ("on-failure|0", "on-failure"),
        ("on-failure|invalid", None),
        ("on-failure", None),
        ("always|0", "always"),
        ("unless-stopped", "unless-stopped"),
        ("|0", "no"),
        ("", "no"),
        ("unknown|0", None),
    ],
)
def test_restart_policy_preserves_docker_retry_limit(monkeypatch, inspect_output, expected):
    def inspect(args, *, timeout):
        assert args[:4] == ["docker", "inspect", "container-id", "--format"]
        assert ".RestartPolicy.MaximumRetryCount" in args[4]
        return 0, inspect_output, ""

    monkeypatch.setattr(dockerlib, "sh", inspect)
    assert dockerlib.get_restart_policy("container-id") == expected
