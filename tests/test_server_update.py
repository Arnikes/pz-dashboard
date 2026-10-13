"""Game image updates must preserve deployment identity and accepted cancellation."""

import copy
import json
import threading
from collections import deque
from pathlib import Path
from unittest.mock import Mock

import pytest

import actions
import config
import dockerlib
import ops
import serverupdate

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64


@pytest.fixture
def deployment(monkeypatch):
    monkeypatch.setattr(ops, "_LAST_CHECK", {})
    monkeypatch.setitem(config.CFG, "pz_container", "game")
    monkeypatch.setitem(config.CFG, "pz_service", "game")
    monkeypatch.setitem(config.CFG, "dashboard_container", "panel")
    monkeypatch.setitem(config.CFG, "compose_file", "/compose/compose.yml")
    container = {
        "Id": "container-1",
        "Image": OLD,
        "State": {"Running": True, "StartedAt": "S1"},
        "Config": {
            "Image": "registry.example:5000/pz/game:latest",
            "Labels": {
                "com.docker.compose.project": "production",
                "com.docker.compose.service": "game",
            },
        },
        "Mounts": [{"Type": "bind", "Source": "/srv/pz/data", "Destination": "/data"}],
    }
    panel = {"Mounts": [{"Type": "bind", "Source": "/srv/pz", "Destination": "/compose"}]}
    model = {
        "name": "wrong-default",
        "services": {
            "game": {
                "image": container["Config"]["Image"],
                "container_name": "game",
                "environment": {"PASSWORD": "literal$secret"},
                "volumes": [{"type": "bind", "source": "/compose/data", "target": "/data"}],
            },
            "panel": {"image": "panel:latest"},
        },
    }
    monkeypatch.setattr(
        ops.dashboardupdate,
        "inspect",
        lambda name: copy.deepcopy(container if name == "game" else panel),
    )
    monkeypatch.setattr(
        dockerlib, "sh", Mock(side_effect=lambda args, **kw: (0, json.dumps(model), ""))
    )
    return container, model


def test_prepare_preserves_project_and_maps_world_mount(deployment):
    container, model = serverupdate.prepare()
    assert model["name"] == "production"
    assert model["services"]["game"]["volumes"][0]["source"] == "/srv/pz/data"
    assert container["Image"] == OLD


@pytest.mark.parametrize("changed", [False, True])
def test_prepare_preserves_named_world_volume(deployment, changed):
    container, model = deployment
    container["Mounts"] = [{"Type": "volume", "Name": "production_world", "Destination": "/data"}]
    model["volumes"] = {"world": {"name": "wrong_world" if changed else "production_world"}}
    model["services"]["game"]["volumes"] = [
        {"type": "volume", "source": "world", "target": "/data"}
    ]
    if changed:
        with pytest.raises(ops.OpsError, match="Пути данных"):
            serverupdate.prepare()
    else:
        assert serverupdate.prepare()[1]["volumes"]["world"]["name"] == "production_world"


@pytest.mark.parametrize(
    "change,match",
    [
        ("image", "Образ сервера"),
        ("build", "без build"),
        ("mount", "Пути данных"),
        ("name", "Имя контейнера"),
        ("service", "сервису Compose"),
    ],
)
def test_prepare_rejects_mismatched_deployment_before_pull(deployment, change, match, monkeypatch):
    container, model = deployment
    definition = model["services"]["game"]
    if change == "image":
        definition["image"] = "default/image:latest"
    elif change == "build":
        definition["build"] = {"context": "/source"}
    elif change == "mount":
        definition["volumes"][0]["source"] = "/wrong/world"
    elif change == "name":
        definition["container_name"] = "wrong"
    else:
        container["Config"]["Labels"]["com.docker.compose.service"] = "wrong"
    monkeypatch.setattr(dockerlib, "image_pull", Mock())
    monkeypatch.setattr(dockerlib, "compose_version", lambda: True)
    monkeypatch.setattr(ops, "graceful_stop", Mock())
    with pytest.raises(ops.OpsError, match=match):
        ops._do_apply_update(0)
    dockerlib.image_pull.assert_not_called()
    ops.graceful_stop.assert_not_called()


def test_recreate_freezes_secrets_and_recreates_only_game(deployment, monkeypatch):
    _, model = serverupdate.prepare()
    captured = {}

    def command(args, **kwargs):
        path = Path(args[args.index("-f") + 1])
        captured.update(path=path, model=json.loads(path.read_text(encoding="utf-8")), args=args)
        return 0, "", ""

    monkeypatch.setattr(dockerlib, "sh", command)
    serverupdate.recreate(model)
    assert captured["model"]["services"]["game"]["environment"]["PASSWORD"] == "literal$$secret"
    assert captured["args"][-8:] == [
        "up",
        "-d",
        "--no-deps",
        "--no-build",
        "--pull",
        "never",
        "--force-recreate",
        "game",
    ]
    assert not captured["path"].exists()


