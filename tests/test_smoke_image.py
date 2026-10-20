"""Container smoke checks use one isolated container and preserve failure status."""

import base64
import subprocess
from unittest.mock import Mock

import pytest

from scripts import smoke_image


@pytest.fixture
def docker(monkeypatch):
    create = Mock(return_value="unique-container-id\n")
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    sleep = Mock()
    monkeypatch.setattr(smoke_image.subprocess, "check_output", create)
    monkeypatch.setattr(smoke_image.subprocess, "run", run)
    monkeypatch.setattr(smoke_image.time, "sleep", sleep)
    return create, run, sleep


def test_smoke_uses_exact_image_and_isolated_container(docker):
    create, run, sleep = docker
    smoke_image.smoke_image("sha256:tested")
    command = create.call_args.args[0]
    assert command[:2] == ["docker", "create"]
    assert command[-1] == "sha256:tested"
    assert not any(option in command for option in ("--name", "--network", "-p"))
    key = next(value.split("=", 1)[1] for value in command if value.startswith("PZ_AUTH_KEY="))
    assert len(base64.urlsafe_b64decode(key)) == 32
    assert [call.args[0] for call in run.call_args_list] == [
        ["docker", "start", "unique-container-id"],
        ["docker", "exec", "unique-container-id", "python", "healthcheck.py"],
        ["docker", "logs", "unique-container-id"],
        ["docker", "rm", "-f", "unique-container-id"],
    ]
    sleep.assert_not_called()


@pytest.mark.parametrize("succeeds", [False, True])
def test_health_retries_are_bounded_and_always_clean_up(docker, succeeds):
    _, run, sleep = docker
    statuses = iter([1, 1, 0 if succeeds else 1])

    def execute(command, **_kwargs):
        return subprocess.CompletedProcess(command, next(statuses) if command[1] == "exec" else 0)

    run.side_effect = execute
    if succeeds:
        smoke_image.smoke_image("tested", attempts=3)
    else:
        with pytest.raises(RuntimeError, match="after 3 attempts"):
            smoke_image.smoke_image("tested", attempts=3)
    assert sleep.call_count == 2
    assert run.call_args.args[0] == ["docker", "rm", "-f", "unique-container-id"]


def test_failed_start_keeps_error_even_if_diagnostics_fail(docker):
    _, run, sleep = docker
    error = subprocess.CalledProcessError(9, ["docker", "start"])
    run.side_effect = [error, OSError("logs unavailable"), subprocess.CompletedProcess([], 0)]
    with pytest.raises(subprocess.CalledProcessError) as caught:
        smoke_image.smoke_image("tested")
    assert caught.value is error
    assert run.call_args.args[0] == ["docker", "rm", "-f", "unique-container-id"]
    sleep.assert_not_called()


def test_failed_create_does_not_remove_another_container(docker):
    create, run, _ = docker
    create.side_effect = subprocess.CalledProcessError(1, ["docker", "create"])
    with pytest.raises(subprocess.CalledProcessError):
        smoke_image.smoke_image("tested")
    run.assert_not_called()
