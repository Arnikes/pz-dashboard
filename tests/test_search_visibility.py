"""The administrator panel must not advertise indexable pages or responses."""

import http.client
from html.parser import HTMLParser
from pathlib import Path

import pytest

import app
from test_auth import auth_server as auth_server, request, sign_in


@pytest.mark.parametrize(
    "method,path,status",
    [
        ("GET", "/", 303),
        ("GET", "/login", 200),
        ("GET", "/login?lang=en", 200),
        ("GET", "/static/login.html", 200),
        ("GET", "/static/offline.html", 200),
        ("GET", "/static/config-help.html", 200),
        ("GET", "/static/fonts/fonts.css", 200),
        ("GET", "/favicon.ico", 200),
        ("GET", "/manifest.webmanifest", 200),
        ("GET", "/sw.js", 200),
        ("GET", "/api/health", 200),
        ("GET", "/api/overview", 401),
        ("GET", "/static/index.html", 401),
        ("GET", "/static/../../app.py", 403),
        ("GET", "/static/missing.html", 404),
        ("POST", "/api/auth/login", 401),
        ("HEAD", "/login", 501),
        ("OPTIONS", "/login", 501),
    ],
)
def test_anonymous_responses_exclude_search_indexing(auth_server, method, path, status):
    code, headers, _ = request(auth_server, method, path)
    assert code == status
    assert {"noindex", "nofollow", "nosnippet"} <= set(
        headers["X-Robots-Tag"].replace(" ", "").split(",")
    )


def test_robots_allows_crawlers_to_read_noindex_without_exposing_private_data(auth_server):
    status, headers, body = request(auth_server, "GET", "/robots.txt")
    assert status == 200
    assert headers["Content-Type"].startswith("text/plain")
    rules = [line.strip() for line in body.decode().splitlines() if not line.startswith("#")]
    assert rules == ["User-agent: *", "Disallow:"]
    assert "noindex" in headers["X-Robots-Tag"]
    assert request(auth_server, "GET", "/api/overview")[0] == 401


def test_authenticated_html_errors_and_downloads_exclude_search_indexing(
    auth_server, monkeypatch, tmp_path
):
    cookie = sign_in(auth_server)
    archive = tmp_path / "world.tar.gz"
    archive.write_bytes(b"isolated archive fixture")
    monkeypatch.setattr(app.ops, "backup_download_path", lambda name: str(archive))
    monkeypatch.setattr(app.ops, "docker_ok_cached", lambda: True)

    def full_logs(destination):
        destination.write(b"isolated log fixture")
        return True

    monkeypatch.setattr(app.ops, "full_logs", full_logs)
    for path, expected in [
        ("/", 200),
        ("/index.html", 200),
        ("/login", 303),
        ("/api/auth/session", 200),
        ("/missing", 404),
        ("/api/backup/download?name=world.tar.gz", 200),
        ("/api/logs/full", 200),
    ]:
        status, headers, _ = request(auth_server, "GET", path, cookie=cookie)
        assert status == expected
        assert "noindex" in headers["X-Robots-Tag"]


def test_unconfigured_auth_still_excludes_search_indexing(auth_server):
    auth_server.auth = None
    status, headers, _ = request(auth_server, "GET", "/")
    assert status == 503
    assert "noindex" in headers["X-Robots-Tag"]


def test_live_stream_excludes_search_indexing(auth_server, monkeypatch):
    cookie = sign_in(auth_server)
    monkeypatch.setattr(app.payloads, "STREAM_PLAN", ())
    connection = http.client.HTTPConnection(*auth_server.server_address, timeout=5)
    try:
        connection.request("GET", "/api/stream", headers={"Cookie": cookie})
        response = connection.getresponse()
        assert response.status == 200
        assert "noindex" in response.getheader("X-Robots-Tag", "")
        request(auth_server, "POST", "/api/auth/logout", cookie=cookie)
        assert b"event: auth-expired" in response.read()
    finally:
        connection.close()


class RobotsMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.directives = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("name") == "robots":
            self.directives.append(attributes.get("content", ""))


@pytest.mark.parametrize("path", sorted(Path(app.STATIC_DIR).glob("*.html")), ids=lambda p: p.name)
def test_every_html_page_excludes_indexing_even_if_proxy_strips_headers(path):
    metadata = RobotsMetadata()
    metadata.feed(path.read_text(encoding="utf-8"))
    assert len(metadata.directives) == 1
    assert {"noindex", "nofollow", "nosnippet"} <= set(
        metadata.directives[0].replace(" ", "").split(",")
    )
