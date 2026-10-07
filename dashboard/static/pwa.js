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
  let updateBlocked = false;

  async function reconnect() {
    if (retry.disabled) return;
    retry.disabled = true;
    const status = document.getElementById("pwaRetryStatus");
    status.textContent = I18n.t("Проверяем соединение…");
    try {
      const response = await fetch("/api/health", { cache: "no-store", signal: AbortSignal.timeout(5000) });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error("unavailable");
      if (location.pathname === "/static/offline.html") location.replace("/" + location.hash);
      else location.reload();
    } catch {
      status.textContent = I18n.t("Пульт пока недоступен. Проверьте подключение и повторите попытку.");
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
  bar.setAttribute("aria-label", I18n.t("Приложение PZ Пульт"));
  bar.hidden = true;
  bar.innerHTML = I18n.html('<p class="pwa-message" role="status" aria-live="polite"></p><button class="pwa-button" id="pwaInstall" type="button" hidden>Установить приложение</button><p class="pwa-help" hidden></p>');
  if (!retry) {
    const topbar = document.querySelector(".topbar");
    if (topbar) topbar.after(bar);
    else document.body.append(bar);
  }
  const message = bar.querySelector(".pwa-message");
  const install = bar.querySelector("#pwaInstall");
  const help = bar.querySelector(".pwa-help");
  const updateAlert = document.createElement("aside");
  updateAlert.className = "pwa-update-alert";
  updateAlert.hidden = true;
  updateAlert.innerHTML = '<p class="pwa-update-message" role="status" aria-live="polite"></p><button class="pwa-update-action" id="pwaUpdate" type="button"><span></span><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M5 12h14m-6-6 6 6-6 6" stroke-linecap="round" stroke-linejoin="round"/></svg></button><p class="pwa-update-note" id="pwaUpdateNote" role="status" aria-live="polite" hidden></p>';
  if (!retry) document.body.append(updateAlert);
  const update = updateAlert.querySelector("#pwaUpdate");
  const updateMessage = updateAlert.querySelector(".pwa-update-message");
  const updateNote = updateAlert.querySelector(".pwa-update-note");
  // Mobile notifications reserve space above the alert without changing page layout.
  if (!retry && "ResizeObserver" in window) {
    new ResizeObserver(() => {
      document.documentElement.style.setProperty("--pwa-update-clearance", `${updateAlert.hidden ? 0 : updateAlert.offsetHeight + 12}px`);
    }).observe(updateAlert);
  }
  const updateToken = () => registration?.waiting || (controllerChanged && navigator.serviceWorker.controller);
  help.id = "pwaInstallHelp";
  install.setAttribute("aria-controls", help.id);
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

  function render() {
    bar.setAttribute("aria-label", I18n.t("Приложение PZ Пульт"));
    install.textContent = I18n.t("Установить приложение");
    updateAlert.setAttribute("aria-label", I18n.t("Обновление приложения"));
    update.setAttribute("aria-label", I18n.t("Обновить приложение"));
    update.querySelector("span").textContent = I18n.t(updateRequested ? "Обновляем…" : "Обновить пульт");
    updateMessage.textContent = I18n.t("Доступна новая версия");
    const canInstall = !standalone() && (!!installPrompt || (ios && window.isSecureContext));
    const token = updateToken();
    const hasUpdate = !!token;
    install.hidden = !canInstall;
    updateAlert.hidden = !!retry || !hasUpdate;
    updateAlert.setAttribute("aria-busy", String(updateRequested));
    update.disabled = !navigator.onLine || updateRequested;
    updateNote.hidden = navigator.onLine && !updateBlocked;
    updateNote.textContent = !navigator.onLine
      ? I18n.t("Нет сети. Восстановите связь и повторите действие.")
      : updateBlocked ? I18n.t("Сохраните введённые изменения и дождитесь завершения операции, затем повторите обновление.") : "";
    if (updateNote.hidden) update.removeAttribute("aria-describedby");
    else update.setAttribute("aria-describedby", updateNote.id);
    bar.hidden = !!retry || (navigator.onLine && !canInstall && !notice);
    message.textContent = !navigator.onLine
      ? I18n.t("Нет сети. Последние данные могут устареть; команды недоступны. Ввод остаётся в открытом окне.")
      : notice || I18n.t("Пульт можно открыть отдельным приложением.");
  }

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    installPrompt = event;
    render();
  });
  document.addEventListener("pz:language-changed", () => {
    notice = "";
    help.hidden = true;
    const status = document.getElementById("pwaRetryStatus");
    if (status) status.textContent = "";
    render();
  });
  window.addEventListener("appinstalled", () => { installPrompt = null; help.hidden = true; notice = ""; render(); });
  window.matchMedia("(display-mode: standalone)").addEventListener("change", render);
  window.addEventListener("offline", () => { notice = ""; render(); });
  window.addEventListener("online", () => { notice = ""; render(); checkUpdate(); });

  install.addEventListener("click", async () => {
    if (!installPrompt) {
      help.textContent = I18n.t("На iPhone и iPad откройте пульт в Safari: «Поделиться» → «На экран Домой» → «Добавить». Если пункта нет, раскройте список действий.");
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
      notice = I18n.t("Откройте меню браузера и выберите установку приложения.");
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
      updateBlocked = true;
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
    updateBlocked = false;
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
      const watchInstalling = () => {
        registration.installing?.addEventListener("statechange", render);
      };
      registration.addEventListener("updatefound", watchInstalling);
      // Registration may resolve after updatefound, while precaching is still running.
      watchInstalling();
      render();
      checkUpdate();
    }).catch(() => { /* The regular web app remains available if storage/registration is blocked. */ });
    document.addEventListener("visibilitychange", checkUpdate);
    window.addEventListener("pageshow", checkUpdate);
    setInterval(checkUpdate, 60000);
  }
  render();
})();
