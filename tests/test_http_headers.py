"""Header injection regressions exercised through real HTTP responses."""

import io
from pathlib import Path
import urllib.parse

import pytest

import app
from test_auth import auth_server, request, sign_in  # noqa: F401


@pytest.mark.parametrize("separator", ["\r\n", "\r", "\n"])
def test_static_mime_mapping_cannot_inject_headers(auth_server, monkeypatch, separator):  # noqa: F811
    monkeypatch.setattr(
        app.mimetypes, "guess_type", lambda path: (f"text/plain{separator}X-Injected: yes", None)
    )
    status, headers, body = request(auth_server, "GET", "/static/login.js")
    assert status == 200
    assert headers["Content-Type"] == "text/plainX-Injected: yes"
    assert "X-Injected" not in headers
    assert body == (Path(app.STATIC_DIR) / "login.js").read_bytes()


@pytest.mark.parametrize(
    "filename",
    [
        "pzserver-20261006.tar.gz",
        'backup"; injected="yes.tar.gz',
        "backup\r\nX-Injected: yes.tar.gz",
        "backup\rX-Injected: yes.tar.gz",
        "backup\nX-Injected: yes.tar.gz",
        "Бэкап сервера.tar.gz",
        "backup%0D%0AX-Injected.tar.gz",
    ],
)
def test_backup_download_encodes_resolved_filename(auth_server, monkeypatch, filename):  # noqa: F811
    body = b"fictional archive bytes"
    # A POSIX symlink target may contain characters Windows cannot create.
    # Simulate that resolved file while exercising the real HTTP handler.
    path = "/backups/" + filename
    monkeypatch.setattr(app.ops, "backup_download_path", lambda name: path)
    monkeypatch.setattr(app.os.path, "getsize", lambda path: len(body))
    monkeypatch.setattr(app, "open", lambda *args: io.BytesIO(body), raising=False)
    status, headers, received = request(
        auth_server,
        "GET",
        "/api/backup/download?name=pzserver-20261006.tar.gz",
        cookie=sign_in(auth_server),
    )
    assert status == 200
    assert received == body
    assert "X-Injected" not in headers
    disposition = headers["Content-Disposition"]
    prefix = "attachment; filename=\"backup.tar.gz\"; filename*=UTF-8''"
    assert disposition.startswith(prefix)
    encoded = disposition.removeprefix(prefix)
    assert urllib.parse.unquote(encoded) == filename
    assert all(ord(char) < 128 and char not in '\r\n"; ' for char in encoded)
