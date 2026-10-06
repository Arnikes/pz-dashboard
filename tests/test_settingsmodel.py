"""Settings candidates stay isolated until the operation layer publishes them."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

import config
import ops
import settingsmodel


def test_rejected_late_section_preserves_inputs_and_persisted_state(tmp_path, monkeypatch):
    current = deepcopy(settingsmodel.DEFAULTS)
    patch = {
        "autoUpdate": {"enabled": True, "intervalHours": "9"},
        "telegram": {"botToken": 123},
    }
    original = deepcopy(patch)
    monkeypatch.setattr(ops, "_SETTINGS", current)
    monkeypatch.setattr(ops, "_SETTINGS_VERSION", {"epoch": "test", "revision": 7})
    monkeypatch.setitem(config.CFG, "settings_file", str(tmp_path / "settings.json"))
    save = Mock()
    monkeypatch.setattr(ops, "_save_settings", save)

    assert ops.patch_settings(patch) == "botToken должен быть строкой"
    assert current == settingsmodel.DEFAULTS
    assert patch == original
    assert ops._SETTINGS_VERSION["revision"] == 7
    save.assert_not_called()


def test_candidate_can_be_changed_without_touching_inputs():
    current = deepcopy(settingsmodel.DEFAULTS)
    current["telegram"]["botToken"] = "saved-token"
    patch = {"telegram": {"botToken": "•••oken", "groups": {"backup": False}}}
    original = deepcopy(patch)
    candidate, error = settingsmodel.prepare_patch(current, patch, Mock())

    assert error is None
    assert candidate["telegram"]["botToken"] == "saved-token"
    assert candidate["telegram"]["groups"] == {"backup": False}
    candidate["autoUpdate"]["enabled"] = True
    candidate["telegram"]["groups"]["backup"] = True
    assert current["autoUpdate"]["enabled"] is False
    assert current["telegram"]["groups"]["backup"] is True
    assert patch == original


@pytest.mark.parametrize(
    "patch, expected, calls",
    [
        ({"autoBackup": {"enabled": True, "time": " 3:05 "}}, 12345, ["03:05"]),
        ({"autoBackup": {"enabled": False}}, None, []),
        ({"autoBackup": {"stopServer": True}}, 999, []),
        ({"autoUpdate": {"enabled": True}}, 999, []),
    ],
)
def test_only_schedule_changes_recalculate_the_next_backup(patch, expected, calls):
    current = deepcopy(settingsmodel.DEFAULTS)
    current["nextBackupRun"] = 999
    next_run = Mock(return_value=12345)
    candidate, error = settingsmodel.prepare_patch(current, patch, next_run)

    assert error is None
    assert candidate["nextBackupRun"] == expected
    assert [call.args[0] for call in next_run.call_args_list] == calls
    assert current["nextBackupRun"] == 999


def test_loaded_values_and_api_values_keep_their_distinct_compatibility_rules():
    current = deepcopy(settingsmodel.DEFAULTS)
    data = {"autoUpdate": {"intervalHours": "9"}}
    settingsmodel.merge_loaded(current, deepcopy(data))
    settingsmodel.normalize_loaded(current)
    assert current["autoUpdate"]["intervalHours"] == 6
    candidate, error = settingsmodel.prepare_patch(current, data, Mock())
    assert error is None
    assert candidate["autoUpdate"]["intervalHours"] == 9


@pytest.mark.parametrize(
    "patch, error",
    [
        (
            {"autoUpdate": {"enabled": "yes", "intervalHours": []}},
            "enabled должен быть true/false",
        ),
        (
            {"autoUpdate": {"intervalHours": [], "backupBeforeUpdate": "yes"}},
            "intervalHours должен быть числом 1–168",
        ),
        (
            {"watchdog": {"enabled": "yes"}, "autoUpdate": {"enabled": "yes"}},
            "enabled должен быть true/false",
        ),
    ],
)
def test_multiple_invalid_fields_preserve_the_first_api_error(patch, error):
    candidate, actual = settingsmodel.prepare_patch(settingsmodel.DEFAULTS, patch, Mock())
    assert candidate is None
    assert actual == error


@pytest.mark.parametrize("value, expected", [(0, 0), (12, 12), (99, 60), (-1, 0), ("7", 7)])
def test_watchdog_grace_setting_is_validated_and_persisted(tmp_path, monkeypatch, value, expected):
    monkeypatch.setattr(ops, "_SETTINGS", deepcopy(settingsmodel.DEFAULTS))
    monkeypatch.setitem(config.CFG, "settings_file", str(tmp_path / "settings.json"))
    assert ops.patch_settings({"watchdog": {"gracePeriodMin": value}}) is None
    assert ops.get_settings()["watchdog"]["gracePeriodMin"] == expected
    monkeypatch.setattr(ops, "_SETTINGS", deepcopy(settingsmodel.DEFAULTS))
    ops._load_settings()
    assert ops.get_settings()["watchdog"]["gracePeriodMin"] == expected


@pytest.mark.parametrize("value", [True, None, [], "bad"])
def test_invalid_grace_setting_is_rejected(value):
    candidate, error = settingsmodel.prepare_patch(
        settingsmodel.DEFAULTS, {"watchdog": {"gracePeriodMin": value}}, Mock()
    )
    assert candidate is None
    assert error == "gracePeriodMin должен быть числом 0–60"


@pytest.mark.parametrize("loaded", [{}, {"gracePeriodMin": "bad"}, {"gracePeriodMin": True}])
def test_legacy_or_invalid_loaded_grace_uses_five_minutes(loaded):
    current = deepcopy(settingsmodel.DEFAULTS)
    settingsmodel.merge_loaded(current, {"watchdog": loaded})
    settingsmodel.normalize_loaded(current)
    assert current["watchdog"]["gracePeriodMin"] == 5
