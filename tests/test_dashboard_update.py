"""Console replacement must survive its own restart without touching the game."""

import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import actions
import app
import config
import dashboardupdate as updater
import dockerlib
import ops

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64
IMAGE = "ghcr.io/example/console:latest"


@pytest.fixture
def startup(monkeypatch, tmp_path, authenticated_admin):
    monkeypatch.setitem(config.CFG, "backup_dir", str(tmp_path / "backups"))
    monkeypatch.setitem(config.CFG, "dashboard_dir", str(tmp_path / "dashboard"))
    monkeypatch.setattr(app.auth.Auth, "from_env", lambda: authenticated_admin[0])
    monkeypatch.setattr(ops, "_load_settings", Mock())
    monkeypatch.setattr(ops, "log_event", Mock())
    monkeypatch.setattr(ops, "start_scheduler", Mock())
    monkeypatch.setattr(ops, "start_watchdog", Mock())
    monkeypatch.setattr(ops, "rcon", Mock())
    monkeypatch.setattr(ops, "check_update", Mock())
    monkeypatch.setattr(app.notify, "start_worker", Mock())
    monkeypatch.setattr(dockerlib, "docker_version", lambda: True)
    monkeypatch.setattr(dockerlib, "compose_version", lambda: True)
    monkeypatch.setattr(app.time, "sleep", Mock())
    monkeypatch.setattr(app, "ThreadingHTTPServer", Mock())
    threads = {}

    def thread(*, target, daemon, name):
        assert daemon
        threads[name] = target
        return Mock()

    monkeypatch.setattr(app.threading, "Thread", thread)
    return threads


@pytest.mark.parametrize("available", [True, False, None])
def test_startup_populates_console_check_without_manual_action(
    startup, deployment, monkeypatch, available
):
    command = deployment[2]
    command.side_effect = lambda args, **kw: (
        1 if available is None else 0,
        json.dumps(
            {"SchemaV2Manifest": {"config": {"digest": NEW if available else OLD}}}
            if "manifest" in args
            else [{"Os": "linux", "Architecture": "amd64"}]
        ),
        "",
    )
    # A failed game-image check must not prevent the console's independent check.
    ops.check_update.side_effect = RuntimeError("game registry unavailable")
    app.main()
    assert not updater.state().get("at")
    startup["pz-startup-check"]()
    startup["pz-dashboard-startup-check"]()
    result = updater.state()
    assert result["at"] and result["available"] is available
    assert bool(result["error"]) is (available is None)
    ops.check_update.assert_called_once_with(force_event=True)
    dockerlib.image_pull.assert_not_called()
    assert not updater.operation()
    assert all("run" not in call.args[0] for call in command.call_args_list)


def test_startup_skips_unsupported_console_image(startup, deployment, monkeypatch):
    deployment[0]["Config"]["Image"] = IMAGE + "@" + OLD
    check = Mock()
    monkeypatch.setattr(updater, "check", check)
    app.main()
    startup["pz-dashboard-startup-check"]()
    check.assert_not_called()
    assert all(call.args[0] != "error" for call in ops.log_event.call_args_list)


def test_startup_skips_console_check_without_docker(startup, monkeypatch):
    monkeypatch.setattr(dockerlib, "docker_version", lambda: False)
    app.main()
    assert "pz-startup-check" in startup
    assert "pz-dashboard-startup-check" not in startup


def test_startup_console_failure_is_contained(startup, monkeypatch):
    monkeypatch.setattr(updater, "state", Mock(side_effect=RuntimeError("inspect unavailable")))
    app.main()
    startup["pz-dashboard-startup-check"]()
    ops.log_event.assert_called_with("error", "inspect unavailable")
    startup["pz-startup-check"]()
    ops.check_update.assert_called_once_with(force_event=True)


