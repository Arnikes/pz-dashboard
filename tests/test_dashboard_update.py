"""Console replacement must survive its own restart without touching the game."""

import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import actions
import config
import dashboardupdate as updater
import dockerlib
import ops

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64
IMAGE = "ghcr.io/example/console:latest"


@pytest.fixture
def deployment(monkeypatch, tmp_path):
    monkeypatch.setitem(config.CFG, "dashboard_dir", str(tmp_path))
    monkeypatch.setitem(config.CFG, "compose_file", "/compose/docker-compose.yml")
    monkeypatch.setattr(updater, "_CHECK", {})
    monkeypatch.setattr(updater, "_CONTAINER", {"at": 0, "name": None, "value": None})
    container = {
        "Id": "console-id",
        "Image": OLD,
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "Config": {
            "Image": IMAGE,
            "Labels": {
                "com.docker.compose.project": "pz",
                "com.docker.compose.service": "pz-dashboard",
            },
        },
        "Mounts": [
            {"Type": "bind", "Destination": "/compose", "Source": "/srv/pz", "RW": False},
            {
                "Type": "volume",
                "Destination": str(tmp_path),
                "Source": "/volumes/console",
                "RW": True,
            },
        ],
    }
    model = {
        "name": "pz",
        "services": {
            "pz-dashboard": {
                "image": IMAGE,
                "container_name": "pz-dashboard",
                "environment": {"TOKEN": "fictional-secret"},
                "volumes": [{"type": "bind", "source": "/compose", "target": "/compose"}],
            },
            "pzserver": {"image": "example/game:latest"},
        },
    }
    monkeypatch.setattr(updater, "inspect", lambda name: copy.deepcopy(container))
    monkeypatch.setattr(dockerlib, "image_digests", lambda image: OLD)
    monkeypatch.setattr(dockerlib, "compose_version", lambda: True)
    monkeypatch.setattr(dockerlib, "image_pull", Mock(return_value=(0, "", "")))

    def command(args, **kwargs):
        if "config" in args:
            return 0, json.dumps(model), ""
        if "image" in args and "--format" in args:
            return 0, NEW, ""
        return 0, "helper-id", ""

    sh = Mock(side_effect=command)
    monkeypatch.setattr(dockerlib, "sh", sh)
    return container, model, sh


def test_detached_handoff_preserves_host_paths_and_secrets(deployment):
    _, _, command = deployment
    assert updater.apply(Mock()) == "handoff"
    launch = command.call_args.args[0]
    assert launch[:5] == ["docker", "run", "--detach", "--rm", "--network"]
    assert launch[launch.index("--volumes-from") + 1] == "console-id"
    assert OLD in launch
    assert "fictional-secret" not in " ".join(launch)
    snapshot = Path(launch[launch.index("/app/dashboardupdate.py") + 1])
    frozen = json.loads(snapshot.read_text())
    assert frozen["services"]["pz-dashboard"]["volumes"][0]["source"] == "/srv/pz"
    assert frozen["services"]["pz-dashboard"]["environment"]["TOKEN"] == "fictional-secret"
    assert ops.op_busy()
    assert ops.op_state()["active"]["op"] == "apply-dashboard-update"


@pytest.mark.parametrize("failure", ["pull", "helper", "config", "changed-image", "missing-volume"])
def test_preflight_and_launch_failures_leave_console_running(deployment, monkeypatch, failure):
    container, model, command = deployment
    original = command.side_effect
    if failure == "changed-image":
        model["services"]["pz-dashboard"]["image"] = "example/other:latest"
    elif failure == "missing-volume":
        container["Mounts"].pop()
    elif failure == "pull":
        dockerlib.image_pull.return_value = (1, "", "fictional-secret")
    elif failure in {"helper", "config"}:
        command.side_effect = lambda args, **kw: (
            (1, "", "fictional-secret")
            if ("run" if failure == "helper" else "config") in args
            else original(args, **kw)
        )
    with pytest.raises(ops.OpsError) as error:
        updater.apply(Mock())
    assert "fictional-secret" not in str(error.value)
    assert not ops.op_busy()
    assert not list(Path(config.CFG["dashboard_dir"]).glob("dashboard-compose-*.json"))
    assert all("stop" not in call.args[0] for call in command.call_args_list)


