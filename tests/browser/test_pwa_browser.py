"""Exercise the shipped worker through the real HTTP handler, without live PZ operations."""

import shutil
from http.server import ThreadingHTTPServer
from threading import Event, Thread
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect

import app
import auth

from browser_evidence import capture_evidence
from test_editors import editing, editor, env  # noqa: F401

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
            # Model a reverse proxy that retains each CSS/JS URL across releases.
            asset_cache = getattr(self.server, "asset_cache", None)
            if (
                asset_cache is not None
                and path.startswith("/static/")
                and path.endswith((".js", ".css"))
            ):
                body = asset_cache.setdefault(
                    self.path, (root / path.removeprefix("/static/")).read_bytes()
                )
                self.send_response(200)
                self.send_header(
                    "Content-Type", "text/javascript" if path.endswith(".js") else "text/css"
                )
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(body)
                return
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
    expect(page.locator("#pwaUpdate")).to_be_hidden()


def controlled_update(page, *, controlled=True, waiting=True, language="ru"):
    """Control browser lifecycle boundaries that real workers cannot reliably delay."""
    page.clock.install()
    page.add_init_script(f"localStorage.setItem('pz-language', '{language}')")
    script = """(() => {
      sessionStorage.pwaTestLoads = Number(sessionStorage.pwaTestLoads || 0) + 1;
      const registration = new EventTarget();
      let controller = __CONTROLLED__ ? {scriptURL: location.origin + '/sw.js'} : null;
      const lifecycle = window.pwaLifecycle = {
        registration, messages: [], mode: 'hold', disappear: false,
        worker(name) {
          const worker = new EventTarget();
          worker.state = 'installed';
          worker.scriptURL = location.origin + '/sw.js';
          worker.postMessage = message => {
            lifecycle.messages.push({name, type: message.type});
            if (lifecycle.mode === 'throw') throw new Error('Activation transport failed');
            if (lifecycle.disappear) lifecycle.activating(worker);
            if (lifecycle.mode === 'activate') queueMicrotask(() => lifecycle.activate(worker));
          };
          return worker;
        },
        offer(name) {
          registration.waiting = lifecycle.worker(name);
          registration.installing = registration.waiting;
          registration.dispatchEvent(new Event('updatefound'));
          registration.waiting.dispatchEvent(new Event('statechange'));
          registration.installing = null;
          return registration.waiting;
        },
        activating(worker = lifecycle.current) {
          registration.waiting = null;
          worker.state = 'activating';
          worker.dispatchEvent(new Event('statechange'));
        },
        activate(worker = lifecycle.current) {
          registration.waiting = null;
          registration.active = controller = worker;
          worker.state = 'activated';
          worker.dispatchEvent(new Event('statechange'));
          navigator.serviceWorker.dispatchEvent(new Event('controllerchange'));
        },
        redundant(worker = lifecycle.current) {
          if (registration.waiting === worker) registration.waiting = null;
          worker.state = 'redundant';
          worker.dispatchEvent(new Event('statechange'));
        },
      };
      lifecycle.current = lifecycle.worker('one');
      registration.waiting = __WAITING__ ? lifecycle.current : null;
      registration.installing = null;
      registration.active = controller;
      registration.update = async () => {};
      Object.defineProperty(navigator.serviceWorker, 'controller', {get: () => controller});
      navigator.serviceWorker.register = async () => registration;
      navigator.serviceWorker.getRegistration = async () => registration;
    })();"""
    page.add_init_script(
        script.replace("__CONTROLLED__", str(controlled).lower()).replace(
            "__WAITING__", str(waiting).lower()
        )
    )


def pause_update_clock(page):
    page.clock.pause_at(page.evaluate("Date.now() / 1000 + 1"))


def assert_not_reloaded(page):
    assert page.evaluate("Number(sessionStorage.pwaTestLoads)") == 1


