"""Exercise the shipped worker through the real HTTP handler, without live PZ operations."""

import shutil
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect

import app
import auth

pytestmark = [pytest.mark.browser, pytest.mark.browser_context_args(service_workers="allow")]


@pytest.fixture
def pwa_server(monkeypatch, tmp_path, page):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    root = tmp_path / "static"
    shutil.copytree(app.STATIC_DIR, root)
    monkeypatch.setattr(app, "STATIC_DIR", str(root))

    class PwaHandler(app.Handler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            path = urlparse(self.path).path
            if getattr(self.server, "unavailable", False) and path in ("/", "/api/health"):
                self._send_error_json(503, "Пульт недоступен")
                return
            if path.startswith("/api/") and not path.startswith("/api/auth/"):
                if path != "/api/health" and not self._require_auth():
                    return
                if path == "/api/stream":
                    body = b'event: overview\ndata: {"ok":true,"serverName":"PWA test","docker":false}\n\n'
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._send_json({"ok": True, "items": [], "profiles": [], "names": []})
                return
            super().do_GET()

    manager = auth.Auth("pwa-admin", "pwa-test-password-long", auth.Fernet.generate_key())
    server = ThreadingHTTPServer(("127.0.0.1", 0), PwaHandler)
    server.auth = manager
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "url": f"http://127.0.0.1:{server.server_port}",
            "root": root,
            "auth": manager,
            "server": server,
        }
        assert not errors, f"Uncaught browser errors: {errors}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def ready(page, server):
    page.goto(server["url"] + "/login")
    page.wait_for_function("!!navigator.serviceWorker.controller")


def test_offline_launch_reconnect_and_private_cache(page, context, pwa_server):
    ready(page, pwa_server)
    cached = page.evaluate("""async () => {
      const result = [];
      for (const name of await caches.keys()) {
        for (const request of await (await caches.open(name)).keys()) result.push(new URL(request.url).pathname);
      }
      return result;
    }""")
    assert "/static/offline.html" in cached
    assert not any("/api/" in path or "login" in path or "index.html" in path for path in cached)
    context.set_offline(True)
    page.goto(pwa_server["url"] + "/#/mods")
    expect(page.get_by_role("heading", name="Нет связи с пультом")).to_be_visible()
    page.get_by_role("button", name="Повторить подключение").click()
    expect(page.locator("#pwaRetryStatus")).to_contain_text("Пульт пока недоступен")
    expect(page.get_by_role("button", name="Повторить подключение")).to_be_enabled()
    context.set_offline(False)
    expect(page.locator("#loginForm")).to_be_visible(timeout=15000)
    # Offline fallback must not bypass the normal authentication redirect.
    assert urlparse(page.url).path == "/login"


def test_update_waits_for_password_and_does_not_reload_other_tab(page, context, pwa_server):
    ready(page, pwa_server)
    old_caches = page.evaluate("caches.keys()")
    other = context.new_page()
    other.goto(pwa_server["url"] + "/login")
    other.locator("#password").fill("keep-this-input")
    root = pwa_server["root"]
    offline = root / "offline.html"
    offline.write_text(
        offline.read_text(encoding="utf-8") + "\n<!-- updated shell -->", encoding="utf-8"
    )
    page.evaluate("async () => (await navigator.serviceWorker.getRegistration()).update()")
    page.wait_for_function("navigator.serviceWorker.getRegistration().then(r => !!r.waiting)")
    expect(page.locator("#pwaUpdate")).to_be_visible()
    page.locator("#password").fill("do-not-lose-this")
    page.locator("#pwaUpdate").click()
    expect(page.locator(".pwa-message")).to_contain_text("Сохраните введённые изменения")
    expect(page.locator("#password")).to_have_value("do-not-lose-this")
    assert page.evaluate("navigator.serviceWorker.getRegistration().then(r => !!r.waiting)")
    page.locator("#password").fill("")
    with page.expect_navigation(wait_until="load"):
        page.locator("#pwaUpdate").click()
    page.wait_for_function("!!navigator.serviceWorker.controller")
    new_caches = page.evaluate("caches.keys()")
    assert len(new_caches) == 1 and new_caches != old_caches
    expect(other.locator("#password")).to_have_value("keep-this-input")
    expect(other.locator("#pwaUpdate")).to_be_visible()