@pytest.fixture
def update_env(deployment, monkeypatch):
    container, _ = deployment
    monkeypatch.setattr(
        ops, "_ACTIVE", {"op": "apply-update", "cancellable": True, "cancelRequested": False}
    )
    monkeypatch.setattr(ops, "_OP_CANCEL", threading.Event())
    monkeypatch.setattr(ops, "_OP_HISTORY", deque())
    monkeypatch.setattr(ops, "log_event", Mock())
    monkeypatch.setattr(ops, "_begin_watchdog_grace", Mock())
    monkeypatch.setattr(ops, "check_update", Mock())
    monkeypatch.setattr(ops, "rcon", Mock(return_value=""))
    monkeypatch.setattr(ops, "get_settings", lambda: {"autoUpdate": {"backupBeforeUpdate": False}})
    monkeypatch.setattr(dockerlib, "compose_version", lambda: True)
    monkeypatch.setattr(dockerlib, "image_pull", Mock(return_value=(0, "", "")))
    monkeypatch.setattr(serverupdate, "image_id", Mock(return_value=NEW))
    monkeypatch.setattr(ops, "graceful_stop", Mock(return_value="stopped"))
    monkeypatch.setattr(ops, "wait_until_running", lambda timeout: True)
    monkeypatch.setattr(ops, "wait_until_ready", Mock(return_value=True))

    def recreate(model, snapshot):
        container["Image"] = NEW
        return 0, "", ""

    monkeypatch.setattr(serverupdate, "recreate", Mock(side_effect=recreate))
    return container


def test_update_verifies_pulled_image_and_readiness(update_env):
    ops._do_apply_update(0)
    serverupdate.recreate.assert_called_once()
    ops.wait_until_ready.assert_called_once()
    assert ops._ACTIVE["message"] == "Сервер обновлён и запущен"


def test_old_image_after_recreation_is_failure(update_env):
    serverupdate.recreate.side_effect = None
    serverupdate.recreate.return_value = (0, "", "")
    with pytest.raises(ops.OpsError, match="не подтверждено"):
        ops._do_apply_update(0)
    ops.wait_until_ready.assert_not_called()
    ops.check_update.assert_not_called()


def test_no_restart_when_immutable_image_is_current(update_env):
    serverupdate.image_id.return_value = OLD
    ops._do_apply_update(0)
    ops.graceful_stop.assert_not_called()
    serverupdate.recreate.assert_not_called()


def test_snapshot_write_failure_preserves_running_server(update_env, monkeypatch):
    monkeypatch.setattr(serverupdate.Path, "write_text", Mock(side_effect=OSError("Disk full")))
    with pytest.raises(OSError, match="Disk full"):
        ops._do_apply_update(0)
    ops.graceful_stop.assert_not_called()
    serverupdate.recreate.assert_not_called()


@pytest.mark.parametrize("changed", ["container", "image"])
def test_concurrent_replacement_aborts_before_stop(update_env, changed):
    def pull(image):
        if changed == "container":
            update_env["Id"] = "replacement"
        else:
            serverupdate.image_id.side_effect = [NEW, OLD]
        return 0, "", ""

    dockerlib.image_pull.side_effect = pull
    with pytest.raises(ops.OpsError, match="изменился"):
        ops._do_apply_update(0)
    ops.graceful_stop.assert_not_called()


def test_overview_keeps_comparable_digests_and_invalidates_replaced_image(deployment, monkeypatch):
    container, _ = deployment
    monkeypatch.setattr(dockerlib, "registry_digests", lambda *args: (OLD, NEW))
    ops.check_update()
    monkeypatch.setattr(ops, "docker_ok_cached", lambda: False)
    monkeypatch.setattr(ops, "count_backups", lambda: 0)
    monkeypatch.setattr(ops, "local_digest_cached", Mock(return_value="index-digest"))
    state = {
        "image": container["Config"]["Image"],
        "imageId": OLD,
        "status": "running",
        "running": True,
        "startedAt": "",
    }
    result = ops.overview(state_provider=lambda: state)["update"]
    assert result["local"] == OLD and result["remote"] == NEW
    ops.local_digest_cached.assert_not_called()
    state["imageId"] = NEW
    result = ops.overview(state_provider=lambda: state)["update"]
    assert result["available"] is None and result["remote"] is None


