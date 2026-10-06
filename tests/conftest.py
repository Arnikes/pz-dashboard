"""Shared authenticated credentials for existing API behavior checks."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

import auth  # noqa: E402
import ops  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_watchdog(monkeypatch):
    """Controlled launches in one test must not mute probes in another."""
    monkeypatch.setattr(ops, "_WD", dict(ops._WD))
    monkeypatch.setattr(ops, "_WD_GRACE", {"until": 0.0, "generation": 0})


@pytest.fixture(scope="session")
def authenticated_admin():
    manager = auth.Auth("test-admin", "test-password-long", auth.Fernet.generate_key())
    token = manager.sign_in("test-admin", "test-password-long", "fixture")
    return manager, {"Cookie": f"{auth.COOKIE_NAME}={token}", "X-PZ-Request": "1"}
