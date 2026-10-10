"""Atomic publication must preserve existing data and clean failed temporary files."""

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

import fileio


def test_atomic_write_publishes_closed_synced_sibling(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "state.bin"
    path.parent.mkdir()
    path.write_bytes(b"original")
    synced = []
    fsync = fileio.os.fsync
    replace = fileio.replace

    def sync(descriptor):
        fsync(descriptor)
        synced.append(descriptor)

    def publish(source, target):
        source = Path(source)
        assert source.parent == path.parent
        assert source.read_bytes() == b"replacement"
        assert path.read_bytes() == b"original"
        assert synced
        # Windows replacement also exercises closure of the temporary file.
        replace(source, target)

    monkeypatch.setattr(fileio.os, "fsync", sync)
    monkeypatch.setattr(fileio, "replace", publish)
    fileio.atomic_write(path, b"replacement")
    assert path.read_bytes() == b"replacement"
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("failure", ["write", "fsync", "chmod", "chown", "replace"])
@pytest.mark.parametrize("existing", [False, True])
def test_atomic_write_failure_preserves_target_and_removes_temporary(
    tmp_path, monkeypatch, failure, existing
):
    path = tmp_path / "state.bin"
    if existing:
        path.write_bytes(b"original")
    failing = Mock(side_effect=OSError("disk unavailable"))
    if failure in ("fsync", "chmod", "chown"):
        monkeypatch.setattr(fileio.os, failure, failing, raising=False)
    elif failure == "replace":
        monkeypatch.setattr(fileio, "replace", failing)
    data = "invalid bytes" if failure == "write" else b"replacement"

    with pytest.raises((OSError, TypeError)):
        fileio.atomic_write(
            path, data, mode=0o600, owner=(123, 456) if failure == "chown" else None
        )
    if existing:
        assert path.read_bytes() == b"original"
    else:
        assert not path.exists()
    assert not list(tmp_path.glob(".pz-*"))


def test_atomic_write_creates_parent_directories(tmp_path):
    path = tmp_path / "new" / "nested" / "state.bin"
    fileio.atomic_write(str(path), b"saved")
    assert path.read_bytes() == b"saved"
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and modes")
def test_atomic_write_applies_metadata_before_publication(tmp_path, monkeypatch):
    path = tmp_path / "state.bin"
    path.write_bytes(b"original")
    owner = (os.getuid(), os.getgid())
    replace = fileio.replace

    def publish(source, target):
        stat = Path(source).stat()
        assert stat.st_mode & 0o777 == 0o640
        assert (stat.st_uid, stat.st_gid) == owner
        replace(source, target)

    monkeypatch.setattr(fileio, "replace", publish)
    fileio.atomic_write(path, b"replacement", mode=0o640, owner=owner)
    assert path.read_bytes() == b"replacement"