def finish_controlled_update(page):
    page.evaluate("pwaLifecycle.mode = 'activate'")
    with page.expect_navigation(wait_until="load"):
        page.locator("#pwaUpdate").click()
    assert page.evaluate("Number(sessionStorage.pwaTestLoads)") == 2


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
    expect(page.locator("#pwaUpdateNote")).to_contain_text("Завершите вход или очистите пароль")
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
    # The fresh window registers the worker and starts its own update check.
    # Finish that job before asking the original tab to activate the waiting worker.
    expect(fresh.locator("#pwaUpdate")).to_be_visible()
    fresh.evaluate("async () => (await navigator.serviceWorker.getRegistration()).update()")
    fresh.wait_for_function(
        "navigator.serviceWorker.getRegistration().then(r => !r.installing && r.waiting?.state === 'installed')"
    )
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
    capture_evidence(
        page, path=str(tmp_path / f"update-alert-{language}-{width}.png"), animations="disabled"
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
    capture_evidence(page, path=str(tmp_path / "install-ios-390.png"), full_page=True)
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
        capture_evidence(page, path=str(tmp_path / f"offline-{width}.png"), full_page=True)
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
    # The shell is visible but remains inert while the loader exits.
    expect(page.locator("#startupLoader")).to_be_hidden()
    capture_evidence(page, path=str(tmp_path / f"install-bar-{width}.png"))
    page.locator("#view-overview").evaluate("""async element => {
      await Promise.all(element.getAnimations().map(animation => animation.finished));
    }""")
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


def test_activation_transport_error_can_retry_without_stale_timeout(page, pwa_server):
    controlled_update(page)
    page.goto(pwa_server["url"] + "/login")
    pause_update_clock(page)
    page.evaluate("pwaLifecycle.mode = 'throw'")
    page.locator("#pwaUpdate").click()
    alert = page.locator(".pwa-update-alert")
    expect(alert).to_have_attribute("aria-busy", "false")
    expect(page.locator("#pwaUpdateNote")).to_contain_text("Повторите попытку")
    expect(page.locator("#pwaUpdate")).to_be_enabled()
    assert_not_reloaded(page)
    page.clock.run_for(5000)
    page.evaluate("pwaLifecycle.mode = 'hold'")
    page.locator("#pwaUpdate").click()
    expect(alert).to_have_attribute("aria-busy", "true")
    page.clock.run_for(10001)
    expect(alert).to_have_attribute("aria-busy", "true")
    assert page.evaluate("pwaLifecycle.messages.length") == 2
    with page.expect_navigation(wait_until="load"):
        page.evaluate("pwaLifecycle.activate()")
    assert page.evaluate("Number(sessionStorage.pwaTestLoads)") == 2


@pytest.mark.parametrize("disappear", [False, True])
def test_activation_timeout_retains_offer_and_can_retry(page, pwa_server, disappear):
    controlled_update(page)
    page.goto(pwa_server["url"] + "/login")
    pause_update_clock(page)
    page.evaluate("value => pwaLifecycle.disappear = value", disappear)
    page.locator("#pwaUpdate").click()
    alert = page.locator(".pwa-update-alert")
    expect(alert).to_be_visible()
    expect(alert).to_have_attribute("aria-busy", "true")
    expect(page.locator("#pwaUpdate")).to_be_disabled()
    expect(alert.locator(".pwa-update-message")).to_have_text("Применяем обновление…")
    page.clock.run_for(15001)
    expect(alert).to_be_visible()
    expect(alert).to_have_attribute("aria-busy", "false")
    expect(page.locator("#pwaUpdate")).to_be_enabled()
    expect(page.locator("#pwaUpdate")).to_have_accessible_name("Повторить обновление приложения")
    assert_not_reloaded(page)
    if disappear:
        page.evaluate("pwaLifecycle.current = pwaLifecycle.offer('replacement')")
    finish_controlled_update(page)


def test_late_activation_requires_explicit_reload_after_timeout(page, pwa_server):
    controlled_update(page)
    page.goto(pwa_server["url"] + "/login")
    pause_update_clock(page)
    page.evaluate("pwaLifecycle.disappear = true")
    page.locator("#pwaUpdate").click()
    page.clock.run_for(15001)
    page.evaluate("pwaLifecycle.activate()")
    expect(page.locator("#pwaUpdate")).to_contain_text("Перезагрузить")
    expect(page.locator(".pwa-update-alert")).to_have_attribute("aria-busy", "false")
    assert_not_reloaded(page)
    finish_controlled_update(page)


def test_input_entered_during_activation_is_preserved(page, pwa_server):
    controlled_update(page)
    page.goto(pwa_server["url"] + "/login")
    pause_update_clock(page)
    page.locator("#pwaUpdate").click()
    page.locator("#password").fill("entered-while-activating")
    page.evaluate("pwaLifecycle.activate()")
    expect(page.locator("#password")).to_have_value("entered-while-activating")
    expect(page.locator("#pwaUpdateNote")).to_contain_text("очистите пароль")
    expect(page.locator("#pwaUpdate")).to_be_enabled()
    assert_not_reloaded(page)
    page.locator("#password").fill("")
    finish_controlled_update(page)


def test_offline_activation_preserves_page_until_explicit_online_retry(page, pwa_server):
    controlled_update(page)
    page.goto(pwa_server["url"] + "/login")
    pause_update_clock(page)
    page.locator("#pwaUpdate").click()
    page.context.set_offline(True)
    page.evaluate("pwaLifecycle.activate()")
    expect(page.locator(".pwa-update-alert")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#pwaUpdate")).to_be_disabled()
    expect(page.locator("#pwaUpdateNote")).to_contain_text("Нет сети")
    assert_not_reloaded(page)
    page.context.set_offline(False)
    expect(page.locator("#pwaUpdate")).to_be_enabled()
    assert_not_reloaded(page)
    finish_controlled_update(page)


def test_redundant_worker_retry_uses_current_registration(page, pwa_server):
    controlled_update(page)
    page.goto(pwa_server["url"] + "/login")
    pause_update_clock(page)
    page.locator("#pwaUpdate").click()
    page.evaluate("pwaLifecycle.redundant()")
    expect(page.locator("#pwaUpdate")).to_be_enabled()
    expect(page.locator(".pwa-update-alert")).to_have_attribute("aria-busy", "false")
    page.evaluate("""() => {
      pwaLifecycle.registration.update = async () => {
        pwaLifecycle.current = pwaLifecycle.offer('replacement');
      };
    }""")
    finish_controlled_update(page)


def test_initial_install_does_not_offer_update_but_other_tab_activation_does(page, pwa_server):
    controlled_update(page, controlled=False, waiting=False)
    page.goto(pwa_server["url"] + "/login")
    pause_update_clock(page)
    expect(page.locator("#pwaUpdate")).to_be_hidden()
    page.evaluate("pwaLifecycle.activate()")
    expect(page.locator("#pwaUpdate")).to_be_hidden()
    page.locator("#password").fill("other-tab-must-preserve-this")
    page.evaluate("pwaLifecycle.current = pwaLifecycle.offer('other-tab'); pwaLifecycle.activate()")
    expect(page.locator("#pwaUpdate")).to_be_visible()
    expect(page.locator("#password")).to_have_value("other-tab-must-preserve-this")
    assert_not_reloaded(page)


def test_different_controller_does_not_complete_pending_attempt(page, pwa_server):
    controlled_update(page)
    page.goto(pwa_server["url"] + "/login")
    pause_update_clock(page)
    page.locator("#pwaUpdate").click()
    page.evaluate("""() => {
      pwaLifecycle.old = pwaLifecycle.current;
      pwaLifecycle.current = pwaLifecycle.worker('other-tab');
      pwaLifecycle.activate();
    }""")
    expect(page.locator(".pwa-update-alert")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#pwaUpdate")).to_contain_text("Перезагрузить")
    assert_not_reloaded(page)
    page.evaluate("pwaLifecycle.redundant(pwaLifecycle.old)")
    page.clock.run_for(15001)
    expect(page.locator("#pwaUpdate")).to_contain_text("Перезагрузить")
    expect(page.locator(".pwa-update-message")).to_have_text("Доступна новая версия")
    page.locator("#password").fill("keep-after-other-controller")
    page.locator("#pwaUpdate").click()
    expect(page.locator("#pwaUpdateNote")).to_contain_text("очистите пароль")
    assert_not_reloaded(page)
    page.locator("#password").fill("")
    finish_controlled_update(page)


@pytest.mark.parametrize("guard", ["external", "password", "login-pending"])
def test_update_respects_existing_cancelable_and_login_guards(page, pwa_server, guard):
    controlled_update(page)
    page.goto(pwa_server["url"] + "/login")
    if guard == "external":
        page.evaluate("""document.addEventListener('pz:before-update', event => {
          window.receivedCancelableGuard = event.cancelable;
          event.preventDefault();
        }, {once: true})""")
    elif guard == "password":
        page.locator("#password").fill("must-not-discard")
    else:
        page.locator("#loginSubmit").evaluate("button => button.disabled = true")
    page.locator("#pwaUpdate").click()
    expect(page.locator("#pwaUpdateNote")).to_be_visible()
    expect(page.locator(".pwa-update-alert")).to_have_attribute("aria-busy", "false")
    assert page.evaluate("pwaLifecycle.messages.length") == 0
    assert_not_reloaded(page)
    if guard == "external":
        assert page.evaluate("window.receivedCancelableGuard")
        expect(page.locator("#pwaUpdateNote")).to_contain_text("защиты вашей работы")
    elif guard == "password":
        expect(page.locator("#pwaUpdateNote")).to_contain_text("очистите пароль")
    else:
        expect(page.locator("#pwaUpdateNote")).to_contain_text("вход")


@pytest.mark.browser_context_args(service_workers="block")
def test_workshop_entry_blocks_update_until_cleared_or_submitted(page, dashboard, editing):  # noqa: F811
    page.goto(dashboard["url"] + "/#/mods")
    expect(page.locator("#workshopInput")).to_be_enabled()
    page.wait_for_function("ConfigEditor.ready")
    allowed = (
        "() => document.dispatchEvent(new CustomEvent('pz:before-update', {cancelable: true}))"
    )
    assert page.evaluate(allowed)
    page.locator("#modQuery").fill("library")
    page.locator("#modFilter").select_option("selected")
    assert page.evaluate(allowed)
    page.locator("#workshopInput").fill("111")
    assert not page.evaluate(allowed)
    page.locator("#workshopInput").fill("")
    assert page.evaluate(allowed)
    page.route(
        "**/api/workshop-resolve",
        lambda route: route.fulfill(json={"ok": True, "items": [{"workshopId": "111"}]}),
    )
    page.locator("#workshopInput").fill("111")
    page.locator("#workshopInput").press("Enter")
    expect(page.locator("#workshopInput")).to_have_value("")
    expect(page.locator("#workshopInput")).to_be_enabled()
    assert page.evaluate(allowed)


@pytest.mark.parametrize(
    "language,width,motion",
    [("ru", 320, "reduce"), ("en", 390, "no-preference"), ("en", 1440, "reduce")],
)
def test_update_retry_and_blocked_copy_remain_accessible(page, pwa_server, language, width, motion):
    controlled_update(page, language=language)
    page.set_viewport_size({"width": width, "height": 550})
    page.emulate_media(reduced_motion=motion)
    page.goto(pwa_server["url"] + "/login")
    pause_update_clock(page)
    action = page.locator("#pwaUpdate")
    alert = page.locator(".pwa-update-alert")
    expect(action).to_have_text("Обновить" if language == "ru" else "Update")
    action.click()
    expect(action).to_have_text("Обновляем…" if language == "ru" else "Updating…")
    page.clock.run_for(15001)
    expect(action).to_be_enabled()
    expect(action).to_have_text("Повторить" if language == "ru" else "Retry")
    expect(alert.locator(".pwa-update-message")).to_have_attribute("role", "status")
    expect(alert.locator(".pwa-update-message")).to_have_attribute("aria-live", "polite")
    expect(action).to_have_accessible_name(
        "Повторить обновление приложения" if language == "ru" else "Retry application update"
    )
    expect(action).to_have_attribute("aria-describedby", "pwaUpdateNote")
    assert (
        page.evaluate("""async () => {
      let announcements = 0;
      const observer = new MutationObserver(records => announcements += records.length);
      document.querySelectorAll('.pwa-update-alert [role="status"]').forEach(element => {
        observer.observe(element, {childList: true, characterData: true, subtree: true});
      });
      window.dispatchEvent(new Event('online'));
      await Promise.resolve();
      observer.disconnect();
      return announcements;
    }""")
        == 0
    )
    if language == "ru":
        page.locator("[data-language-switch]").select_option("en")
        expect(action).to_have_accessible_name("Retry application update")
        expect(action).to_have_text("Retry")
        expect(alert.locator(".pwa-update-message")).to_have_text("Update not completed")
        page.locator("[data-language-switch]").select_option("ru")
    page.evaluate(
        "document.addEventListener('pz:before-update', event => event.preventDefault(), {once: true})"
    )
    action.click()
    expect(page.locator("#pwaUpdateNote")).to_be_visible()
    expect(action).to_be_focused()
    expect(alert).to_have_attribute("aria-busy", "false")
    assert_not_reloaded(page)
    box = alert.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width
    assert box["y"] >= 0 and box["y"] + box["height"] <= 550
    assert action.bounding_box()["height"] >= 44
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if language == "en":
        assert "Сохраните" not in alert.inner_text()