@pytest.fixture(
    params=[
        IMAGE,
        "gitea.arnike.ru/arnike/pz-console:latest",
        "registry.example:5443/team/console:stable",
    ]
)
def deployment(monkeypatch, tmp_path, request):
    monkeypatch.setitem(config.CFG, "dashboard_dir", str(tmp_path))
    monkeypatch.setitem(config.CFG, "compose_file", "/compose/docker-compose.yml")
    monkeypatch.setattr(updater, "_CHECK", {})
    monkeypatch.setattr(updater, "_CONTAINER", {"at": 0, "name": None, "value": None})
    container = {
        "Id": "console-id",
        "Image": OLD,
        "State": {"Running": True, "Health": {"Status": "healthy"}},
        "Config": {
            "Image": request.param,
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
                "image": request.param,
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
    assert frozen["services"]["pz-dashboard"]["image"] == deployment[0]["Config"]["Image"]
    dockerlib.image_pull.assert_called_once_with(deployment[0]["Config"]["Image"])
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
    command.assert_any_call(
        ["docker", "manifest", "inspect", "--verbose", deployment[0]["Config"]["Image"]],
        timeout=25,
    )
    dockerlib.image_pull.assert_not_called()
    assert updater.state()["available"] is True


@pytest.mark.parametrize("schema", ["SchemaV2Manifest", "OCIManifest"])
@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("multi_platform", [False, True])
def test_containerd_compares_running_manifest_not_index_or_config(
    deployment, schema, changed, multi_platform
):
    container, _, command = deployment
    platform = {"os": "linux", "architecture": "arm64", "variant": "v8"}
    running_manifest = "sha256:" + "c" * 64
    remote_manifest = NEW if changed else running_manifest
    container["ImageManifestDescriptor"] = {"digest": running_manifest, "platform": platform}
    manifests = {
        "Descriptor": {"digest": remote_manifest, "platform": platform},
        schema: {"config": {"digest": "sha256:" + "d" * 64}},
    }
    if multi_platform:
        manifests = [
            {
                "Descriptor": {
                    "digest": OLD,
                    "platform": {"os": "unknown", "architecture": "unknown"},
                },
                schema: {"config": {"digest": OLD}},
            },
            {
                "Descriptor": {"digest": OLD, "platform": {**platform, "variant": "v7"}},
                schema: {"config": {"digest": OLD}},
            },
            manifests,
        ]
    command.side_effect = lambda args, **kw: (
        0,
        json.dumps(
            manifests
            if "manifest" in args
            else [
                {
                    "Id": OLD,
                    "Os": "linux",
                    "Architecture": "amd64",
                    "Descriptor": {
                        "digest": OLD,
                        "mediaType": "application/vnd.oci.image.index.v1+json",
                    },
                }
            ]
        ),
        "",
    )
    result = updater.check()
    assert result["image"] == container["Config"]["Image"]
    assert result["imageId"] == OLD
    assert result["local"] == running_manifest
    assert result["remote"] == remote_manifest
    assert result["available"] is changed
    assert result["error"] is None
    assert updater.state() == result
    command.assert_any_call(["docker", "image", "inspect", OLD], timeout=30)
    dockerlib.image_pull.assert_not_called()


def test_missing_containerd_manifest_leaves_availability_unknown(deployment):
    _, _, command = deployment
    command.side_effect = lambda args, **kw: (
        0,
        json.dumps(
            {"OCIManifest": {"config": {"digest": NEW}}}
            if "manifest" in args
            else [{"Os": "linux", "Architecture": "amd64", "Descriptor": {"digest": OLD}}]
        ),
        "",
    )
    result = updater.check()
    assert result["available"] is None
    assert result["remote"] is None
    assert result["error"]


@pytest.mark.parametrize("change", ["repository", "image-id", "manifest"])
def test_explicit_check_refreshes_replaced_container_and_invalidates_old_result(deployment, change):
    container, _, command = deployment
    command.side_effect = lambda args, **kw: (
        0,
        json.dumps(
            {
                "Descriptor": {"digest": NEW},
                "OCIManifest": {"config": {"digest": NEW}},
            }
            if "manifest" in args
            else [{"Os": "linux", "Architecture": "amd64"}]
        ),
        "",
    )
    assert updater.check()["available"] is True
    if change == "repository":
        container["Config"]["Image"] = "other-registry.example/new/console:latest"
    elif change == "image-id":
        container["Image"] = NEW
    else:
        container["ImageManifestDescriptor"] = {"digest": NEW}
    assert updater.state()["available"] is True  # Passive overview still uses its cache.
    result = updater.check()
    assert result["image"] == container["Config"]["Image"]
    assert result["available"] is (change == "repository")
    command.assert_any_call(
        ["docker", "manifest", "inspect", "--verbose", container["Config"]["Image"]], timeout=25
    )


def test_containerd_handoff_uses_runnable_image_id(deployment):
    container, _, command = deployment
    manifest = "sha256:" + "c" * 64
    container["ImageManifestDescriptor"] = {"digest": manifest}
    assert updater.apply(Mock()) == "handoff"
    launch = command.call_args.args[0]
    assert launch[launch.index("python3") + 1] == OLD
    assert launch[-1] == NEW
    assert manifest not in launch


def test_current_containerd_image_clears_previous_update_notice(deployment):
    container, _, command = deployment
    manifest = "sha256:" + "c" * 64
    container["ImageManifestDescriptor"] = {"digest": manifest}
    current = updater.state()
    updater._CHECK.update(current, remote=NEW, available=True, error=None)
    original = command.side_effect
    command.side_effect = lambda args, **kw: (
        (0, OLD, "") if "image" in args and "--format" in args else original(args, **kw)
    )
    assert updater.apply(Mock()) is None
    result = updater.state()
    assert result["available"] is False
    assert result["local"] == result["remote"] == manifest
    assert all("run" not in call.args[0] for call in command.call_args_list)


def test_image_reference_change_during_apply_aborts_before_pull(deployment, monkeypatch):
    container, _, command = deployment
    changed = copy.deepcopy(container)
    changed["Config"]["Image"] = "other-registry.example/console:latest"
    monkeypatch.setattr(updater, "inspect", Mock(side_effect=[container, changed]))
    with pytest.raises(ops.OpsError, match="Контейнер пульта изменился"):
        updater.apply(Mock())
    dockerlib.image_pull.assert_not_called()
    assert not command.called


def test_containerd_update_then_check_reports_current(deployment, monkeypatch):
    container, _, command = deployment
    container["ImageManifestDescriptor"] = {"digest": "sha256:" + "c" * 64}
    assert updater.apply(Mock()) == "handoff"
    snapshot = next(Path(config.CFG["dashboard_dir"]).glob("dashboard-compose-*.json"))
    container["Image"] = NEW
    running_manifest = "sha256:" + "d" * 64
    container["ImageManifestDescriptor"] = {
        "digest": running_manifest,
        "platform": {"os": "linux", "architecture": "amd64"},
    }
    monkeypatch.setattr(updater.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(ops, "log_event", Mock())
    updater.run_helper(snapshot, "pz-dashboard", "pz-dashboard", "pz", NEW)
    assert updater.operation()["ok"] is True
    command.side_effect = lambda args, **kw: (
        0,
        json.dumps(
            {
                "Descriptor": {"digest": running_manifest},
                "OCIManifest": {"config": {"digest": OLD}},
            }
            if "manifest" in args
            else [{"Os": "linux", "Architecture": "amd64", "Descriptor": {"digest": NEW}}]
        ),
        "",
    )
    result = updater.check()
    assert result["error"] is None
    assert result["available"] is False
    assert result["local"] == result["remote"] == running_manifest


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