def test_current_image_does_not_restart(deployment):
    _, _, command = deployment
    original = command.side_effect
    command.side_effect = lambda args, **kw: (
        (0, OLD, "") if "image" in args and "--format" in args else original(args, **kw)
    )
    phase = Mock()
    assert updater.apply(phase) is None
    phase.assert_called_with("Готово", "Образ пульта уже актуален")
    assert not ops.op_busy()
    assert all("run" not in call.args[0] for call in command.call_args_list)


@pytest.mark.parametrize("healthy", [True, False])
def test_helper_only_replaces_console_and_persists_outcome(deployment, monkeypatch, healthy):
    container, _, command = deployment
    updater.apply(Mock())
    snapshot = next(Path(config.CFG["dashboard_dir"]).glob("dashboard-compose-*.json"))
    container["Image"] = NEW
    if not healthy:
        container["State"]["Health"]["Status"] = "unhealthy"
    ticks = iter([0, 0, 0, 181])
    monkeypatch.setattr(updater.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(updater.time, "sleep", lambda seconds: None)
    log = Mock()
    monkeypatch.setattr(ops, "log_event", log)
    updater.run_helper(snapshot, "pz-dashboard", "pz-dashboard", "pz", NEW)
    args = command.call_args.args[0]
    assert args[-1] == "pz-dashboard"
    assert "--no-deps" in args and "--no-build" in args
    assert not snapshot.exists()
    assert not ops.op_busy()
    result = ops.op_state()["history"][0]
    assert result["ok"] is healthy
    assert result["finishedAt"]
    log.assert_called_once()


@pytest.mark.parametrize("schema", ["SchemaV2Manifest", "OCIManifest"])
def test_registry_check_selects_running_platform_without_pull(deployment, schema):
    _, _, command = deployment
    manifests = [
        {
            "Descriptor": {"platform": {"os": "linux", "architecture": arch}},
            schema: {"config": {"digest": digest}},
        }
        for arch, digest in [("arm64", OLD), ("amd64", NEW)]
    ]
    command.side_effect = lambda args, **kw: (
        0,
        json.dumps(manifests if "manifest" in args else [{"Os": "linux", "Architecture": "amd64"}]),
        "",
    )
    result = updater.check()
    assert result["available"] is True and result["remote"] == NEW
    dockerlib.image_pull.assert_not_called()
    assert updater.state()["available"] is True


def test_registry_failure_never_claims_image_is_current(deployment):
    deployment[2].return_value = (1, "", "fictional-secret")
    deployment[2].side_effect = None
    result = updater.check()
    assert result["available"] is None and result["error"]
    assert "fictional-secret" not in result["error"]


def test_build_and_digest_deployments_are_unavailable(deployment):
    container, _, _ = deployment
    container["Config"]["Image"] = IMAGE + "@" + OLD
    assert not updater.state()["supported"]
    with pytest.raises(ops.OpsError):
        updater.check()


def test_dispatch_and_busy_lock(deployment, monkeypatch):
    monkeypatch.setattr(ops, "get_settings", lambda: {"autoUpdate": {"warnSeconds": 0}})
    start = Mock()
    monkeypatch.setattr(ops, "start_op", start)
    assert actions.dispatch({"op": "apply-dashboard-update"})["started"] == "apply-dashboard-update"
    start.call_args.args[1]()
    for action in ("apply-dashboard-update", "check-dashboard-update", "restart"):
        with pytest.raises(actions.ActionError) as error:
            actions.dispatch({"op": action})
        assert error.value.status == 409
