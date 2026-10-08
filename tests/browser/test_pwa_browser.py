"""Exercise the shipped worker through the real HTTP handler, without live PZ operations."""

import shutil
from http.server import ThreadingHTTPServer
from threading import Event, Thread
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
            if path == "/static/offline.html" and hasattr(self.server, "precache_gate"):
                self.server.precache_gate.wait(timeout=5)
            if path == "/static/offline.html" and getattr(self.server, "shell_unavailable", False):
                self._send_error_json(503, "Shell unavailable")
                return
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
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
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
        if hasattr(server, "precache_gate"):
            server.precache_gate.set()
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
    expect(page.locator("#pwaUpdateNote")).to_contain_text("Сохраните введённые изменения")
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


def test_open_app_detects_update_within_a_minute(page, pwa_server):
    page.clock.install()
    ready(page, pwa_server)
    page.evaluate("async () => (await navigator.serviceWorker.getRegistration()).update()")
    page.wait_for_function(
        "navigator.serviceWorker.getRegistration().then(r => !r.installing && !r.waiting)"
    )
    offline = pwa_server["root"] / "offline.html"
    offline.write_text(
        offline.read_text(encoding="utf-8") + "\n<!-- next release -->", encoding="utf-8"
    )
    page.clock.fast_forward(60_001)
    expect(page.locator("#pwaUpdate")).to_be_visible(timeout=10000)


def test_registration_already_installing_still_announces_update(page, context, pwa_server):
    ready(page, pwa_server)
    page.evaluate("async () => (await navigator.serviceWorker.getRegistration()).update()")
    offline = pwa_server["root"] / "offline.html"
    offline.write_text(
        offline.read_text(encoding="utf-8") + "\n<!-- slow release -->", encoding="utf-8"
    )
    pwa_server["server"].precache_gate = Event()
    other = context.new_page()
    other.add_init_script("""(() => {
      const register = navigator.serviceWorker.register.bind(navigator.serviceWorker);
      navigator.serviceWorker.register = async (...args) => {
        const registration = await register(...args);
        await registration.update();
        // Expose the supported state where updatefound precedes the register callback.
        await new Promise(resolve => setTimeout(resolve, 100));
        window.registerReturnedInstalling = !!registration.installing;
        return registration;
      };
    })();""")
    other.goto(pwa_server["url"] + "/login")
    other.wait_for_function("window.registerReturnedInstalling === true")
    pwa_server["server"].precache_gate.set()
    other.wait_for_function("navigator.serviceWorker.getRegistration().then(r => !!r.waiting)")
    expect(other.locator("#pwaUpdate")).to_be_visible()


def test_update_loads_changed_dashboard_html_css_and_javascript(page, context, pwa_server):
    token = pwa_server["auth"].sign_in("pwa-admin", "pwa-test-password-long", "fixture")
    context.add_cookies([{"name": auth.COOKIE_NAME, "value": token, "url": pwa_server["url"]}])
    page.goto(pwa_server["url"] + "/#/settings")
    page.wait_for_function("!!navigator.serviceWorker.controller && !!window.ConfigEditor")
    expect(page.locator("#startupLoader")).to_be_hidden()
    # Drain the startup check before changing files: overlapping update() jobs can
    # share its unchanged worker response and miss the release published below.
    page.evaluate("async () => (await navigator.serviceWorker.getRegistration()).update()")
    page.wait_for_function(
        "navigator.serviceWorker.getRegistration().then(r => !r.installing && !r.waiting)"
    )
    old_caches = page.evaluate("caches.keys()")
    root = pwa_server["root"]
    html = root / "index.html"
    html.write_text(
        html.read_text(encoding="utf-8").replace("<body", '<body data-pwa-release="two"', 1),
        encoding="utf-8",
    )
    offline = root / "offline.html"
    offline.write_text(
        offline.read_text(encoding="utf-8").replace("<body", '<body data-pwa-release="two"', 1),
        encoding="utf-8",
    )
    for name, addition in [
        ("style.css", "\nbody { --pwa-dashboard-release: two; }"),
        ("app.js", '\ndocument.body.dataset.pwaAppRelease = "two";'),
        ("editor.js", '\ndocument.body.dataset.pwaEditorRelease = "two";'),
        ("pwa.css", "\nbody { --pwa-shell-release: two; }"),
        ("pwa.js", '\ndocument.body.dataset.pwaShellRelease = "two";'),
    ]:
        asset = root / name
        asset.write_text(asset.read_text(encoding="utf-8") + addition, encoding="utf-8")
    page.evaluate("async () => (await navigator.serviceWorker.getRegistration()).update()")
    expect(page.locator("#pwaUpdate")).to_be_visible(timeout=10000)
    assert page.locator("body").get_attribute("data-pwa-release") is None
    # A fresh window must receive the latest assets even while the old worker is active.
    fresh = context.new_page()
    fresh.goto(pwa_server["url"] + "/")
    expect(fresh.locator("body")).to_have_attribute("data-pwa-release", "two")
    expect(fresh.locator("body")).to_have_attribute("data-pwa-shell-release", "two")
    assert set(old_caches).issubset(page.evaluate("caches.keys()"))
    # The fresh-window check switched tabs; updating is a foreground user action.
    page.bring_to_front()
    with page.expect_navigation(wait_until="load"):
        page.locator("#pwaUpdate").click()
    assert page.url.endswith("/#/settings")
    for attribute in [
        "data-pwa-release",
        "data-pwa-app-release",
        "data-pwa-editor-release",
        "data-pwa-shell-release",
    ]:
        expect(page.locator("body")).to_have_attribute(attribute, "two")
    for prop in ["--pwa-dashboard-release", "--pwa-shell-release"]:
        assert (
            page.locator("body").evaluate(
                "(body, prop) => getComputedStyle(body).getPropertyValue(prop).trim()", prop
            )
            == "two"
        )
    page.wait_for_function("caches.keys().then(names => names.length === 1)")
    assert page.evaluate("caches.keys()") != old_caches
    context.set_offline(True)
    page.goto(pwa_server["url"] + "/")
    expect(page.locator("body")).to_have_attribute("data-pwa-release", "two")
    expect(page.locator("body")).to_have_attribute("data-pwa-shell-release", "two")
    assert (
        page.locator("body").evaluate(
            "body => getComputedStyle(body).getPropertyValue('--pwa-shell-release').trim()"
        )
        == "two"
    )
    context.set_offline(False)


