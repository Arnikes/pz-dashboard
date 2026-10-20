"""Registry fallbacks apply only while obtaining the official Python base image."""

import subprocess
import sys
from unittest.mock import Mock, call

import pytest

from scripts import prepare_image_base


@pytest.fixture
def docker(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(prepare_image_base.subprocess, "run", run)
    return run


def test_first_registry_success_prepares_local_tag_without_fallback(docker):
    selected = prepare_image_base.prepare_image_base("pz-console-python-base:ci")

    assert selected == "docker.io/library/python:3.12-alpine"
    assert docker.call_args_list == [
        call(["docker", "pull", selected], check=False, timeout=120),
        call(["docker", "tag", selected, "pz-console-python-base:ci"], check=True),
    ]


def test_failed_pull_uses_second_registry_and_tags_its_image(docker):
    docker.side_effect = [
        subprocess.CompletedProcess([], 1),
        subprocess.CompletedProcess([], 0),
        subprocess.CompletedProcess([], 0),
    ]

    selected = prepare_image_base.prepare_image_base("prepared-base:ci", timeout=25)

    assert selected == "public.ecr.aws/docker/library/python:3.12-alpine"
    assert docker.call_args_list == [
        call(["docker", "pull", prepare_image_base.PYTHON_BASE_IMAGES[0]], check=False, timeout=25),
        call(["docker", "pull", selected], check=False, timeout=25),
        call(["docker", "tag", selected, "prepared-base:ci"], check=True),
    ]


@pytest.mark.parametrize("times_out", [False, True])
def test_all_pull_failures_stop_before_tagging(docker, times_out):
    if times_out:
        docker.side_effect = subprocess.TimeoutExpired(["docker", "pull"], 17)
    else:
        docker.return_value = subprocess.CompletedProcess([], 1)

    with pytest.raises(RuntimeError, match="Check registry availability") as caught:
        prepare_image_base.prepare_image_base("prepared-base:ci", timeout=17)

    assert docker.call_args_list == [
        call(["docker", "pull", image], check=False, timeout=17)
        for image in prepare_image_base.PYTHON_BASE_IMAGES
    ]
    assert all(image in str(caught.value) for image in prepare_image_base.PYTHON_BASE_IMAGES)


def test_timed_out_pull_uses_next_registry(docker):
    docker.side_effect = [
        subprocess.TimeoutExpired(["docker", "pull"], 8),
        subprocess.CompletedProcess([], 0),
        subprocess.CompletedProcess([], 0),
    ]

    selected = prepare_image_base.prepare_image_base("prepared-base:ci", timeout=8)

    assert selected == prepare_image_base.PYTHON_BASE_IMAGES[1]
    assert docker.call_args_list == [
        call(["docker", "pull", prepare_image_base.PYTHON_BASE_IMAGES[0]], check=False, timeout=8),
        call(["docker", "pull", selected], check=False, timeout=8),
        call(["docker", "tag", selected, "prepared-base:ci"], check=True),
    ]


def test_tag_failure_is_preserved_without_trying_another_registry(docker):
    error = subprocess.CalledProcessError(9, ["docker", "tag"])
    docker.side_effect = [subprocess.CompletedProcess([], 0), error]

    with pytest.raises(subprocess.CalledProcessError) as caught:
        prepare_image_base.prepare_image_base("prepared-base:ci")

    assert caught.value is error
    assert [entry.args[0][1] for entry in docker.call_args_list] == ["pull", "tag"]


@pytest.mark.parametrize(
    "error", [FileNotFoundError("docker missing"), OSError("Docker unavailable")]
)
def test_missing_docker_does_not_retry_registry(docker, error):
    docker.side_effect = error

    with pytest.raises(OSError) as caught:
        prepare_image_base.prepare_image_base("prepared-base:ci")

    assert caught.value is error
    docker.assert_called_once_with(
        ["docker", "pull", prepare_image_base.PYTHON_BASE_IMAGES[0]], check=False, timeout=120
    )


def test_cli_prepares_requested_local_tag(monkeypatch):
    prepare = Mock()
    monkeypatch.setattr(prepare_image_base, "prepare_image_base", prepare)
    monkeypatch.setattr(sys, "argv", ["prepare_image_base.py", "requested-base:ci"])

    prepare_image_base.main()

    prepare.assert_called_once_with("requested-base:ci")
