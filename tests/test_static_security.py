"""Static requests stay inside their root and preserve dashboard authentication."""

import os
import subprocess
from urllib.parse import quote

import pytest

import app
from test_auth import auth_server as auth_server, request, sign_in


@pytest.fixture
def static_root(monkeypatch, tmp_path):
    root = tmp_path / "static"
    root.mkdir()
    (root / "nested").mkdir()
    (root / "nested" / "site style.css").write_bytes(b"body { color: green; }")
    (root / "index.html").write_bytes(b"<html>private dashboard</html>")
    monkeypatch.setattr(app, "STATIC_DIR", str(root))
    return root


def test_nested_encoded_asset_is_public(auth_server, static_root):
    status, headers, body = request(auth_server, "GET", "/static/nested/site%20style.css?version=1")
    assert status == 200
    assert body == (static_root / "nested" / "site style.css").read_bytes()
    assert headers["Content-Type"].startswith("text/css")
    assert headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize(
    "path", ["../static-private/secret.txt", "%2e%2e%2fstatic-private/secret.txt"]
)
def test_static_traversal_cannot_read_sibling_with_shared_prefix(auth_server, static_root, path):
    outside = static_root.parent / "static-private"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"outside secret")
    status, _, body = request(auth_server, "GET", "/static/" + path)
    assert status == 403
    assert body != b"outside secret"


@pytest.mark.skipif(os.name != "nt", reason="Native Windows path semantics")
def test_windows_drive_and_backslash_traversal_are_forbidden(auth_server, static_root):
    outside = static_root.parent / "secret.txt"
    outside.write_bytes(b"outside secret")
    for path in (str(outside), r"..\secret.txt"):
        status, _, body = request(auth_server, "GET", "/static/" + quote(path, safe=""))
        assert status == 403
        assert body != b"outside secret"


@pytest.mark.parametrize("path", ["missing.css", "nested", "nested/site%20style.css%00"])
def test_non_file_static_requests_return_errors(auth_server, static_root, path):
    status, _, body = request(auth_server, "GET", "/static/" + path)
    assert status in (403, 404)
    assert body["ok"] is False


def test_normalized_index_alias_preserves_authentication(auth_server, static_root):
    path = "/static/nested/../%69ndex.html"
    assert request(auth_server, "GET", path)[0] == 401
    cookie = sign_in(auth_server)
    status, headers, body = request(auth_server, "GET", path, cookie=cookie)
    assert status == 200
    assert body == (static_root / "index.html").read_bytes()
    assert headers["Cache-Control"] == "no-store"


def test_static_symlink_cannot_escape_root(auth_server, static_root):
    outside = static_root.parent / "secret.txt"
    outside.write_bytes(b"outside secret")
    try:
        (static_root / "linked.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    assert request(auth_server, "GET", "/static/linked.txt")[0] == 403


def test_index_symlink_requires_login_and_serves_authenticated_dashboard(auth_server, static_root):
    try:
        (static_root / "alias.html").symlink_to(static_root / "index.html")
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    assert request(auth_server, "GET", "/static/alias.html")[0] == 401
    cookie = sign_in(auth_server)
    status, headers, body = request(auth_server, "GET", "/static/alias.html", cookie=cookie)
    assert status == 200
    assert body == (static_root / "index.html").read_bytes()
    assert headers["Cache-Control"] == "no-store"


@pytest.mark.skipif(os.name != "nt", reason="Junctions are a Windows filesystem feature")
def test_static_directory_junction_cannot_escape_root(auth_server, static_root):
    outside = static_root.parent / "private"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"outside secret")
    junction = static_root / "linked-directory"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        pytest.skip("Junction creation is unavailable")
    assert request(auth_server, "GET", "/static/linked-directory/secret.txt")[0] == 403