def test_failed_update_preserves_active_version_and_can_retry(page, pwa_server):
    ready(page, pwa_server)
    page.evaluate("async () => (await navigator.serviceWorker.getRegistration()).update()")
    old_cache = page.evaluate("caches.keys()")[0]
    offline = pwa_server["root"] / "offline.html"
    offline.write_text(
        offline.read_text(encoding="utf-8") + "\n<!-- next release -->", encoding="utf-8"
    )
    pwa_server["server"].shell_unavailable = True
    page.evaluate("""async () => {
      const registration = await navigator.serviceWorker.getRegistration();
      window.failedWorker = null;
      registration.addEventListener('updatefound', () => {
        window.failedWorker = registration.installing;
      }, {once: true});
      await registration.update();
    }""")
    page.wait_for_function("window.failedWorker?.state === 'redundant'")
    expect(page.locator("#pwaUpdate")).to_be_hidden()
    assert old_cache in page.evaluate("caches.keys()")
    page.context.set_offline(True)
    page.goto(pwa_server["url"] + "/")
    expect(page.get_by_role("heading", name="Нет связи с пультом")).to_be_visible()
    pwa_server["server"].shell_unavailable = False
    page.context.set_offline(False)
    # The offline shell reconnects automatically; a simultaneous goto can abort it.
    expect(page.locator("#loginForm")).to_be_visible(timeout=15000)
    page.evaluate("async () => (await navigator.serviceWorker.getRegistration()).update()")
    expect(page.locator("#pwaUpdate")).to_be_visible(timeout=10000)
    with page.expect_navigation(wait_until="load"):
        page.locator("#pwaUpdate").click()
    page.wait_for_function("caches.keys().then(names => names.length === 1)")
    assert page.evaluate("caches.keys()") != [old_cache]