@pytest.mark.parametrize("stage", ["before", "pull", "backup", "countdown"])
def test_accepted_cancel_prevents_stop_at_every_pre_stop_stage(update_env, monkeypatch, stage):
    if stage == "before":
        ops.cancel_image_update()
    elif stage == "pull":
        dockerlib.image_pull.side_effect = lambda image: (ops.cancel_image_update() or 0, "", "")
    elif stage == "backup":
        monkeypatch.setattr(
            ops, "get_settings", lambda: {"autoUpdate": {"backupBeforeUpdate": True}}
        )

        def backup(stop):
            ops.cancel_image_update()
            return {"name": "backup", "size": 1}

        monkeypatch.setattr(ops, "_do_backup", backup)
    else:
        monkeypatch.setattr(
            ops, "rcon_warn_broadcast", lambda *args, **kwargs: ops.cancel_image_update()
        )
    assert ops._do_apply_update(120) == "cancelled"
    ops.graceful_stop.assert_not_called()
    serverupdate.recreate.assert_not_called()
    assert ops._ACTIVE["phase"] == "Отменено"
    if stage == "countdown":
        assert "cancelled" in ops.rcon.call_args.args[0]


def test_countdown_cancel_wakes_worker_and_records_history(update_env, monkeypatch):
    warned = threading.Event()
    done = threading.Event()
    monkeypatch.setattr(ops, "rcon", lambda *args, **kw: warned.set() or "")
    monkeypatch.setitem(ops._ACTIVE, "op", None)

    def run():
        try:
            return ops._do_apply_update(600)
        finally:
            done.set()

    ops.start_op("apply-update", run)
    try:
        assert warned.wait(3)
        ops.cancel_image_update()
        assert done.wait(3)
        # Wait for the worker's finally as well as the operation body.
        for thread in threading.enumerate():
            if thread.name == "op-apply-update":
                thread.join(3)
        assert ops.op_state()["history"][0]["cancelled"] is True
        assert ops.op_state()["active"] is None
        ops.graceful_stop.assert_not_called()
    finally:
        ops._OP_CANCEL.set()


def test_cancel_rejected_after_atomic_transition_to_stop(update_env):
    def stop(hook):
        with pytest.raises(ops.OpsError, match="уже останавливается"):
            ops.cancel_image_update()
        return "stopped"

    ops.graceful_stop.side_effect = stop
    ops._do_apply_update(0)


@pytest.mark.parametrize("operation", [None, "restart", "mods-restart"])
def test_cancel_action_rejects_unrelated_operation(update_env, operation):
    ops._ACTIVE["op"] = operation
    with pytest.raises(actions.ActionError) as error:
        actions.dispatch({"op": "cancel-update"})
    assert error.value.status == 409
    assert not ops._OP_CANCEL.is_set()


def test_cancel_action_accepts_busy_update_and_is_idempotent(update_env):
    assert actions.dispatch({"op": "cancel-update"})["cancelRequested"]
    assert actions.dispatch({"op": "cancel-update"})["cancelRequested"]


@pytest.mark.parametrize("containerd", [False, True])
def test_check_uses_real_registry_and_same_digest_type(deployment, monkeypatch, containerd):
    container, _ = deployment
    if containerd:
        container["ImageManifestDescriptor"] = {
            "digest": OLD,
            "platform": {"os": "linux", "architecture": "amd64"},
        }
    manifest = {
        "Descriptor": {"digest": NEW, "platform": {"os": "linux", "architecture": "amd64"}},
        "OCIManifest": {"config": {"digest": NEW}},
    }

    def command(args, **kwargs):
        return (
            0,
            json.dumps(
                manifest if "manifest" in args else [{"Os": "linux", "Architecture": "amd64"}]
            ),
            "",
        )

    monkeypatch.setattr(dockerlib, "sh", Mock(side_effect=command))
    result = ops.check_update()
    assert result["available"] is True
    assert result["local"] == OLD and result["remote"] == NEW
    dockerlib.sh.assert_any_call(
        ["docker", "manifest", "inspect", "--verbose", container["Config"]["Image"]], timeout=25
    )


def test_registry_failure_is_unknown_and_does_not_leak_credentials(deployment, monkeypatch):
    monkeypatch.setattr(dockerlib, "sh", lambda *args, **kw: (1, "", "secret-token"))
    result = ops.check_update()
    assert result["available"] is None
    assert "secret-token" not in result["error"]
