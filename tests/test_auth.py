"""Authentication boundaries tested through real HTTP and encrypted sessions."""

import http.client
import io
import json
import os
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app
import auth

LOGIN = "админ"
PASSWORD = "длинный-пароль-админа"


@pytest.fixture
def auth_server(monkeypatch, tmp_path):
    key = auth.Fernet.generate_key()
    manager = auth.Auth(LOGIN, PASSWORD, key, sessions_file=tmp_path / "auth-sessions.bin")
    monkeypatch.setattr(app.Handler, "log_message", lambda *args: None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    server.auth = manager
    server.test_key = key
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(server, method, path, data=None, *, cookie=None, headers=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    sent_headers = {"X-PZ-Request": "1", **(headers or {})}
    if cookie:
        sent_headers["Cookie"] = cookie
    try:
        body = json.dumps(data).encode() if data is not None else None
        connection.request(method, path, body, sent_headers)
        response = connection.getresponse()
        raw = response.read()
        data = (
            json.loads(raw) if "application/json" in response.getheader("Content-Type", "") else raw
        )
        return response.status, dict(response.getheaders()), data
    finally:
        connection.close()


def sign_in(server):
    status, headers, data = request(
        server, "POST", "/api/auth/login", {"login": LOGIN, "password": PASSWORD}
    )
    assert status == 200 and data == {"ok": True}
    return headers["Set-Cookie"].split(";", 1)[0]


@pytest.mark.parametrize(
    "login,password,key",
    [("", PASSWORD, None), (LOGIN, "short", None), (LOGIN, PASSWORD, "invalid")],
)
def test_bootstrap_rejects_missing_or_invalid_secrets(login, password, key):
    with pytest.raises(auth.AuthError):
        auth.Auth(login, password, key if key is not None else auth.Fernet.generate_key())


def test_bootstrap_from_env_and_secure_cookie(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_DIR", str(tmp_path))
    monkeypatch.setenv("PZ_ADMIN_LOGIN", LOGIN)
    monkeypatch.setenv("PZ_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("PZ_AUTH_KEY", auth.Fernet.generate_key().decode())
    monkeypatch.setenv("PZ_AUTH_COOKIE_SECURE", "true")
    manager = auth.Auth.from_env()
    token = manager.sign_in(LOGIN, PASSWORD, "peer")
    assert "Secure" in manager.cookie(token)
    assert PASSWORD.encode() != manager._password_hash
    monkeypatch.setenv("PZ_AUTH_COOKIE_SECURE", "invalid")
    with pytest.raises(auth.AuthError):
        auth.Auth.from_env()


@pytest.mark.parametrize("remember", [False, True])
def test_remember_cookie_and_restart(auth_server, remember):
    status, headers, _ = request(
        auth_server,
        "POST",
        "/api/auth/login",
        {
            "login": LOGIN,
            "password": PASSWORD,
            "remember": remember,
        },
    )
    assert status == 200
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    if remember:
        assert f"Max-Age={auth.REMEMBER_SECONDS}" in headers["Set-Cookie"]
    else:
        assert "Max-Age" not in headers["Set-Cookie"]
        assert "expires=" not in headers["Set-Cookie"].lower()
    auth_server.auth = auth.Auth(
        LOGIN, PASSWORD, auth_server.test_key, sessions_file=auth_server.auth._sessions_file
    )
    assert request(auth_server, "GET", "/api/auth/session", cookie=cookie)[0] == (
        200 if remember else 401
    )
    if remember:
        assert request(auth_server, "POST", "/api/auth/logout", cookie=cookie)[0] == 200
        auth_server.auth = auth.Auth(
            LOGIN, PASSWORD, auth_server.test_key, sessions_file=auth_server.auth._sessions_file
        )
        assert request(auth_server, "GET", "/api/auth/session", cookie=cookie)[0] == 401


@pytest.mark.parametrize("remember", ["true", 1, None, [], {}])
def test_remember_requires_boolean(auth_server, remember):
    assert (
        request(
            auth_server,
            "POST",
            "/api/auth/login",
            {
                "login": LOGIN,
                "password": PASSWORD,
                "remember": remember,
            },
        )[0]
        == 400
    )


@pytest.mark.parametrize("remember", [False, True])
def test_session_lifetime_is_fixed(tmp_path, monkeypatch, remember):
    key = auth.Fernet.generate_key()
    path = tmp_path / "auth-sessions.bin"
    manager = auth.Auth(LOGIN, PASSWORD, key, sessions_file=path)
    now = auth.time.time()
    # Keep password-hash duration from moving the session's issuance deadline.
    monkeypatch.setattr(auth.time, "time", lambda: now)
    token = manager.sign_in(LOGIN, PASSWORD, "peer", remember=remember)
    cookie = f"pz_session={token}"
    session_id = manager.session(cookie)
    deadline = manager._sessions[session_id]
    monkeypatch.setattr(auth.time, "time", lambda: now + auth.SESSION_SECONDS + 1)
    assert bool(manager.session(cookie)) == remember
    assert not remember or manager._sessions[session_id] == deadline
    monkeypatch.setattr(auth.time, "time", lambda: now + auth.REMEMBER_SECONDS + 1)
    assert not manager.session(cookie)
    assert not auth.Auth(LOGIN, PASSWORD, key, sessions_file=path).session(cookie)


@pytest.mark.parametrize("change", ["login", "password", "key", "damage"])
def test_saved_sessions_revoke_on_secret_change_or_damage(tmp_path, change):
    key = auth.Fernet.generate_key()
    path = tmp_path / "auth-sessions.bin"
    manager = auth.Auth(LOGIN, PASSWORD, key, sessions_file=path)
    token = manager.sign_in(LOGIN, PASSWORD, "peer", remember=True)
    cookie = f"pz_session={token}"
    session_id = manager.session(cookie)
    stored = path.read_bytes()
    assert all(value.encode() not in stored for value in (LOGIN, PASSWORD, session_id))
    if change == "damage":
        path.write_bytes(b"damaged")
    changed = auth.Auth(
        LOGIN + "2" if change == "login" else LOGIN,
        PASSWORD + "2" if change == "password" else PASSWORD,
        auth.Fernet.generate_key() if change == "key" else key,
        sessions_file=path,
    )
    assert not changed.session(cookie)
    assert not auth.Auth(LOGIN, PASSWORD, key, sessions_file=path).session(cookie)


def test_storage_failure_does_not_issue_or_resurrect_session(auth_server, monkeypatch):
    status, headers, _ = request(
        auth_server,
        "POST",
        "/api/auth/login",
        {
            "login": LOGIN,
            "password": PASSWORD,
            "remember": True,
        },
    )
    assert status == 200
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    sessions = auth_server.auth._sessions.copy()

    def fail_replace(*args):
        raise OSError("read-only")

    monkeypatch.setattr(auth.os, "replace", fail_replace)
    status, headers, _ = request(
        auth_server,
        "POST",
        "/api/auth/login",
        {
            "login": LOGIN,
            "password": PASSWORD,
            "remember": True,
        },
    )
    assert status == 503 and "Set-Cookie" not in headers
    assert auth_server.auth._sessions == sessions
    assert request(auth_server, "POST", "/api/auth/logout", cookie=cookie)[0] == 503
    assert auth_server.auth.session(cookie)
    assert not list(auth_server.auth._sessions_file.parent.glob(".auth-*"))


def test_unwritable_session_store_rejects_startup(tmp_path):
    path = tmp_path / "not-a-directory"
    path.write_text("blocked")
    with pytest.raises(auth.SessionStorageError):
        auth.Auth(LOGIN, PASSWORD, auth.Fernet.generate_key(), sessions_file=path / "sessions")


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/overview"),
        ("GET", "/api/stream"),
        ("GET", "/api/logs/full"),
        ("GET", "/api/config-draft"),
        ("GET", "/api/server-configs"),
        ("GET", "/api/telegram-chats"),
        ("GET", "/api/backup/download?name=secret.tar.gz"),
        ("POST", "/api/rcon"),
        ("POST", "/api/action"),
        ("POST", "/api/settings"),
        ("POST", "/api/config-draft"),
        ("DELETE", "/api/backup?name=secret.tar.gz"),
    ],
)
def test_anonymous_requests_never_reach_operations(auth_server, monkeypatch, method, path):
    dispatch = Mock()
    monkeypatch.setattr(app.actions, "dispatch", dispatch)
    monkeypatch.setattr(app.payloads, "stream_payload", dispatch)
    monkeypatch.setattr(app.ops, "rcon", dispatch)
    monkeypatch.setattr(app.ops, "delete_backup", dispatch)
    assert request(auth_server, method, path, {})[0] == 401
    dispatch.assert_not_called()


def test_pages_health_and_static_alias_cannot_bypass_login(auth_server):
    assert request(auth_server, "GET", "/")[1]["Location"] == "/login"
    assert request(auth_server, "GET", "/login")[0] == 200
    assert request(auth_server, "GET", "/static/login.js")[0] == 200
    assert request(auth_server, "GET", "/api/health")[0] == 200
    for path in ("/static/index.html", "/static/../index.html", "/static/%69ndex.html"):
        assert request(auth_server, "GET", path)[0] in (401, 403)


@pytest.mark.skipif(os.name != "nt", reason="Windows treats backslashes as path separators")
@pytest.mark.parametrize("path", ["/static/.%5cindex.html", "/static/assets%5c..%5cindex.html"])
def test_windows_static_alias_cannot_bypass_login(auth_server, path):
    assert request(auth_server, "GET", path)[0] in (401, 403)


def test_static_symlink_cannot_bypass_login(auth_server, monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text("protected dashboard", encoding="utf-8")
    try:
        (tmp_path / "alias.html").symlink_to(tmp_path / "index.html")
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    monkeypatch.setattr(app, "STATIC_DIR", str(tmp_path))
    assert request(auth_server, "GET", "/static/alias.html")[0] == 401


def test_invalid_static_path_returns_error(auth_server):
    assert request(auth_server, "GET", "/static/login.js%00")[0] in (403, 404)


def test_login_logout_revokes_cookie_and_replay(auth_server):
    cookie = sign_in(auth_server)
    assert request(auth_server, "GET", "/api/auth/session", cookie=cookie)[2]["login"] == LOGIN
    assert request(auth_server, "GET", "/", cookie=cookie)[0] == 200
    assert request(auth_server, "GET", "/login", cookie=cookie)[1]["Location"] == "/"
    assert PASSWORD not in cookie and LOGIN not in cookie
    status, headers, _ = request(auth_server, "POST", "/api/auth/logout", cookie=cookie)
    assert status == 200 and "Max-Age=0" in headers["Set-Cookie"]
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    assert request(auth_server, "GET", "/api/auth/session", cookie=cookie)[0] == 401


def test_password_failures_and_rate_limit(auth_server, monkeypatch):
    for _ in range(5):
        status, _, data = request(
            auth_server, "POST", "/api/auth/login", {"login": LOGIN, "password": "wrong"}
        )
        assert status == 401 and data["error"] == "Неверный логин или пароль"
    assert (
        request(auth_server, "POST", "/api/auth/login", {"login": LOGIN, "password": PASSWORD})[0]
        == 429
    )
    now = auth.time.monotonic()
    monkeypatch.setattr(auth.time, "monotonic", lambda: now + 61)
    assert sign_in(auth_server)


@pytest.mark.parametrize(
    "headers",
    [
        {"X-PZ-Request": ""},
        {"Origin": "https://evil.test"},
        {"Origin": "null"},
        {"Origin": "http://[evil"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_cross_origin_login_and_mutations_rejected(auth_server, headers):
    cookie = sign_in(auth_server)
    assert (
        request(
            auth_server,
            "POST",
            "/api/auth/login",
            {"login": LOGIN, "password": PASSWORD},
            headers=headers,
        )[0]
        == 403
    )
    assert (
        request(auth_server, "POST", "/api/settings", {}, cookie=cookie, headers=headers)[0] == 403
    )
    assert request(auth_server, "DELETE", "/api/backup", cookie=cookie, headers=headers)[0] == 403
    assert request(auth_server, "GET", "/api/auth/session", cookie=cookie)[0] == 200


def test_tampering_expiry_and_restart_invalidate_sessions(auth_server, monkeypatch):
    cookie = sign_in(auth_server)
    assert auth_server.auth.session(cookie)
    assert not auth_server.auth.session(cookie[:-5] + "abcde")
    assert not auth_server.auth.session("pz_session=bad")
    session_id = auth_server.auth.session(cookie)
    restarted = auth.Auth(LOGIN, PASSWORD, auth_server.test_key)
    assert not restarted.session(cookie)
    wrong_key = auth.Auth(LOGIN, PASSWORD, auth.Fernet.generate_key())
    assert not wrong_key.session(cookie)
    expired_token = auth_server.auth._cipher.encrypt_at_time(
        session_id.encode(), int(auth.time.time()) - auth.SESSION_SECONDS - 1
    ).decode()
    assert not auth_server.auth.session(f"pz_session={expired_token}")
    auth_server.auth._sessions[session_id] = auth.time.time() - 1
    assert request(auth_server, "GET", "/api/auth/session", cookie=cookie)[0] == 401


def test_sse_closes_when_session_revoked(auth_server, monkeypatch):
    cookie = sign_in(auth_server)
    monkeypatch.setattr(app.payloads, "STREAM_PLAN", (("overview", 100),))
    monkeypatch.setattr(app.payloads, "stream_payload", lambda name, **_kwargs: {"ok": True})
    connection = http.client.HTTPConnection(*auth_server.server_address, timeout=5)
    try:
        connection.request("GET", "/api/stream", headers={"Cookie": cookie})
        response = connection.getresponse()
        assert response.status == 200
        assert response.readline() == b"retry: 3000\n"
        request(auth_server, "POST", "/api/auth/logout", cookie=cookie)
        assert b"event: auth-expired" in response.read()
    finally:
        connection.close()


def test_unconfigured_server_is_closed(auth_server):
    auth_server.auth = None
    assert request(auth_server, "GET", "/api/overview")[0] == 503
    assert request(auth_server, "POST", "/api/auth/login", {})[0] == 503
    assert request(auth_server, "GET", "/api/health")[0] == 200


@pytest.mark.parametrize(
    "body",
    [b'{"nested":' + b"[" * 15000 + b"0" + b"]" * 15000 + b"}", b"{}"],
    ids=["deep-nesting", "truncated"],
)
def test_json_parser_rejects_deep_nesting_and_truncated_body(body):
    handler = SimpleNamespace(
        headers={"Content-Length": str(len(body) + (1 if body == b"{}" else 0))},
        path="/api/auth/login",
        rfile=io.BytesIO(body),
    )
    with pytest.raises(ValueError, match="JSON"):
        app._read_json(handler)


@pytest.mark.parametrize("depth", [20, 63])
def test_json_parser_preserves_nested_data_within_the_depth_limit(depth):
    body = b'{"nested":' + b"[" * depth + b"0" + b"]" * depth + b"}"
    handler = SimpleNamespace(
        headers={"Content-Length": str(len(body))},
        path="/api/settings",
        rfile=io.BytesIO(body),
    )
    assert app._read_json(handler) == json.loads(body)


def test_json_parser_rejects_the_first_container_over_the_depth_limit():
    body = b'{"nested":' + b"[" * 64 + b"0" + b"]" * 64 + b"}"
    handler = SimpleNamespace(
        headers={"Content-Length": str(len(body))},
        path="/api/settings",
        rfile=io.BytesIO(body),
    )
    with pytest.raises(ValueError, match="JSON"):
        app._read_json(handler)


def test_nested_settings_input_is_rejected_before_recursive_processing(auth_server, monkeypatch):
    cookie = sign_in(auth_server)
    patch_settings = Mock(return_value=None)
    monkeypatch.setattr(app.ops, "patch_settings", patch_settings)
    nested = 0
    for _ in range(500):
        nested = [nested]
    status, _, response = request(
        auth_server, "POST", "/api/settings", {"autoUpdate": {"extra": nested}}, cookie=cookie
    )
    assert status == 400 and response["ok"] is False
    patch_settings.assert_not_called()


def test_deeply_nested_login_body_returns_json_error(auth_server):
    connection = http.client.HTTPConnection(*auth_server.server_address, timeout=5)
    body = b'{"nested":' + b"[" * 15000 + b"0" + b"]" * 15000 + b"}"
    try:
        connection.request("POST", "/api/auth/login", body, {"X-PZ-Request": "1"})
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read())["ok"] is False
    finally:
        connection.close()


def test_deeply_nested_saved_sessions_are_replaced_with_empty_store(tmp_path):
    key = auth.Fernet.generate_key()
    path = tmp_path / "auth-sessions.bin"
    manager = auth.Auth(LOGIN, PASSWORD, key, sessions_file=path)
    damaged = b'{"nested":' + b"[" * 15000 + b"0" + b"]" * 15000 + b"}"
    path.write_bytes(manager._cipher.encrypt(damaged))
    restarted = auth.Auth(LOGIN, PASSWORD, key, sessions_file=path)
    assert not restarted._sessions
    assert json.loads(restarted._cipher.decrypt(path.read_bytes()))["sessions"] == {}