@pytest.mark.parametrize("width", [1440, 740, 600, 390, 320])
@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("motion", ["no-preference", "reduce"])
def test_update_alert_floats_without_moving_layout_and_stays_visible(
    page, context, pwa_server, tmp_path, width, language, motion
):
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion=motion)
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    token = pwa_server["auth"].sign_in("pwa-admin", "pwa-test-password-long", "fixture")
    context.add_cookies([{"name": auth.COOKIE_NAME, "value": token, "url": pwa_server["url"]}])
    page.goto(pwa_server["url"] + "/")
    page.wait_for_function("!!navigator.serviceWorker.controller && !!window.ConfigEditor")
    expect(page.locator("#startupLoader")).to_be_hidden()
    page.evaluate(
        "async () => { await document.fonts.ready; await (await navigator.serviceWorker.getRegistration()).update(); }"
    )
    # The view entrance translates its heading independently of the update alert.
    # Capture the baseline only after it settles; keep exact geometry assertions.
    page.locator("#view-overview").evaluate("""async element => {
      await Promise.all(element.getAnimations().map(animation => animation.finished));
    }""")
    page.locator("#btnCommands").focus()
    geometry = """() => ['.topbar', '.main', '#view-overview h1', '.nav', '.site-footer'].map(selector => {
      const box = document.querySelector(selector).getBoundingClientRect();
      return [box.x, box.y, box.width, box.height];
    })"""
    before = page.evaluate(geometry)
    offline = pwa_server["root"] / "offline.html"
    offline.write_text(
        offline.read_text(encoding="utf-8") + "\n<!-- alert release -->", encoding="utf-8"
    )
    page.evaluate("async () => (await navigator.serviceWorker.getRegistration()).update()")
    alert = page.locator(".pwa-update-alert")
    expect(alert).to_be_visible(timeout=10000)
    # Animated translation can round a 44px bounding box slightly below 44px.
    # Measure the settled alert, including when reduced motion disables animation.
    alert.evaluate("""async element => {
      await Promise.all(element.getAnimations({subtree: true}).map(animation => animation.finished));
    }""")
    expect(page.locator(".pwa-bar")).to_be_hidden()
    expect(page.locator("#btnCommands")).to_be_focused()
    assert page.evaluate(geometry) == before
    assert alert.evaluate("element => getComputedStyle(element).position") == "fixed"
    box = alert.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width
    assert box["y"] >= before[0][1] + before[0][3]
    assert box["y"] + box["height"] < 900 - before[4][3]
    assert page.locator("#pwaUpdate").bounding_box()["height"] >= 44
    expect(alert.get_by_role("button")).to_have_count(1)
    assert alert.locator(".pwa-update-message").evaluate(
        "element => getComputedStyle(element, '::before').content"
    ) in ("none", "normal")
    if width <= 740:
        assert abs(box["x"] + box["width"] / 2 - width / 2) < 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if language == "en":
        assert "New version available" in alert.inner_text()
    page.screenshot(
        path=str(tmp_path / f"update-alert-{language}-{width}.png"), animations="disabled"
    )
    if width <= 740:
        page.evaluate("toast('PWA companion notification', 'ok', 120000)")
        page.wait_for_function("""() => {
          const toast = document.querySelector('.toast').getBoundingClientRect();
          const alert = document.querySelector('.pwa-update-alert').getBoundingClientRect();
          return toast.bottom < alert.top;
        }""")
    if width == 320:
        page.context.set_offline(True)
        expect(page.locator("#pwaUpdate")).to_be_disabled()
        expect(page.locator("#pwaUpdateNote")).to_be_visible()
        page.context.set_offline(False)
        expect(page.locator("#pwaUpdate")).to_be_enabled()
    assert page.evaluate("navigator.serviceWorker.getRegistration().then(r => !!r.waiting)")
    page.evaluate("window.dispatchEvent(new Event('online'))")
    expect(alert).to_be_visible()
    page.locator("#pwaUpdate").focus()
    page.keyboard.press("Escape")
    expect(alert).to_be_visible()
    expect(page.locator("#pwaUpdate")).to_be_focused()


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


@pytest.mark.browser_context_args(service_workers="block")
@pytest.mark.parametrize("width", [1667, 1440, 741, 740, 390, 320])
def test_install_bar_stays_clear_of_navigation(page, dashboard, tmp_path, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(dashboard["url"])
    page.evaluate("""() => {
      window.installCalls = 0;
      const event = new Event('beforeinstallprompt', {cancelable: true});
      event.prompt = async () => { window.installCalls++; };
      event.userChoice = Promise.resolve({outcome: 'dismissed'});
      window.dispatchEvent(event);
    }""")
    button = page.locator("#pwaInstall")
    expect(button).to_be_visible()
    page.screenshot(path=str(tmp_path / f"install-bar-{width}.png"))
    nav = page.locator(".nav").bounding_box()
    bar = page.locator(".pwa-bar").bounding_box()
    if width >= 741:
        assert bar["x"] >= nav["x"] + nav["width"]
        heading = page.locator("#view-overview h1").bounding_box()
        message = page.locator(".pwa-message").bounding_box()
        assert abs(message["x"] - heading["x"]) <= 1
    else:
        assert bar["y"] + bar["height"] <= nav["y"]
        assert bar["x"] == 0
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    box = button.bounding_box()
    assert page.evaluate(
        """({x, y}) => !!document.elementFromPoint(x, y)?.closest('#pwaInstall')""",
        {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2},
    )
    button.click()
    assert page.evaluate("window.installCalls") == 1
    expect(page.locator(".pwa-bar")).to_be_hidden()
