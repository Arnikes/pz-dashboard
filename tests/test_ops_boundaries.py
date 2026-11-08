"""Damaged optional persistence must not interrupt monitoring or startup."""

import json
from collections import deque
from copy import deepcopy
from pathlib import Path

import pytest

import config
import ops


NESTED_JSON = "[" * 12_000 + "0" + "]" * 12_000


@pytest.fixture
def persistence(tmp_path, monkeypatch):
    cfg = {
        **config.CFG,
        "dashboard_dir": str(tmp_path),
        "settings_file": str(tmp_path / "settings.json"),
        "events_file": str(tmp_path / "events.jsonl"),
    }
    monkeypatch.setattr(config, "CFG", cfg)
    monkeypatch.setattr(ops, "_SETTINGS", deepcopy(ops._DEFAULTS))
    monkeypatch.setattr(ops, "_EV_MEM", deque(maxlen=300))
    monkeypatch.setattr(ops, "_PH", None)
    return tmp_path


def test_deep_settings_keep_normalized_defaults_and_bootstrap(persistence, monkeypatch):
    path = Path(config.CFG["settings_file"])
    path.write_text(NESTED_JSON, encoding="utf-8")
    monkeypatch.setattr(
        config,
        "telegram_env",
        lambda: {"language": "invalid", "enabled": "invalid", "chatId": "fixture-chat"},
    )

    ops._load_settings()

    expected = deepcopy(ops._DEFAULTS)
    expected["telegram"]["chatId"] = "fixture-chat"
    assert ops._SETTINGS == expected
    assert path.read_text(encoding="utf-8") == NESTED_JSON


def test_deep_event_record_preserves_surrounding_history_and_limits(persistence):
    older = {"ts": "2026-11-08T10:00:00+00:00", "type": "backup", "text": "older"}
    newer = {**older, "text": "newer"}
    path = Path(config.CFG["events_file"])
    path.write_text(
        "\n".join((json.dumps(older), NESTED_JSON, "invalid", json.dumps(newer))) + "\n",
        encoding="utf-8",
    )

    assert ops.get_events(0) == []
    assert ops.get_events(1) == [newer]
    assert ops.get_events(10) == [newer, older]
    ops.log_event("backup", "after recovery", notify=False)
    assert [entry["text"] for entry in ops.get_events(3)] == ["after recovery", "newer", "older"]


def test_deep_backup_record_preserves_order_and_pagination(persistence):
    records = [{"name": name, "status": "ok"} for name in ("oldest", "middle", "newest")]
    Path(ops._journal_path()).write_text(
        "\n".join(
            (
                json.dumps(records[0]),
                NESTED_JSON,
                json.dumps(records[1]),
                "invalid",
                json.dumps(records[2]),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert ops.get_backup_journal(0) == []
    assert ops.get_backup_journal(2) == [records[2], records[1]]
    assert ops.get_backup_journal(1, offset=1) == [records[1]]
    assert ops.get_backup_journal(2, offset=2) == [records[0]]
    assert ops.get_backup_journal(1, offset=3) == []


def test_deep_player_history_recovers_and_accepts_new_samples(persistence, monkeypatch):
    path = Path(ops._ph_file())
    path.write_text(NESTED_JSON, encoding="utf-8")

    assert ops.get_players_history() == []
    ops.record_players_sample(3)
    recovered = ops.get_players_history()
    assert len(recovered) == 1 and recovered[0]["count"] == 3
    assert json.loads(path.read_text(encoding="utf-8")) == recovered
    monkeypatch.setattr(ops, "_PH", None)
    assert ops.get_players_history() == recovered
