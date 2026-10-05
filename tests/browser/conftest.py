"""Serve only static assets; all API traffic is intercepted by the browser tests."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest


class StaticHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/static/"):
            self.path = self.path.removeprefix("/static")
        super().do_GET()


@pytest.fixture(scope="session")
def static_url():
    directory = Path(__file__).resolve().parents[2] / "dashboard" / "static"
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(StaticHandler, directory=str(directory)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def dashboard(page, static_url):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "response",
        lambda response: (
            errors.append(response.url)
            if "/static/" in response.url and response.status >= 400
            else None
        ),
    )
    overview = {
        "ok": True,
        "serverName": "Browser test server",
        "docker": True,
        "compose": True,
        "rconConfigured": True,
        "rcon": {"state": "ok"},
        "containerInfo": {"running": True, "status": "running", "uptimeSec": 600},
        "settings": {},
        "backupsCount": 0,
    }
    actions = []
    players = {"ok": True, "names": [], "count": 0, "raw": ""}

    def route_api(route):
        path = route.request.url.split("/api/", 1)[1].split("?", 1)[0]
        if path == "stream":
            import json

            route.fulfill(
                content_type="text/event-stream",
                body=(
                    f"event: overview\ndata: {json.dumps(overview)}\n\n"
                    f"event: players\ndata: {json.dumps(players)}\n\n"
                ),
            )
        elif path == "action":
            actions.append(route.request.post_data_json)
            route.fulfill(status=409, json={"ok": False, "error": "Тест: сервер занят"})
        elif path == "overview":
            route.fulfill(json=overview)
        else:
            route.fulfill(json={"ok": True, "items": [], "points": [], "names": []})

    page.route("**/api/**", route_api)
    yield {"url": static_url, "actions": actions, "players": players, "overview": overview}
    assert not errors, f"Uncaught browser errors: {errors}"
