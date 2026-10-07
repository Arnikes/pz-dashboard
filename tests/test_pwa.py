"""PWA metadata, public endpoints and content-derived worker revision."""

import json
import struct
from pathlib import Path

import app
import pwa
from test_auth import auth_server as auth_server, request


def test_public_pwa_endpoints_and_private_html(auth_server):
    for path, content_type in [
        ("/manifest.webmanifest", "application/manifest+json"),
        ("/sw.js", "text/javascript"),
        ("/static/offline.html", "text/html"),
    ]:
        status, headers, body = request(auth_server, "GET", path)
        assert status == 200 and body
        assert headers["Content-Type"].startswith(content_type)
        assert headers["Cache-Control"] == "no-cache"
        assert headers["X-Content-Type-Options"] == "nosniff"
        if path == "/sw.js":
            assert headers["Service-Worker-Allowed"] == "/"
            assert b"__PZ_" not in body
    for path in ["/", "/index.html", "/static/index.html"]:
        assert request(auth_server, "GET", path)[0] in (303, 401)
    assert request(auth_server, "GET", "/login")[1]["Cache-Control"] == "no-store"
    assert request(auth_server, "GET", "/api/auth/session")[0] == 401


def test_manifest_icons_and_entrypoints():
    root = Path(app.STATIC_DIR)
    manifest = json.loads((root / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["id"] == manifest["scope"] == manifest["start_url"] == "/"
    assert manifest["display"] == "standalone" and manifest["lang"] == "ru"
    assert {icon["purpose"] for icon in manifest["icons"]} == {"any", "maskable"}
    for icon in manifest["icons"]:
        raw = (root / icon["src"].removeprefix("/static/")).read_bytes()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", raw[16:24])
        assert icon["sizes"] == f"{width}x{height}"
    for name in ["index.html", "login.html", "offline.html"]:
        html = (root / name).read_text(encoding="utf-8")
        assert 'href="/manifest.webmanifest"' in html
        assert 'href="/static/icons/apple-touch-icon.png"' in html
        assert 'src="/static/pwa.js"' in html


def test_worker_revision_changes_with_private_html_and_public_assets(tmp_path):
    (tmp_path / "fonts").mkdir()
    (tmp_path / "icons").mkdir()
    (tmp_path / "sw.js").write_text("__PZ_REVISION__\n__PZ_ASSETS__")
    private = tmp_path / "index.html"
    private.write_text("v1")
    first = pwa.service_worker(tmp_path)
    assert first == pwa.service_worker(tmp_path)
    private.write_text("v2")
    assert first != pwa.service_worker(tmp_path)
    assets = json.loads(first.decode().splitlines()[1])
    assert "/static/offline.html" in assets
    assert not any("index.html" in path or "login" in path or "/api/" in path for path in assets)


def test_public_shell_only_precaches_font_sources_used_by_current_css():
    worker = pwa.service_worker(app.STATIC_DIR).decode()
    assets = json.loads(worker.split("const ASSETS = ", 1)[1].split(";", 1)[0])
    fonts = [path for path in assets if path.endswith(".woff2")]
    assert len(fonts) == 6
    assert all("-400-" in path for path in fonts)
    assert all((Path(app.STATIC_DIR) / path.removeprefix("/static/")).is_file() for path in fonts)