def test_install_prompt_and_standalone_hides_install(page, pwa_server):
    ready(page, pwa_server)
    session = page.context.new_cdp_session(page)
    metadata = session.send("Page.getAppManifest")
    assert not metadata["errors"]
    assert session.send("Page.getInstallabilityErrors")["installabilityErrors"] == []
    page.evaluate("""() => {
      window.installCalls = 0;
      const event = new Event('beforeinstallprompt', {cancelable: true});
      event.prompt = async () => { window.installCalls++; };
      event.userChoice = Promise.resolve({outcome: 'dismissed'});
      window.dispatchEvent(event);
    }""")
    expect(page.locator("#pwaInstall")).to_be_visible()
    page.locator("#pwaInstall").click()
    assert page.evaluate("window.installCalls") == 1
    expect(page.locator("#pwaInstall")).to_be_hidden()
    page.evaluate("""() => {
      Object.defineProperty(navigator, 'standalone', {value: true});
      window.dispatchEvent(new Event('appinstalled'));
    }""")
    expect(page.locator(".pwa-bar")).to_be_hidden()


def test_ios_guidance_and_live_offline_preserves_login_input(page, pwa_server, tmp_path):
    page.set_viewport_size({"width": 390, "height": 844})
    page.add_init_script("Object.defineProperty(navigator, 'userAgent', {value: 'iPhone'});")
    ready(page, pwa_server)
    page.locator("#pwaInstall").click()
    expect(page.locator(".pwa-help")).to_contain_text("На экран Домой")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / "install-ios-390.png"), full_page=True)
    page.locator("#login").fill("my-admin")
    page.locator("#password").fill("my-password")
    page.context.set_offline(True)
    expect(page.locator(".pwa-message")).to_contain_text("Нет сети")
    expect(page.locator("#password")).to_have_value("my-password")
    page.context.set_offline(False)
    expect(page.locator("#login")).to_have_value("my-admin")


def test_server_unavailable_uses_offline_shell(page, pwa_server):
    ready(page, pwa_server)
    pwa_server["server"].unavailable = True
    page.goto(pwa_server["url"] + "/")
    expect(page.get_by_role("heading", name="Нет связи с пультом")).to_be_visible()
    page.locator("#pwaRetry").click()
    expect(page.locator("#pwaRetryStatus")).to_contain_text("Пульт пока недоступен")


@pytest.mark.browser_context_args(service_workers="block")
def test_dashboard_update_guard_and_offline_mutation(page, dashboard):
    page.goto(dashboard["url"])
    page.wait_for_function("!!window.ConfigEditor")
    allowed = (
        "() => document.dispatchEvent(new CustomEvent('pz:before-update', {cancelable: true}))"
    )
    assert page.evaluate(allowed)
    page.evaluate("document.getElementById('consoleInput').value = 'servermsg hello'")
    assert not page.evaluate(allowed)
    page.evaluate("document.getElementById('consoleInput').value = ''")
    assert page.evaluate(allowed)
    page.evaluate("document.getElementById('iniSource').dispatchEvent(new Event('input'))")
    assert not page.evaluate(allowed)
    page.context.set_offline(True)
    result = page.evaluate("""async () => {
      try { await api('/api/action', {method: 'POST', body: {op: 'restart'}}); return null; }
      catch (error) { return error.message; }
    }""")
    assert "Нет сети" in result
    assert dashboard["actions"] == []
    page.context.set_offline(False)


def test_logout_and_api_remain_network_only(page, context, pwa_server):
    token = pwa_server["auth"].sign_in("pwa-admin", "pwa-test-password-long", "fixture")
    context.add_cookies([{"name": auth.COOKIE_NAME, "value": token, "url": pwa_server["url"]}])
    page.goto(pwa_server["url"] + "/")
    page.wait_for_function("!!navigator.serviceWorker.controller")
    context.set_offline(True)
    assert page.evaluate("fetch('/api/auth/session').then(() => false, () => true)")
    assert page.evaluate(
        "fetch('/api/action', {method: 'POST', body: '{}'}).then(() => false, () => true)"
    )
    context.set_offline(False)
    page.locator("#btnLogout").click()
    expect(page.locator("#loginForm")).to_be_visible()
    page.goto(pwa_server["url"] + "/")
    expect(page.locator("#loginForm")).to_be_visible()
    cached = page.evaluate("""async () => {
      const result = [];
      for (const name of await caches.keys()) result.push(...(await (await caches.open(name)).keys()).map(r => r.url));
      return result;
    }""")
    assert not any("/api/" in url or "index.html" in url or "/login" in url for url in cached)


def test_offline_layout_mobile_and_desktop(page, pwa_server, tmp_path):
    ready(page, pwa_server)
    page.context.set_offline(True)
    page.goto(pwa_server["url"] + "/")
    for width, height in [(1440, 900), (390, 844), (320, 640)]:
        page.set_viewport_size({"width": width, "height": height})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator("#pwaRetry").bounding_box()["height"] >= 44
        page.screenshot(path=str(tmp_path / f"offline-{width}.png"), full_page=True)
    page.context.set_offline(False)
