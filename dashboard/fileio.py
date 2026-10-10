"""Atomic file replacement with bounded retries for Windows sharing failures."""

import os
import tempfile
import time
from pathlib import Path


def replace(source, target):
    for attempt in range(5):
        try:
            os.replace(source, target)
            return
        except PermissionError as error:
            if (
                os.name != "nt"
                or getattr(error, "winerror", None) not in (5, 32, 33)
                or attempt == 4
            ):
                raise
            time.sleep(0.025 * 2**attempt)


def atomic_write(path, data, *, mode=None, owner=None, prefix=".pz-"):
    """Flush bytes to a sibling temporary file before publishing them.

    Callers own serialization, error reporting and directory durability. Optional
    metadata is applied before replacement, so a failure leaves the target intact.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=prefix, dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            if mode is not None:
                os.chmod(temporary, mode)
            if owner is not None and hasattr(os, "chown"):
                os.chown(temporary, *owner)
            os.fsync(stream.fileno())
        replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
