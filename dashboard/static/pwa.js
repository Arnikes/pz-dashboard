"use strict";

(() => {
  const retry = document.getElementById("pwaRetry");
  const standalone = () => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  let registration = null;
  let installPrompt = null;
  let reloading = false;
  let updateRequested = false;
  let controllerChanged = false;
  let notice = "";

  async function reconnect() {
    if (retry.disabled) return;
    retry.disabled = true;
    const status = document.getElementById("pwaRetryStatus");
    status.textContent = "Проверяем соединение…";
    try {
      const response = await fetch("/api/health", { cache: "no-store", signal: AbortSignal.timeout(5000) });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error("unavailable");
      if (location.pathname === "/static/offline.html") location.replace("/" + location.hash);
      else location.reload();
    } catch {
      status.textContent = "Пульт пока недоступен. Проверьте подключение и повторите попытку.";
      retry.disabled = false;
    }
  }
  if (retry) {
    retry.addEventListener("click", reconnect);
    window.addEventListener("online", reconnect);
    // VPN/server recovery need not fire an OS online event. Check only this shell,
    // never reload the live dashboard or replay a command.
    setInterval(() => { if (navigator.onLine && !document.hidden) reconnect(); }, 5000);
  }

  const bar = document.createElement("aside");
  bar.className = "pwa-bar";
  bar.setAttribute("aria-label", "Приложение PZ Пульт");
  bar.hidden = true;
  bar.innerHTML = '<p class="pwa-message" role="status" aria-live="polite"></p><button class="pwa-button" id="pwaInstall" type="button" hidden>Установить приложение</button><button class="pwa-button" id="pwaUpdate" type="button" hidden>Обновить приложение</button><p class="pwa-help" hidden></p>';
  if (!retry) {
    const topbar = document.querySelector(".topbar");
    if (topbar) topbar.after(bar);
    else document.body.append(bar);
  }
  const message = bar.querySelector(".pwa-message");
  const install = bar.querySelector("#pwaInstall");
  const update = bar.querySelector("#pwaUpdate");
  const help = bar.querySelector(".pwa-help");
  help.id = "pwaInstallHelp";
  install.setAttribute("aria-controls", help.id);
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

  function render() {
    const canInstall = !standalone() && (!!installPrompt || (ios && window.isSecureContext));
    const hasUpdate = !!registration?.waiting || controllerChanged;
    install.hidden = !canInstall;
    update.hidden = !hasUpdate;
    update.disabled = !navigator.onLine || updateRequested;
    bar.hidden = retry || (navigator.onLine && !canInstall && !hasUpdate && !notice);
    message.textContent = !navigator.onLine
      ? "Нет сети. Последние данные могут устареть; команды недоступны. Ввод остаётся в открытом окне."
      : notice || (hasUpdate ? "Доступна новая версия пульта. Сохраните изменения перед обновлением." : "Пульт можно открыть отдельным приложением.");
  }

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    installPrompt = event;
    render();
  });
  window.addEventListener("appinstalled", () => { installPrompt = null; help.hidden = true; notice = ""; render(); });
  window.matchMedia("(display-mode: standalone)").addEventListener("change", render);
  window.addEventListener("offline", () => { notice = ""; render(); });
  window.addEventListener("online", () => { notice = ""; render(); checkUpdate(); });

  install.addEventListener("click", async () => {
    if (!installPrompt) {
      help.textContent = "На iPhone и iPad откройте пульт в Safari: «Поделиться» → «На экран Домой» → «Добавить». Если пункта нет, раскройте список действий.";
      help.hidden = !help.hidden;
      install.setAttribute("aria-expanded", String(!help.hidden));
      return;
    }
    const prompt = installPrompt;
    installPrompt = null;
    install.disabled = true;
    try {
      await prompt.prompt();
      await prompt.userChoice;
    } catch {
      notice = "Откройте меню браузера и выберите установку приложения.";
    } finally {
      install.disabled = false;
      render();
    }
  });

  function canReload() {
    const event = new CustomEvent("pz:before-update", { cancelable: true });
    document.dispatchEvent(event);
    const password = document.getElementById("password");
    if (event.defaultPrevented || (password && password.value) || document.getElementById("loginSubmit")?.disabled) {
      notice = "Сохраните введённые изменения и дождитесь завершения операции, затем повторите обновление.";
      render();
      return false;
    }
    return true;
  }

  function reload() {
    if (reloading || !canReload()) { updateRequested = false; render(); return; }
    reloading = true;
    location.reload();
  }
  update.addEventListener("click", () => {
    if (!navigator.onLine || !canReload()) return;
    notice = "";
    if (registration?.waiting) {
      updateRequested = true;
      render();
      registration.waiting.postMessage({ type: "SKIP_WAITING" });
    } else if (controllerChanged) reload();
  });

  let lastCheck = 0;
  async function checkUpdate() {
    if (!registration || !navigator.onLine || document.hidden || Date.now() - lastCheck < 60000) return;
    lastCheck = Date.now();
    try { await registration.update(); } catch { /* Retry on the next foreground check. */ }
  }

  if ("serviceWorker" in navigator && window.isSecureContext && location.protocol !== "file:") {
    let controlled = !!navigator.serviceWorker.controller;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      // Only the tab that requested an update may reload; other tabs keep their input.
      if (controlled || updateRequested) controllerChanged = true;
      controlled = true;
      if (updateRequested) reload();
      else render();
    });
    navigator.serviceWorker.register("/sw.js", { scope: "/", updateViaCache: "none" }).then((value) => {
      registration = value;
      registration.addEventListener("updatefound", () => {
        registration.installing?.addEventListener("statechange", render);
      });
      render();
      checkUpdate();
    }).catch(() => { /* The regular web app remains available if storage/registration is blocked. */ });
    document.addEventListener("visibilitychange", checkUpdate);
    window.addEventListener("pageshow", checkUpdate);
    setInterval(checkUpdate, 60 * 60 * 1000);
  }
  render();
})();
