"""Shared authenticated credentials for existing API behavior checks."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))

import auth  # noqa: E402


@pytest.fixture(scope="session")
def authenticated_admin():
    manager = auth.Auth("test-admin", "test-password-long", auth.Fernet.generate_key())
    token = manager.sign_in("test-admin", "test-password-long", "fixture")
    return manager, {"Cookie": f"{auth.COOKIE_NAME}={token}", "X-PZ-Request": "1"}
