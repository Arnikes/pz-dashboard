"use strict";

(() => {
  const retry = document.getElementById("pwaRetry");
  const standalone = () => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  let registration = null;
  let installPrompt = null;
  let reloading = false;
  const activationTimeout = 15000;
  let updateAttempt = null;
  let updateFailed = false;
  let controlled = !!navigator.serviceWorker?.controller;
  let controllerChanged = false;
  let notice = "";
  let updateBlocked = "";

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
  const { element: updateAlert, action: update, message: updateMessage, note: updateNote } = FloatingAlerts.create({
    className: "pwa-update-alert", actionId: "pwaUpdate", noteId: "pwaUpdateNote",
    icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M5 12h14m-6-6 6 6-6 6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  });
  const waitingWorker = () => controlled && registration?.waiting?.state !== "redundant" && registration?.waiting;
  const updateToken = () => waitingWorker() || (controllerChanged && navigator.serviceWorker.controller);
  help.id = "pwaInstallHelp";
  install.setAttribute("aria-controls", help.id);
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

  function render() {
    bar.setAttribute("aria-label", I18n.t("Приложение PZ Пульт"));
    install.textContent = I18n.t("Установить приложение");
    updateAlert.setAttribute("aria-label", I18n.t("Обновление приложения"));
    const applying = !!updateAttempt;
    const reloadReady = controllerChanged && !waitingWorker();
    update.setAttribute("aria-label", I18n.t(applying ? "Применяем обновление приложения" : reloadReady ? "Перезагрузить приложение" : updateFailed ? "Повторить обновление приложения" : "Обновить приложение"));
    update.querySelector("span").textContent = I18n.t(applying ? "Обновляем…" : reloadReady ? "Перезагрузить" : updateFailed ? "Повторить" : "Обновить пульт");
    const statusMessage = I18n.t(applying ? "Применяем обновление…" : updateFailed ? "Обновление не завершено" : "Доступна новая версия");
    if (updateMessage.textContent !== statusMessage) updateMessage.textContent = statusMessage;
    const canInstall = !standalone() && (!!installPrompt || (ios && window.isSecureContext));
    const token = updateToken();
    const hasUpdate = !!token || applying || updateFailed;
    install.hidden = !canInstall;
    updateAlert.hidden = !!retry || !hasUpdate;
    updateAlert.setAttribute("aria-busy", String(applying));
    update.disabled = !navigator.onLine || applying;
    updateNote.hidden = navigator.onLine && !updateBlocked && !updateFailed;
    const noteMessage = !navigator.onLine
      ? I18n.t("Нет сети. Восстановите связь и повторите действие.")
      : updateBlocked ? blockedMessage(updateBlocked)
      : updateFailed ? I18n.t("Не удалось применить новую версию. Повторите попытку; введённые данные сохранены.") : "";
    if (updateNote.textContent !== noteMessage) updateNote.textContent = noteMessage;
    if (updateNote.hidden) update.removeAttribute("aria-describedby");
    else update.setAttribute("aria-describedby", updateNote.id);
    bar.hidden = !!retry || (!canInstall && !notice && (navigator.onLine || !!document.querySelector(".topbar")));
    message.textContent = !navigator.onLine && !document.querySelector(".topbar")
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
  window.addEventListener("offline", () => { notice = ""; if (updateAttempt) failAttempt(updateAttempt); render(); });
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

  function blockedMessage(reason) {
    if (reason === "login") return I18n.t("Завершите вход или очистите пароль, затем повторите обновление.");
    if (reason === "signing-in") return I18n.t("Дождитесь завершения входа, затем повторите обновление.");
    if (reason === "operation") return I18n.t("Дождитесь завершения операции или сохранения, затем повторите обновление.");
    if (reason === "unsaved") return I18n.t("Сохраните или очистите введённые изменения, затем повторите обновление.");
    if (reason === "dialog") return I18n.t("Завершите действие в открытом окне, затем повторите обновление.");
    return I18n.t("Сейчас обновление отложено для защиты вашей работы. Завершите текущее действие и повторите попытку.");
  }

  function clearBlockedFeedback() {
    if (!updateBlocked) return;
    updateBlocked = "";
    render();
  }
  // Feedback describes the last check. Known state changes clear it without
  // repeatedly dispatching the cancelable event to third-party guards.
  document.addEventListener("input", clearBlockedFeedback);
  document.addEventListener("change", clearBlockedFeedback);
  document.addEventListener("pz:update-guard-changed", clearBlockedFeedback);
  document.addEventListener("close", clearBlockedFeedback, true);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") clearBlockedFeedback(); });
  document.addEventListener("click", (event) => { if (!updateAlert.contains(event.target)) clearBlockedFeedback(); });

  function canReload() {
    const detail = { reasons: [] };
    const event = new CustomEvent("pz:before-update", { cancelable: true, detail });
    document.dispatchEvent(event);
    const password = document.getElementById("password");
    const signingIn = document.getElementById("loginSubmit")?.disabled;
    if (event.defaultPrevented || password?.value || signingIn) {
      updateBlocked = signingIn ? "signing-in" : password?.value ? "login"
        : ["operation", "unsaved", "dialog"].find(reason => detail.reasons.includes(reason)) || "external";
      render();
      return false;
    }
    return true;
  }

  function reload() {
    if (reloading || !navigator.onLine || !canReload()) { render(); return; }
    reloading = true;
    location.reload();
  }

  function stopAttempt(attempt) {
    if (updateAttempt !== attempt) return false;
    clearTimeout(attempt.timer);
    attempt.worker?.removeEventListener("statechange", attempt.onStateChange);
    updateAttempt = null;
    return true;
  }

  function failAttempt(attempt) {
    if (!stopAttempt(attempt)) return;
    updateFailed = true;
    render();
  }

  function completeAttempt(attempt) {
    if (navigator.serviceWorker.controller !== attempt.worker || !stopAttempt(attempt)) return;
    controllerChanged = true;
    updateFailed = false;
    reload();
  }

  function watchAttempt(attempt, worker) {
    if (updateAttempt !== attempt || attempt.worker) return;
    attempt.worker = worker;
    attempt.onStateChange = () => {
      if (updateAttempt !== attempt) return;
      if (worker.state === "redundant") { failAttempt(attempt); return; }
      if (worker === navigator.serviceWorker.controller) { completeAttempt(attempt); return; }
      if (worker.state === "installed" && !attempt.sent) {
        attempt.sent = true;
        try { worker.postMessage({ type: "SKIP_WAITING" }); }
        catch { failAttempt(attempt); }
      }
    };
    worker.addEventListener("statechange", attempt.onStateChange);
    attempt.onStateChange();
  }

  update.addEventListener("click", async () => {
    if (updateAttempt || reloading || !navigator.onLine || !canReload()) return;
    notice = "";
    updateBlocked = "";
    updateFailed = false;
    if (!waitingWorker() && controllerChanged) { reload(); return; }
    const attempt = { worker: null, sent: false, onStateChange: null, timer: null };
    updateAttempt = attempt;
    attempt.timer = setTimeout(() => failAttempt(attempt), activationTimeout);
    render();
    const worker = waitingWorker() || registration?.installing;
    if (worker) { watchAttempt(attempt, worker); return; }
    try {
      // A redundant worker is no longer retryable. Ask for the current release
      // instead of sending another message to the failed worker.
      await registration?.update();
      if (updateAttempt !== attempt) return;
      const replacement = waitingWorker() || registration?.installing;
      if (replacement) watchAttempt(attempt, replacement);
      else failAttempt(attempt);
    } catch { failAttempt(attempt); }
  });

  let lastCheck = 0;
  async function checkUpdate() {
    if (!registration || !navigator.onLine || document.hidden || Date.now() - lastCheck < 60000) return;
    lastCheck = Date.now();
    try { await registration.update(); } catch { /* Retry on the next foreground check. */ }
  }

  if ("serviceWorker" in navigator && window.isSecureContext && location.protocol !== "file:") {
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      // Only the tab that requested an update may reload; other tabs keep their input.
      if (controlled || updateAttempt) controllerChanged = true;
      controlled = true;
      if (updateAttempt?.worker === navigator.serviceWorker.controller) completeAttempt(updateAttempt);
      else {
        if (updateAttempt) stopAttempt(updateAttempt);
        updateFailed = false;
        render();
      }
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
