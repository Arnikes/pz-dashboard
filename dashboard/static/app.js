/* PZ Пульт · V19 — логика интерфейса.
   V19: подпись события «mods» в журнале; host-only автонастройки глушатся в
   remote-режиме; тикер свежести данных живёт и в SSE-режиме.
   Мультистраничный каркас: hash-роутинг (#/overview, #/mods, …), 7 страниц,
   SSE-поток /api/stream живёт между переключениями; при недоступности — опрос.
   При отсутствии API включается демо-режим.
   Режим «remote»: пульт вне хоста сервера — управление только по RCON. */
"use strict";

/* ───────────────────────── утилиты ───────────────────────── */

const $ = (id) => document.getElementById(id);

const esc = (s) => String(s ?? "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

const timeFmt = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" });
const dateFmt = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
const timeFullFmt = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const dayFmt = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit" });

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  return dateFmt.format(d);
}

function fmtUptime(sec) {
  if (sec == null) return "—";
  if (sec < 60) return `${sec} с`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m} мин`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} ч ${m % 60} мин`;
  return `${Math.floor(h / 24)} д ${h % 24} ч`;
}

function fmtBytes(n) {
  if (n == null) return "—";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let v = Number(n) || 0, u = 0;
  while (v >= 1024 && u < units.length - 1) { v /= 1024; u++; }
  return `${u === 0 ? v : v.toFixed(1)} ${units[u]}`;
}

const shortDigest = (d) => (d ? d.replace("sha256:", "").slice(0, 12) : "—");
const shortWsId = (id) => (id && String(id).length > 8 ? String(id).slice(0, 7) + "…" : String(id || "—"));

function relTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const s = Math.round((Date.now() - d.getTime()) / 1000);
  if (s < -45) {
    // будущее время (например, следующий запуск автобэкапа)
    const f = -s;
    if (f < 3600) return `через ${Math.max(1, Math.round(f / 60))} мин`;
    if (f < 86400) return `через ${Math.round(f / 3600)} ч`;
    return fmtTime(iso);
  }
  if (s < 45) return "только что";
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} мин назад`;
  if (s < 86400) return `${Math.round(s / 3600)} ч назад`;
  if (s < 172800) return "вчера";
  return fmtTime(iso);
}

function copyTextFallback(text) {
  const active = document.activeElement;
  const selection = window.getSelection();
  const ranges = selection
    ? Array.from({ length: selection.rangeCount }, (_, i) => selection.getRangeAt(i).cloneRange())
    : [];
  const field = document.createElement("textarea");
  field.value = text;
  field.readOnly = true;
  field.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none;font-size:16px";
  document.body.appendChild(field);
  try {
    field.focus({ preventScroll: true });
    field.select();
    if (!document.execCommand("copy")) throw new Error("Copy command failed");
  } finally {
    field.remove();
    if (active && active.isConnected) active.focus({ preventScroll: true });
    if (selection) {
      selection.removeAllRanges();
      ranges.forEach((range) => selection.addRange(range));
    }
  }
}

async function copyText(text, label) {
  try {
    // Clipboard API доступен на HTTPS и loopback, но отсутствует на обычном HTTP в LAN.
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
      } catch (e) {
        copyTextFallback(text);
      }
    } else {
      // Выполняем синхронно в обработчике клика, сохраняя пользовательскую активацию.
      copyTextFallback(text);
    }
    toast(`Скопировано: ${label || String(text).slice(0, 42)}`, "ok", 2000);
  } catch (e) {
    toast("Не удалось скопировать", "error", 2000);
  }
}

document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-copy]");
  if (t && t.dataset.copy) copyText(t.dataset.copy);
});

/* ───────────────────────── тосты ───────────────────────── */

function toast(text, kind = "info", ms = 5200) {
  const box = document.createElement("div");
  box.className = "toast";
  box.dataset.kind = kind;
  box.textContent = text;
  box.addEventListener("mouseenter", () => { box.dataset.hold = "1"; });
  box.addEventListener("mouseleave", () => { box.dataset.hold = ""; });
  $("toasts").appendChild(box);
  const kill = () => box.remove();
  const timer = setTimeout(() => { if (!box.dataset.hold) kill(); else box.addEventListener("transitionend", kill, { once: true }); }, ms);
  box.addEventListener("dblclick", () => { clearTimeout(timer); kill(); });
}

/* ───────────────────────── модальное окно ───────────────────────── */

const modal = (() => {
  const root = $("modalRoot");
  let onOk = null;
  let returnFocus = null;

  function open({ title, bodyHTML, okLabel = "Подтвердить", danger = false, onConfirm }) {
    returnFocus = document.activeElement;
    $("modalTitle").textContent = title;
    $("modalTitle").classList.toggle("danger", danger);
    $("modalBody").innerHTML = bodyHTML;
    const okBtn = $("modalOk");
    okBtn.disabled = false;
    okBtn.textContent = okLabel;
    okBtn.className = "btn " + (danger ? "solid-danger" : "primary");
    onOk = onConfirm || null;
    root.hidden = false;
    okBtn.focus();
  }

  function close() {
    root.hidden = true;
    $("modalBody").innerHTML = "";
    onOk = null;
    if (returnFocus?.isConnected && !returnFocus.disabled) returnFocus.focus({ preventScroll: true });
    returnFocus = null;
  }

  $("modalCancel").addEventListener("click", close);
  $("modalBackdrop").addEventListener("click", close);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !root.hidden) close(); });
  // фокус не покидает открытую модалку (Tab зациклен по её элементам)
  root.addEventListener("keydown", (e) => {
    if (root.hidden || e.key !== "Tab") return;
    const els = [...root.querySelectorAll("button, input, select, textarea, a[href]")]
      .filter((el) => !el.disabled && el.getClientRects().length);
    if (!els.length) return;
    const first = els[0], last = els[els.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  $("modalOk").addEventListener("click", async () => {
    if (!onOk) return close();
    const okBtn = $("modalOk");
    okBtn.disabled = true;
    try { await onOk(); close(); }
    catch (e) { toast(e.message || String(e), "error"); }
    finally { okBtn.disabled = false; }
  });

  return { open, close };
})();

/* ───────────────────────── API / демо-режим ───────────────────────── */

const S = {
  demo: false,
  overview: null,
  stats: null,
  players: null,
  op: null,
  logsAuto: true,
  lastOpActive: false,
  logsLevel: "all",
  logsLines: [],
  logsUpdatedAt: 0,
  logsSince: 0,
  logsUntil: 0,
  logsProfile: "",
  backupsItems: [],
  phPoints: [],
  lastDataOk: 0,
};

function demoNow() { return new Date().toISOString(); }

const DEMO = {
  overview: () => ({
    ok: true, serverName: "Кастом-Нокс (демо)", container: "pzserver",
    image: "indifferentbrokkoli/pzserver:latest", docker: true, compose: true,
    rconConfigured: true, rcon: { state: "ok", error: null, at: demoNow() },
    containerInfo: { status: "running", running: true, startedAt: new Date(Date.now() - 569000 * 1000).toISOString(), image: "indifferentbrokkoli/pzserver:latest", uptimeSec: 569000 },
    update: { at: new Date(Date.now() - 2 * 3600 * 1000).toISOString(), local: "sha256:9f21a4c0e7b2d8f1a3c5e7b9d1f3a5c7e9b1d3f5a7c9e1b3d5f7a9c1e3b5d7f9", remote: "sha256:9f21a4c0e7b2d8f1a3c5e7b9d1f3a5c7e9b1d3f5a7c9e1b3d5f7a9c1e3b5d7f9", available: false, error: null },
    modsCheck: { at: new Date(Date.now() - 30 * 60 * 1000).toISOString(), state: "up-to-date", items: [], error: null, source: "auto" },
    settings: { autoUpdate: { enabled: true, intervalHours: 6, warnSeconds: 300, backupBeforeUpdate: true }, modsUpdate: { enabled: true, intervalHours: 6, restartOnUpdate: true, warnSeconds: 600 }, backup: { stopServer: false, maxBackups: 10 }, watchdog: { enabled: true, thresholdMin: 5, autoRestart: false }, telegram: { enabled: true, botTokenMasked: "•••A1b2", chatId: "-1001234567890", groups: { ops: true, backup: true, update: true, problems: true } }, nextCheck: Date.now() / 1000 + 3600 * 4, nextModsCheck: Date.now() / 1000 + 3600 * 2 },
    watchdog: { lastProbeAt: demoNow(), lastResult: "ok", lastError: null, consecutiveFailures: 0, alerted: false },
    notify: { at: demoNow(), ok: true, error: null },
    backupsCount: 2, now: demoNow(),
  }),
  players: () => ({ ok: true, names: ["Дмитрий", "Sledge", "Katya_V"], raw: "Дмитрий\nSledge\nKatya_V", count: 3 }),
  statsHistory: () => {
    const pts = [];
    const now = Date.now();
    for (let i = 30; i >= 0; i--) {
      pts.push({ ts: new Date(now - i * 120000).toISOString(), cpu: +(2 + 6 * Math.random()).toFixed(2), mem: +(17 + 4 * Math.random()).toFixed(2) });
    }
    return { ok: true, points: pts };
  },
  mods: () => ({
    ok: true, files: ["servertest.ini"], file: "servertest.ini",
    mods: ["tsarslib", "commonpackagev15", "commonpackagev15options", "sandbox-plus", "local-mod"],
    unbound: ["local-mod"],
    paired: false, mappingSource: "disk",
    workshop: [
      { workshopId: "2694464646", url: "https://steamcommunity.com/sharedfiles/filedetails/?id=2694464646", title: "Common Package v1.5", mods: ["commonpackagev15", "commonpackagev15options"] },
      { workshopId: "2804001857", url: "https://steamcommunity.com/sharedfiles/filedetails/?id=2804001857", title: "Sandbox+ (Sandbox Options)", mods: ["sandbox-plus"] },
    ],
    pairs: [],
  }),
  history: () => {
    const pts = [];
    const now = Date.now();
    for (let i = 24; i >= 0; i--) {
      const t = new Date(now - i * 3600 * 1000).toISOString();
      const h = new Date(t).getHours();
      pts.push({ ts: t, count: Math.max(0, Math.round(3 + 3 * Math.sin(((h - 14) / 24) * Math.PI * 2))) });
    }
    return { ok: true, points: pts };
  },
  stats: () => {
    const cpu = 4 + Math.random() * 14;
    return { ok: true, cpuPct: cpu, memUsed: 1.24e9, memLimit: 4.29e9, memPct: 29, netIn: 2.4e9, netOut: 1.1e9, pids: 42 };
  },
  logs: () => ({
    ok: true,
    text: `${demoNow()} PZ SERVER: v44.3.2 MP dedicated\n${demoNow()} [save] World saved (demo line)\n${demoNow()} INFO: 3 players online\n`,
  }),
  backups: () => ({
    ok: true, maxBackups: 7,
    autoBackup: { enabled: true, time: "03:00", stopServer: false,
                  nextRun: new Date(Date.now() + 36e5 * 11).toISOString() },
    items: [
      { name: "pz-backup-20260901-040000.tar.gz", size: 684000000, sizeText: "652.3 МБ", mtime: "2026-09-01T04:00:00" },
      { name: "pz-backup-20260831-040000.tar.gz", size: 672000000, sizeText: "640.9 МБ", mtime: "2026-08-31T04:00:00" },
    ],
    journal: [
      { ts: "2026-09-01T04:00:03", trigger: "scheduled", type: "full", name: "pz-backup-20260901-040000.tar.gz", size: 684000000, status: "success", duration: 42.5 },
      { ts: "2026-08-31T04:00:02", trigger: "scheduled", type: "full", name: "pz-backup-20260831-040000.tar.gz", size: 672000000, status: "success", duration: 41.2 },
      { ts: "2026-08-30T04:00:05", trigger: "scheduled", type: "full", name: "", size: 0, status: "error", error: "Каталог данных PZ пуст или не смонтирован", duration: 0.4 },
    ],
  }),
  events: () => ({
    ok: true,
    items: [
      { ts: demoNow(), type: "update-check", text: "Плановая проверка обновлений: обновлений нет" },
      { ts: new Date(Date.now() - 3600e3).toISOString(), type: "backup", text: "Бэкап создан: pz-backup-20260901-040000.tar.gz (652.3 МБ)" },
      { ts: new Date(Date.now() - 7200e3).toISOString(), type: "restart", text: "Сервер перезапущен" },
    ],
  }),
};

function requireLogin() {
  location.replace("/login?next=" + encodeURIComponent(location.hash));
}

window.addEventListener("pageshow", (event) => {
  if (event.persisted) {
    fetch("/api/auth/session").then((response) => {
      if (response.status === 401) requireLogin();
    }).catch(() => {});
  }
});

$("btnLogout").addEventListener("click", async () => {
  const button = $("btnLogout");
  button.disabled = true;
  try {
    const response = await fetch("/api/auth/logout", {
      method: "POST",
      headers: { "X-PZ-Request": "1" },
      signal: AbortSignal.timeout(9000),
    });
    if (!response.ok && response.status !== 401) throw new Error("Не удалось выйти");
    requireLogin();
  } catch (error) {
    toast(error.message || "Не удалось выйти", "error");
    button.disabled = false;
  }
});

async function api(path, opts = {}) {
  if (S.demo) {
    if (opts.method && opts.method !== "GET") {
      throw new Error("Демо-режим: операции недоступны");
    }
    await new Promise((r) => setTimeout(r, 120));
    if (path.startsWith("/api/overview")) return DEMO.overview();
    if (path.startsWith("/api/players/history")) return DEMO.history();
    if (path.startsWith("/api/stats/history")) return DEMO.statsHistory();
    if (path.startsWith("/api/mods")) return DEMO.mods();
    if (path.startsWith("/api/players")) return DEMO.players();
    if (path.startsWith("/api/stats")) return DEMO.stats();
    if (path.startsWith("/api/logs")) return DEMO.logs();
    if (path.startsWith("/api/backups")) return DEMO.backups();
    if (path.startsWith("/api/events")) return DEMO.events();
    if (path.startsWith("/api/ops")) return { ok: true, active: null, history: [] };
    throw new Error("Демо-режим: нет данных");
  }
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), opts.timeout || 9000);
  try {
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers: { "X-PZ-Request": "1", ...(opts.body ? { "Content-Type": "application/json" } : {}) },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      signal: ctrl.signal,
    });
    if (res.status === 401) {
      requireLogin();
      throw new Error("Сессия завершена. Войдите снова");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok && !data.error) data.error = `HTTP ${res.status}`;
    return data;
  } finally {
    clearTimeout(timer);
  }
}

async function action(op, extra = {}) {
  try {
    const res = await api("/api/action", { method: "POST", body: { op, ...extra } });
    if (res.error) throw new Error(res.error);
    toast(`Операция «${OP_TITLES[op] || op}» запущена`, "ok");
  } catch (e) {
    toast(e.message || String(e), "error");
  }
}

/* ───────────────────────── отрисовка: обзор ───────────────────────── */

function setPill(id, state, text) {
  const p = $(id);
  if (!p) return;
  p.dataset.state = state;
  if (text) p.textContent = text;
}

function renderOverview(o) {
  S.overview = o;
  $("serverName").textContent = o.serverName || "Project Zomboid";
  setPill("pillDocker", o.docker ? "ok" : "bad", o.docker ? "Docker" : "Docker: вне хоста");
  setPill("pillCompose", o.compose ? "ok" : "bad", o.compose ? "compose" : "compose ✕");
  const rc = o.rcon || {};
  $("pillRcon").title = "";
  if (!o.rconConfigured) setPill("pillRcon", "bad", "RCON: нет пароля");
  else if (o.docker && o.mode !== "remote" && o.containerInfo?.running === false) {
    setPill("pillRcon", "unknown", "RCON не активен");
    $("pillRcon").title = "Сервер остановлен; RCON будет доступен после запуска";
  }
  else if (rc.state === "ok") setPill("pillRcon", "ok", "RCON");
  else if (rc.state === "error") { setPill("pillRcon", "bad", "RCON ошибка"); $("pillRcon").title = rc.error || ""; }
  else setPill("pillRcon", "unknown", "RCON");

  const remote = o.mode === "remote";
  const c = o.containerInfo;
  const lamp = $("stateLamp"), label = $("stateLabel");
  if (remote) {
    const rs = rc.state || "unknown";
    $("absentHint").hidden = false;
    $("absentHint").textContent =
      "Пульт запущен вне хоста сервера: активны RCON-консоль, игроки, объявления и сохранение мира. " +
      "Управление контейнером, бэкапы и обновление заработают при запуске пульта на сервере (README, вариант Б).";
    if (rs === "ok") {
      lamp.dataset.state = "ok";
      label.textContent = "Работает (RCON)";
      $("uptime").textContent = "удалённое управление по RCON";
    } else if (rs === "error") {
      lamp.dataset.state = "bad";
      label.textContent = "Нет ответа RCON";
      $("uptime").textContent = "проверьте сервер, порт и пароль";
    } else {
      lamp.dataset.state = "warn";
      label.textContent = "Проверка…";
      $("uptime").textContent = "…";
    }
    $("mStarted").textContent = "—";
  } else {
    $("absentHint").hidden = !!c;
    if (!c) {
      lamp.dataset.state = "bad";
      label.textContent = "Контейнер не найден";
      $("uptime").textContent = "—";
    } else if (c.running) {
      lamp.dataset.state = "ok";
      label.textContent = "Работает";
      $("uptime").textContent = `в работе ${fmtUptime(c.uptimeSec)}`;
    } else if (c.status === "restarting") {
      lamp.dataset.state = "warn";
      label.textContent = "Перезапускается";
      $("uptime").textContent = "—";
    } else {
      lamp.dataset.state = "bad";
      label.textContent = "Остановлен";
      $("uptime").textContent = "—";
    }
    $("mStarted").textContent = c ? fmtTime(c.startedAt) : "—";
  }
  $("mContainer").textContent = o.container || "—";
  $("mImage").textContent = o.image || "—";
  $("mDigest").textContent = shortDigest(o.update?.local);
  $("mContainer").dataset.copy = o.container || "";
  $("mImage").dataset.copy = o.image || "";
  if (o.update?.local) $("mDigest").dataset.copy = o.update.local; else $("mDigest").removeAttribute("data-copy");

  // блок обновлений
  const u = o.update || {};
  const pill = $("updPill");
  if (remote) {
    pill.dataset.state = "unknown";
    pill.textContent = "только на хосте сервера";
    $("updNote").textContent = "Сравнение digest требует доступа к локальному образу — обновление выполняется с хоста сервера.";
  } else if (u.available === true) {
    pill.dataset.state = "warn"; pill.textContent = "есть обновление";
    $("updNote").textContent = "Сверяется digest локального образа с Docker Hub.";
  } else if (u.available === false) {
    pill.dataset.state = "ok"; pill.textContent = "актуально";
    $("updNote").textContent = "Сверяется digest локального образа с Docker Hub.";
  } else if (u.error) {
    pill.dataset.state = "bad"; pill.textContent = "ошибка проверки";
    $("updNote").textContent = u.error;
  } else if (u.note) {
    pill.dataset.state = "unknown"; pill.textContent = "—";
    $("updNote").textContent = u.note;
  } else {
    pill.dataset.state = "unknown"; pill.textContent = "не проверялось";
    $("updNote").textContent = "Сверяется digest локального образа с Docker Hub.";
  }
  $("updLocal").textContent = shortDigest(u.local);
  const same = u.local && u.remote && u.local === u.remote;
  $("updRemote").textContent = same ? "совпадает" : shortDigest(u.remote);
  $("updRemote").classList.toggle("ok-same", !!same);
  $("updChecked").textContent = u.at ? fmtTime(u.at) : "никогда";
  $("updHubDate").textContent = u.hubUpdated ? "собрана " + fmtTime(u.hubUpdated) : "—";
  if (u.local) $("updLocal").dataset.copy = u.local; else $("updLocal").removeAttribute("data-copy");
  if (u.remote) $("updRemote").dataset.copy = u.remote; else $("updRemote").removeAttribute("data-copy");

  // автообновление
  const au = o.settings?.autoUpdate || {};
  if (!$("autoSwitch").matches(":focus")) $("autoSwitch").checked = !!au.enabled;
  if (!$("autoInterval").matches(":focus")) $("autoInterval").value = String(au.intervalHours ?? 6);
  if (!$("autoWarn").matches(":focus")) $("autoWarn").value = String(au.warnSeconds ?? 300);
  $("backupNudge").hidden = remote || o.backupsCount !== 0;
  if (!$("buBackup").matches(":focus")) $("buBackup").checked = au.backupBeforeUpdate !== false;
  const wdCfg = o.settings?.watchdog || {};
  if (!$("wdSwitch").matches(":focus")) $("wdSwitch").checked = !!wdCfg.enabled;
  if (!$("wdThreshold").matches(":focus")) $("wdThreshold").value = String(wdCfg.thresholdMin ?? 5);
  if (!$("wdRestart").matches(":focus")) $("wdRestart").checked = !!wdCfg.autoRestart;
  const wds = o.watchdog || {};
  if (wdCfg.enabled) {
    const fails = wds.consecutiveFailures || 0;
    const skipped = wds.lastResult === "skipped" && !fails;
    setPill("wdPill", fails ? "bad" : skipped ? "unknown" : "ok",
      fails ? `сбои: ${fails}` : skipped ? "ожидание" : "следит");
    $("wdStatus").hidden = false;
    $("wdStatus").className = "hint mono " + (fails ? "bad" : "ok");
    $("wdStatus").textContent = `проба ${fmtTime(wds.lastProbeAt)} · ` +
      (skipped ? "сервер остановлен или идёт операция — проба пропущена"
               : `сбоев подряд: ${fails}`) +
      (fails && wds.lastError ? ` · ${wds.lastError}` : "");
  } else {
    setPill("wdPill", "unknown", "выкл");
    $("wdStatus").hidden = true;
  }

  // уведомления Telegram
  const tg = o.settings?.telegram || {};
  const tgEnabled = !!tg.enabled;
  if (!$("tgSwitch").matches(":focus")) $("tgSwitch").checked = tgEnabled;
  if (!$("tgChat").matches(":focus")) $("tgChat").value = tg.chatId || "";
  const groups = tg.groups || {};
  if (!$("tgOps").matches(":focus")) $("tgOps").checked = groups.ops !== false;
  if (!$("tgBackup").matches(":focus")) $("tgBackup").checked = groups.backup !== false;
  if (!$("tgUpdate").matches(":focus")) $("tgUpdate").checked = groups.update !== false;
  if (!$("tgProblems").matches(":focus")) $("tgProblems").checked = groups.problems !== false;
  const masked = tg.botTokenMasked || "";
  $("tgNote").textContent = masked
    ? `Токен сохранён (${masked}) — наружу не отдаётся. Чтобы заменить, введите новый.`
    : "Токен хранится на сервере пульта и наружу не отдаётся.";
  if (tgEnabled) {
    const ns = o.notify || {};
    if (ns.ok === false && ns.error) {
      setPill("tgPill", "bad", "ошибка отправки");
    } else if (ns.ok) {
      setPill("tgPill", "ok", "вкл");
    } else {
      setPill("tgPill", "ok", "вкл");
    }
  } else {
    setPill("tgPill", "unknown", "выкл");
  }

  const next = o.settings?.nextCheck;
  $("autoNext").hidden = !(au.enabled && next);
  if (au.enabled && next) $("autoNext").textContent = `Следующая проверка: ${new Date(next * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}`;

  // проверка модов (RCON checkModsNeedUpdate)
  const mc = o.modsCheck || {};
  if (mc.state === "up-to-date") {
    setPill("modsPill", "ok", "актуальны");
    $("modsCheckNote").textContent = mc.at ? `Проверено ${fmtTime(mc.at)} — сервер вернул «Mods updated».` : "Сервер сверяет версии со Steam Workshop; обновления применяются рестартом.";
  } else if (mc.state === "needs-update") {
    const n = (mc.items || []).length;
    setPill("modsPill", "warn", n ? `обновить: ${n}` : "есть обновления");
    $("modsCheckNote").textContent = mc.at ? `Проверено ${fmtTime(mc.at)} — часть модов устарела, нужен рестарт для загрузки версий.` : "";
  } else if (mc.state === "inconclusive") {
    setPill("modsPill", "warn", "нет ответа");
    $("modsCheckNote").textContent = mc.at ? `Проверено ${fmtTime(mc.at)} — сервер не вернул результат за отведённое время, попробуйте позже.` : "";
  } else {
    setPill("modsPill", "unknown", "не проверялись");
  }
  const items = mc.items || [];
  const needList = $("modsNeedList");
  needList.hidden = !(mc.state === "needs-update" && items.length);
  needList.innerHTML = items.map((it) => `
    <div class="mod-need-row">
      <span class="m-title">${it.url
        ? `<a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.title || it.workshopId)}</a>`
        : esc(it.raw || "мод требует обновления")}</span>
      ${it.workshopId ? `<span class="wid mono" data-copy="${esc(it.workshopId)}" title="нажмите — скопировать">${esc(it.workshopId)}</span>` : ""}
    </div>`).join("");
  $("btnApplyMods").hidden = mc.state !== "needs-update";
  const mu = o.settings?.modsUpdate || {};
  if (!$("modsAutoSwitch").matches(":focus")) $("modsAutoSwitch").checked = !!mu.enabled;
  if (!$("modsAutoInterval").matches(":focus")) $("modsAutoInterval").value = String(mu.intervalHours ?? 6);
  if (!$("modsAutoAction").matches(":focus")) $("modsAutoAction").value = mu.restartOnUpdate === false ? "notify" : "restart";
  if (!$("modsAutoWarn").matches(":focus")) $("modsAutoWarn").value = String(mu.warnSeconds ?? 600);
  const nextM = o.settings?.nextModsCheck;
  $("modsAutoNext").hidden = !(mu.enabled && nextM);
  if (mu.enabled && nextM) $("modsAutoNext").textContent = `Следующая проверка: ${new Date(nextM * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}`;

  renderHealth();
  renderSummaries();
  updateButtons();
}

/* строка здоровья: состояние сервера одним взглядом */
function renderHealth() {
  const o = S.overview;
  const set = (id, state, text) => {
    const el = $(id);
    if (!el) return;
    el.dataset.state = state;
    el.textContent = text;
  };
  const remote = !!(o && o.mode === "remote");
  ["hbBackup", "hbImage", "hbWatchdog"].forEach((id) => { const el = $(id); if (el) el.hidden = remote; });

  if (!o) {
    set("hbServer", "unknown", "Сервер: …");
    set("hbMods", "unknown", "Моды: …");
    return;
  }

  if (remote) {
    const rs = o.rcon ? o.rcon.state : "unknown";
    set("hbServer", rs === "ok" ? "ok" : rs === "error" ? "bad" : "unknown",
      rs === "ok" ? "Сервер: RCON" : rs === "error" ? "Сервер: нет ответа" : "Сервер: …");
  } else {
    const c = o.containerInfo;
    if (!c) set("hbServer", "bad", "Сервер: не найден");
    else if (c.running) set("hbServer", "ok", "Сервер: работает");
    else if (c.status === "restarting") set("hbServer", "warn", "Сервер: перезапускается");
    else set("hbServer", "bad", "Сервер: остановлен");
  }

  const mc = o.modsCheck || {};
  if (mc.state === "up-to-date") set("hbMods", "ok", "Моды: актуальны");
  else if (mc.state === "needs-update") {
    const n = (mc.items || []).length;
    set("hbMods", "warn", n ? `Моды: обновить ${n}` : "Моды: есть обновления");
  }
  else if (mc.state === "inconclusive") set("hbMods", "warn", "Моды: нет ответа");
  else set("hbMods", "unknown", "Моды: не проверялись");

  if (remote) {
    set("hbBackup", "unknown", "Бэкап: на хосте");
    set("hbImage", "unknown", "Образ: на хосте");
    set("hbWatchdog", "unknown", "Watchdog: на хосте");
    return;
  }

  const last = S.backupsItems && S.backupsItems[0];
  if (!last || !last.mtime) set("hbBackup", "bad", "Бэкап: нет");
  else {
    const age = Date.now() - new Date(last.mtime).getTime();
    if (isNaN(age)) set("hbBackup", "unknown", "Бэкап: …");
    else if (age > 72 * 3600 * 1000) set("hbBackup", "warn", `Бэкап: ${Math.floor(age / 86400000)} д назад`);
    else set("hbBackup", "ok", `Бэкап: ${relTime(last.mtime)}`);
  }

  const u = o.update || {};
  if (u.available === false) set("hbImage", "ok", "Образ: актуален");
  else if (u.available === true) set("hbImage", "warn", "Образ: есть обновление");
  else if (u.error) set("hbImage", "bad", "Образ: ошибка");
  else set("hbImage", "unknown", "Образ: не проверялся");

  const wd = o.settings ? o.settings.watchdog : null;
  const fails = (o.watchdog && o.watchdog.consecutiveFailures) || 0;
  if (wd && wd.enabled) set("hbWatchdog", fails ? "bad" : "ok", fails ? `Watchdog: сбои ${fails}` : "Watchdog: ок");
  else set("hbWatchdog", "unknown", "Watchdog: выкл");
}

/* KPI-строка обзора: те же данные, что и в карточках, но одним взглядом */
function renderKpis() {
  const o = S.overview;
  const remote = !!(o && o.mode === "remote");

  const p = S.players;
  const online = p && p.ok ? p.count : null;
  $("kpiOnline").textContent = online == null ? "–" : String(online);
  if (remote) $("kpiOnlineSub").textContent = "RCON";
  else if (S.phPoints && S.phPoints.length) {
    const peak = S.phPoints.reduce((m, x) => Math.max(m, x.count || 0), 0);
    $("kpiOnlineSub").textContent = `пик за сутки: ${peak}`;
  } else $("kpiOnlineSub").textContent = "…";

  const st = S.stats;
  if (remote || !st || !st.ok) {
    $("kpiCpu").textContent = "—";
    $("kpiCpuBar").style.width = "0%";
    $("kpiRam").textContent = "—";
    $("kpiRamSub").textContent = remote ? "только с хоста" : "…";
  } else {
    const cpu = Math.max(0, Math.min(100, st.cpuPct || 0));
    $("kpiCpu").textContent = `${cpu.toFixed(0)}%`;
    $("kpiCpuBar").style.width = `${cpu}%`;
    $("kpiCpuBar").className = cpu >= 85 ? "hot" : cpu >= 60 ? "warm" : "";
    const memPct = Math.max(0, Math.min(100, st.memPct || 0));
    $("kpiRam").textContent = `${memPct.toFixed(0)}%`;
    $("kpiRamSub").textContent = `${fmtBytes(st.memUsed)} / ${fmtBytes(st.memLimit)}`;
  }

  const last = S.backupsItems && S.backupsItems[0];
  if (remote) {
    $("kpiBackup").textContent = "—";
    $("kpiBackupSub").textContent = "на хосте";
  } else if (!last || !last.mtime) {
    $("kpiBackup").textContent = "0";
    $("kpiBackupSub").textContent = "архивов ещё нет";
  } else {
    $("kpiBackup").textContent = last.sizeText || fmtBytes(last.size);
    $("kpiBackupSub").textContent = relTime(last.mtime);
  }
}

/* сводка обновлений на обзоре: зеркалит пилюли карточек обслуживания и модов */
function renderSummaries() {
  const img = $("updPill"), mods = $("modsPill");
  const si = $("sumImagePill"), sm = $("sumModsPill");
  si.dataset.state = img.dataset.state;
  si.textContent = img.textContent;
  sm.dataset.state = mods.dataset.state;
  sm.textContent = mods.textContent;
  const u = (S.overview || {}).update || {};
  $("sumImageMeta").textContent = u.at ? `проверено ${fmtTime(u.at)}` : "не проверялось";
  const mc = (S.overview || {}).modsCheck || {};
  $("sumModsMeta").textContent = mc.at ? `проверено ${fmtTime(mc.at)}` : "не проверялись";
}

function updateButtons() {
  const o = S.overview;
  const busy = !!(S.op && S.op.active) || S.demo;
  const remote = !!(o && o.mode === "remote");
  const rconOk = !!(o && o.rcon && o.rcon.state === "ok");
  const running = !remote && !!(o && o.containerInfo && o.containerInfo.running);
  const found = !!(o && o.containerInfo);
  const hostHint = "Доступно только при запуске пульта на хосте сервера";
  const consoleLive = busy ? false : (remote ? rconOk : running);
  $("btnStart").disabled = busy || remote || !found || running;
  $("btnStop").disabled = busy || S.demo || remote || !running;
  $("btnRestart").disabled = busy || S.demo || remote || !running;
  $("btnSaveWorld").disabled = !consoleLive;
  $("btnCheckUpd").disabled = busy || S.demo || remote;
  $("btnApplyUpd").disabled = busy || remote || (o && o.compose === false);
  $("btnCheckMods").disabled = busy || S.demo || remote;
  $("btnApplyMods").disabled = busy || S.demo || remote;
  const modsRestart = S.op?.active?.op === "mods-restart";
  const cancelPending = modsRestart && !!S.op.active.cancelRequested;
  $("btnCancelMods").hidden = !modsRestart || (!S.op.active.cancellable && !cancelPending);
  $("btnCancelMods").disabled = remote || cancelPending || !S.op?.active?.cancellable;
  $("btnCancelMods").textContent = cancelPending ? "Отмена…" : "Отменить обновление модов";
  // автонастройки и watchdog пишут в настройки и работают только с хоста —
  // в remote-режиме они тихо ничего не делают, честно их глушим
  const hostOnly = busy || S.demo || remote;
  $("autoSwitch").disabled = hostOnly;
  $("autoInterval").disabled = hostOnly;
  $("autoWarn").disabled = hostOnly;
  $("buBackup").disabled = hostOnly;
  $("wdSwitch").disabled = hostOnly;
  $("wdThreshold").disabled = hostOnly;
  $("modsAutoSwitch").disabled = hostOnly;
  $("modsAutoInterval").disabled = hostOnly;
  $("modsAutoAction").disabled = hostOnly;
  $("modsAutoWarn").disabled = hostOnly;
  $("bkAutoSwitch").disabled = hostOnly;
  $("bkAutoTime").disabled = hostOnly;
  $("bkAutoKeep").disabled = hostOnly;
  $("bkAutoStop").disabled = hostOnly;
  for (const id of ["btnStart", "btnStop", "btnRestart", "btnCheckUpd", "btnApplyUpd", "btnBackup", "btnCheckMods", "btnApplyMods",
                    "autoSwitch", "autoInterval", "autoWarn", "buBackup", "wdSwitch", "wdThreshold",
                    "modsAutoSwitch", "modsAutoInterval", "modsAutoAction", "modsAutoWarn",
                    "bkAutoSwitch", "bkAutoTime", "bkAutoKeep", "bkAutoStop"]) {
    $(id).title = remote ? hostHint : (id === "btnApplyUpd" && o && o.compose === false
      ? "Недоступен плагин docker compose в контейнере пульта" : "");
  }
  $("btnBackup").disabled = busy || S.demo || remote;
  $("logsDownload").style.display = (remote || S.demo) ? "none" : "";
  $("logsFilter").disabled = remote || S.demo;
  document.querySelectorAll("#playersBody .p-actions .icon-btn").forEach((b) => { b.disabled = !consoleLive; });
  document.querySelectorAll("#backupsBody .icon-btn").forEach((b) => { b.disabled = busy || S.demo || remote; });
  document.querySelectorAll("#quickCmds .chip").forEach((b) => { b.disabled = !consoleLive; });
  $("consoleInput").disabled = !consoleLive;
  $("consoleForm").querySelector("button").disabled = !consoleLive;
}

function renderOp(op) {
  const active = op && op.active;
  if (active) {
    $("opbar").hidden = false;
    $("opPhase").textContent = `${OP_TITLES[active.op] || active.op}: ${active.phase}`;
    $("opMsg").textContent = active.message || "";
  } else {
    $("opbar").hidden = true;
  }
  if (S.lastOpActive && !active && op && op.history && op.history[0]) {
    const h = op.history[0];
    toast(h.cancelled ? h.message : h.ok ? `Готово: ${h.message || h.op}` : `Не удалось: ${h.message || h.op}`, h.ok ? "ok" : "error", 8000);
    refreshAll();
  }
  S.lastOpActive = !!(active);
  S.op = op;
  updateButtons();
  window.ConfigEditor?.operationChanged();
}

/* человеческие названия операций для полосы прогресса и тостов */
const OP_TITLES = {
  start: "Запуск", stop: "Остановка", restart: "Рестарт",
  "check-update": "Проверка обновлений", "apply-update": "Обновление сервера",
  "check-mods-update": "Проверка модов", "apply-mods-update": "Обновление модов",
  "mods-restart": "Авторестарт модов",
  backup: "Бэкап", restore: "Восстановление", "verify-backup": "Проверка архива",
};

/* ───────────────────────── игроки ───────────────────────── */

function renderPlayers(data) {
  S.players = data;
  const body = $("playersBody");
  if (!data.ok) {
    body.dataset.state = "error";
    body.innerHTML = `<p class="list-error">${esc(data.error || "нет данных")}</p>`;
    $("playersCount").textContent = "–";
    return;
  }
  $("playersCount").textContent = String(data.count);
  if (!data.names.length) {
    body.dataset.state = "empty";
    const raw = (data.raw || "").trim();
    body.innerHTML = `<p class="list-empty"><strong>Пусто.</strong> Выживших не найдено — сервер ждёт.</p>` +
      (raw && !/players/i.test(raw) ? `<p class="list-empty mono">${esc(raw)}</p>` : "");
    return;
  }
  body.dataset.state = "ok";
  body.innerHTML = data.names.map((n) => `
    <div class="player-row">
      <span class="dot"></span>
      <span class="p-name" title="${esc(n)}">${esc(n)}</span>
      <span class="p-actions">
        <button class="icon-btn" data-p="kick" data-name="${esc(n)}" title="Кикнуть" aria-label="Кикнуть ${esc(n)}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v18h-6M10 17l5-5-5-5M15 12H3"/></svg>
        </button>
        <button class="icon-btn danger" data-p="ban" data-name="${esc(n)}" title="Забанить" aria-label="Забанить ${esc(n)}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M5.5 5.5l13 13"/></svg>
        </button>
      </span>
    </div>`).join("");

  body.querySelectorAll(".p-actions .icon-btn").forEach((btn) => {
    btn.addEventListener("click", () => confirmPlayerAction(btn.dataset.p, btn.dataset.name));
  });
  updateButtons();
}

function confirmPlayerAction(kind, name) {
  const isKick = kind === "kick";
  const cmdBase = isKick ? "kickuser" : "banuser";
  modal.open({
    title: `${isKick ? "Кикнуть" : "Забанить"} «${name}»?`,
    danger: true,
    okLabel: isKick ? "Кикнуть" : "Забанить",
    bodyHTML: `
      <p>Игрок будет ${isKick ? "отключён от сервера" : "заблокирован навсегда"} командой
      <span class="mono">${cmdBase}</span>.</p>
      <label class="field">Причина (необязательно)
        <input type="text" id="paReason" maxlength="120" style="height:38px;color:var(--ink);background:var(--bg-deep);border:1px solid var(--line-strong);border-radius:8px;padding:0 12px;" />
      </label>
    `,
    onConfirm: async () => {
      const reason = ($("paReason")?.value || "").replace(/"/g, "'").trim();
      const safeName = name.replace(/"/g, "'");
      const cmd = `${cmdBase} "${safeName}"${reason ? ` "${reason}"` : ""}`;
      try {
        const res = await api("/api/rcon", { method: "POST", body: { command: cmd } });
        if (res.error) throw new Error(res.error);
        consoleAppend(`> ${cmd}`, "c-dim");
        consoleAppend(res.output || "(без ответа)");
        toast(`${isKick ? "Кикнут" : "Забанен"}: ${name}`, "ok");
        refreshPlayers();
      } catch (e) {
        toast(e.message || String(e), "error");
      }
    },
  });
}

/* ───────────────────────── график онлайна за сутки ───────────────────────── */

function renderPlayersHistory(points) {
  S.phPoints = points || [];
  const svg = $("phSpark");
  if (!S.phPoints || S.phPoints.length < 2) {
    $("phEmpty").hidden = false;
    $("phPeak").textContent = "—";
    svg.innerHTML = "";
    return;
  }
  $("phEmpty").hidden = true;
  const W = 120, H = 30;
  const maxC = Math.max(4, ...points.map((p) => p.count || 0));
  const t0 = new Date(points[0].ts).getTime();
  const t1 = Math.max(new Date(points[points.length - 1].ts).getTime(), t0 + 1);
  const x = (ts) => ((new Date(ts).getTime() - t0) / (t1 - t0)) * W;
  const y = (c) => H - 1 - (Math.min(c || 0, maxC) / maxC) * (H - 2);
  const segs = [];
  let d = "";
  let prev = null;
  for (const p of points) {
    const t = new Date(p.ts).getTime();
    if (prev !== null && t - prev > 30 * 60 * 1000) { if (d) segs.push(d); d = ""; }
    d += (d ? " L" : "M") + x(p.ts).toFixed(1) + "," + y(p.count).toFixed(1);
    prev = t;
  }
  if (d) segs.push(d);
  let inner = "";
  for (const s of segs) {
    const first = s.match(/^M([\d.]+),([\d.]+)/);
    const last = s.match(/([\d.]+),([\d.]+)$/);
    inner += `<path d="${s}" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>`;
    if (first && last && first[1] !== last[1]) {
      inner += `<path d="${s} L${last[1]},${H} L${first[1]},${H} Z" fill="currentColor" opacity="0.12" stroke="none"/>`;
    }
  }
  svg.innerHTML = inner;
  const peak = points.reduce((m, p) => Math.max(m, p.count || 0), 0);
  $("phPeak").textContent = `пик: ${peak}`;
  if (typeof renderKpis === "function") renderKpis();
}

(() => {
  const spark = $("phSpark");
  spark.addEventListener("mousemove", (ev) => {
    const pts = S.phPoints;
    if (!pts || pts.length < 2) return;
    const rect = spark.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width));
    const t0 = new Date(pts[0].ts).getTime();
    const t1 = new Date(pts[pts.length - 1].ts).getTime();
    const target = t0 + frac * (t1 - t0);
    let best = pts[0];
    for (const p of pts) {
      if (Math.abs(new Date(p.ts).getTime() - target) < Math.abs(new Date(best.ts).getTime() - target)) best = p;
    }
    const tip = $("phTip");
    tip.hidden = false;
    tip.textContent = `${fmtTime(best.ts)} · ${best.count} игр.`;
    tip.style.left = Math.max(60, Math.min(rect.width - 10, ev.clientX - rect.left)) + "px";
  });
  spark.addEventListener("mouseleave", () => { $("phTip").hidden = true; });
})();

async function refreshPlayersHistory() {
  try { renderPlayersHistory((await api("/api/players/history")).points || []); } catch (e) { /* тихо */ }
}

/* ───────────────────────── метрики ───────────────────────── */

function drawSpark(id, vals) {
  const el = $(id);
  if (!el) return;
  if (!vals || vals.length < 2) { el.setAttribute("points", ""); return; }
  const max = Math.max(10, ...vals);
  const pts = vals.map((v, i) => `${((i / (vals.length - 1)) * 120).toFixed(1)},${(26 - (Math.min(v, max) / max) * 24).toFixed(1)}`).join(" ");
  el.setAttribute("points", pts);
}

function renderStatsHistory(points) {
  drawSpark("cpuSparkLine", (points || []).map((p) => p.cpu));
  drawSpark("ramSparkLine", (points || []).map((p) => p.mem));
}

async function refreshStatsHistory() {
  try { renderStatsHistory((await api("/api/stats/history")).points || []); } catch (e) { /* тихо */ }
}

function renderStats(st) {
  S.stats = st;
  if (S.overview && S.overview.mode === "remote") {
    $("cpuVal").textContent = "—"; $("ramVal").textContent = "—";
    $("ramSub").textContent = "метрики — только с хоста сервера";
    $("netIn").textContent = "—"; $("netOut").textContent = "—"; $("pids").textContent = "—";
    $("cpuBar").style.width = "0%"; $("ramBar").style.width = "0%";
    return;
  }
  if (!st.ok) {
    $("cpuVal").textContent = "—"; $("ramVal").textContent = "—";
    $("ramSub").textContent = st.error || "—";
    $("netIn").textContent = "—"; $("netOut").textContent = "—"; $("pids").textContent = "—";
    $("cpuBar").style.width = "0%"; $("ramBar").style.width = "0%";
    return;
  }
  const cpu = Math.max(0, Math.min(100, st.cpuPct || 0));
  $("cpuVal").textContent = `${cpu.toFixed(1)}%`;
  setBar("cpuBar", cpu);
  const memPct = Math.max(0, Math.min(100, st.memPct || 0));
  $("ramVal").textContent = `${memPct.toFixed(0)}%`;
  setBar("ramBar", memPct);
  $("ramSub").textContent = `${fmtBytes(st.memUsed)} из ${fmtBytes(st.memLimit)}`;
  $("netIn").textContent = fmtBytes(st.netIn);
  $("netOut").textContent = fmtBytes(st.netOut);
  $("pids").textContent = String(st.pids ?? "—");
}

function setBar(id, pct) {
  const bar = $(id);
  bar.style.width = `${pct}%`;
  bar.className = pct >= 85 ? "hot" : pct >= 60 ? "warm" : "";
}

/* ───────────────────────── моды сервера ───────────────────────── */

/* переключатель в строке мода (управление составом) */
const _wsSwitch = (wsId, checked, title) => `
  <label class="switch switch-sm" title="${esc(title)}">
    <input type="checkbox" role="switch" data-ws="${esc(wsId)}"${checked ? " checked" : ""} />
    <span class="switch-track" aria-hidden="true"></span>
  </label>`;

function _renderMods(data) {
  if (window.ConfigEditor) { window.ConfigEditor.background(data); return; }
  const body = $("modsBody");
  const sel = $("modsFile");
  if (!data.ok) {
    body.dataset.state = "error";
    body.innerHTML = `<p class="list-error">${esc(data.error || "нет данных")}</p>`;
    $("modsCount").textContent = "–";
    sel.hidden = true;
    return;
  }
  const files = data.files || [];
  if (files.length > 1) {
    sel.hidden = false;
    if (sel.dataset.current !== data.file) {
      sel.innerHTML = files.map((f) => `<option value="${esc(f)}"${f === data.file ? " selected" : ""}>${esc(f)}</option>`).join("");
      sel.dataset.current = data.file;
    }
  } else {
    sel.hidden = true;
  }
  const mods = data.mods || [];
  const ws = data.workshop || [];
  const canManage = !!data.canManage && !S.demo;
  const bar = $("modsManageBar");
  if (bar) bar.hidden = !canManage;
  const total = data.paired && (data.pairs || []).length
    ? data.pairs.length
    : (ws.length || mods.length);
  $("modsCount").textContent = String(total);
  if (!mods.length && !ws.length) {
    body.dataset.state = "empty";
    body.innerHTML = `<p class="list-empty"><strong>Модов нет.</strong> Параметры Mods= и WorkshopItems= в конфиге пустые.</p>`;
    return;
  }

  let html = "";
  const disabledList = (data.disabled || []);
  if (canManage) {
    const note = data.mappingSource === "container"
      ? "Состав модов из конфига; названия модов читаются внутри контейнера."
      : "Выключатель убирает мод из конфига; изменения применяются рестартом.";
    html += `<p class="mods-note">${esc(note)}</p>`;
  }
  if (data.paired && (data.pairs || []).length) {
    // 1:1 — моды соответствуют Workshop-элементам по порядку
    html += `<p class="mods-note">Моды соответствуют Workshop-элементам по порядку.</p>`;
    html += data.pairs.map((p, i) => `
      <div class="mod-row">
        <span class="m-idx mono">${i + 1}</span>
        <span class="m-name mono" title="${esc(p.mod)}" data-copy="${esc(p.mod)}">${esc(p.mod)}</span>
        <span class="m-ws">
          ${p.url
            ? `<a href="${esc(p.url)}" target="_blank" rel="noopener" title="Steam Workshop · ${esc(p.workshopId)}">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6M20 4l-9 9M10 5H5v14h14v-5"/></svg>
                <span class="ws-title">${esc(p.title || p.workshopId)}</span>
              </a>
              <span class="wid mono" title="Workshop ID: ${esc(p.workshopId)}" data-copy="${esc(p.workshopId)}">${esc(p.workshopId)}</span>`
            : `<span class="wid mono">${esc(p.workshopId || "—")}</span>`}
        </span>
        ${canManage ? _wsSwitch(p.workshopId, true, "Выключить мод в конфиге") : ""}
      </div>`).join("");
  } else {
    // Общий случай: один Workshop-элемент может содержать несколько модов
    if (ws.length) {
      html += `<p class="mods-note">Workshop-элементы — ${ws.length}</p>`;
      if (ws.every((w) => !(w.mods || []).length)) {
        // у элементов нет модов на диске — компактная сетка строк вместо карточек
        html += `<div class="mods-grid">` + ws.map((w) => `
          <div class="mod-line">
            ${w.url
              ? `<a class="m-t" href="${esc(w.url)}" target="_blank" rel="noopener" title="${esc(w.title || w.workshopId)}">${esc(w.title || w.workshopId)}</a>`
              : `<span class="m-t">${esc(w.title || w.workshopId)}</span>`}
            <span class="wid mono" title="Workshop ID: ${esc(w.workshopId)}" data-copy="${esc(w.workshopId)}">${esc(shortWsId(w.workshopId))}</span>
            ${canManage ? _wsSwitch(w.workshopId, true, "Выключить мод в конфиге") : ""}
          </div>`).join("") + `</div>`;
      } else {
        html += ws.map((w) => `
        <div class="ws-card">
          <div class="ws-head">
            ${w.url
              ? `<a href="${esc(w.url)}" target="_blank" rel="noopener" title="Steam Workshop · ${esc(w.workshopId)}">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6M20 4l-9 9M10 5H5v14h14v-5"/></svg>
                  <span class="ws-title">${esc(w.title || w.workshopId)}</span>
                </a>`
              : `<span class="wid mono">${esc(w.workshopId)}</span>`}
            ${w.title ? `<span class="wid mono" title="Workshop ID: ${esc(w.workshopId)}" data-copy="${esc(w.workshopId)}">${esc(w.workshopId)}</span>` : ""}
            ${canManage ? _wsSwitch(w.workshopId, true, "Выключить мод в конфиге") : ""}
          </div>
          ${(w.mods || []).length
            ? `<div class="ws-mods">${w.mods.map((m) => `<span class="chip mono" data-copy="${esc(m)}" title="нажмите — скопировать">${esc(m)}</span>`).join("")}</div>`
            : ""}
        </div>`).join("");
      }
    }
    if (mods.length) {
      html += `<p class="mods-note">Моды из конфига (Mods=) — ${mods.length}</p>`;
      html += `<div class="mods-chips">${mods.map((m) => `<span class="chip mono" data-copy="${esc(m)}" title="нажмите — скопировать">${esc(m)}</span>`).join("")}</div>`;
    }
    if ((data.unbound || []).length && data.mappingSource === "disk") {
      html += `<p class="mods-note">Без привязки к Workshop — ${data.unbound.length}</p>`;
      html += `<div class="mods-chips">${data.unbound.map((m) => `<span class="chip mono" data-copy="${esc(m)}">${esc(m)}</span>`).join("")}</div>`;
    }
    if (!ws.length) {
      html += `<p class="mods-note">Один Workshop-элемент может содержать несколько модов — сопоставление по конфигу невозможно.</p>`;
    }
  }
  if (disabledList.length) {
    html += `<p class="mods-note">Выключенные — ${disabledList.length}</p>`;
    html += disabledList.map((d) => `
      <div class="mod-row disabled-row">
        <span class="m-name mono" title="${esc((d.modIds || []).join(", "))}" data-copy="${esc((d.modIds || [])[0] || d.workshopId)}">${esc(d.title || d.workshopId)}</span>
        <span class="m-ws">
          <span class="wid mono" title="Workshop ID: ${esc(d.workshopId)}" data-copy="${esc(d.workshopId)}">${esc(shortWsId(d.workshopId))}</span>
        </span>
        ${canManage ? _wsSwitch(d.workshopId, false, "Включить мод обратно") : ""}
      </div>`).join("");
  }
  body.dataset.state = "ok";
  body.innerHTML = html;
}

/* реестр модов: SSE кладёт данные в кэш, отрисовка — только на активной странице
   и с учётом активных поиска/фильтра/сортировки */
let modsData = null;
let modsPending = false;
let modsQuery = "";
let modsFilter = "all";
let modsSort = "config";

const modsFilterActive = () => !!(modsQuery.trim() || modsFilter !== "all" || modsSort !== "config");

function renderMods(data) {
  modsData = data;
  if (!data.ok) { _renderMods(data); return; }
  const mods = data.mods || [];
  const ws = data.workshop || [];
  if (!mods.length && !ws.length) { _renderMods(data); return; }
  if (activeView !== "mods") { modsPending = true; return; }
  if (modsFilterActive()) { renderModsFiltered(); return; }
  _renderMods(data);
}

function renderModsFiltered() {
  const data = modsData;
  if (!data || !data.ok) return;
  modsPending = false;
  const q = modsQuery.trim().toLowerCase();
  const matchQ = (...vals) => !q || vals.some((v) => String(v || "").toLowerCase().includes(q));
  const cmp = (a, b) => modsSort === "title"
    ? String(a.title || a.workshopId || "").localeCompare(String(b.title || b.workshopId || ""), "ru")
    : String(a.workshopId || "").localeCompare(String(b.workshopId || ""));
  const view = { ...data };
  let shown = null;

  if (data.paired && (data.pairs || []).length) {
    let pairs = data.pairs.filter((p) => matchQ(p.mod, p.title, p.workshopId));
    if (modsSort !== "config") pairs = [...pairs].sort(cmp);
    shown = [pairs.length, data.pairs.length];
    view.pairs = pairs;
  } else {
    let list = (data.workshop || []).filter((w) =>
      matchQ(w.title, w.workshopId, ...(w.mods || [])) &&
      (modsFilter === "all" ||
        (modsFilter === "disk" ? (w.mods || []).length > 0 : !(w.mods || []).length)));
    if (modsSort !== "config") list = [...list].sort(cmp);
    shown = [list.length, (data.workshop || []).length];
    view.workshop = list;
    view.mods = (data.mods || []).filter((m) => matchQ(m));
    view.unbound = (data.unbound || []).filter((m) => matchQ(m));
  }

  _renderMods(view);

  const totalRaw = data.paired && (data.pairs || []).length
    ? data.pairs.length
    : ((data.workshop || []).length || (data.mods || []).length);
  $("modsCount").textContent = String(totalRaw);

  const note = $("modsShown");
  if (note) {
    if (modsFilterActive() && shown && shown[0] !== shown[1]) {
      note.hidden = false;
      note.textContent = `Показано ${shown[0]} из ${shown[1]}`;
    } else {
      note.hidden = true;
    }
  }

  if (shown && shown[0] === 0 && !(view.mods || []).length) {
    const body = $("modsBody");
    body.dataset.state = "empty";
    body.innerHTML = `<p class="list-empty"><strong>Ничего не найдено.</strong> Измените запрос или сбросьте фильтр.</p>`;
  }
}

$("modsSearch").addEventListener("input", () => {
  modsQuery = $("modsSearch").value;
  renderModsFiltered();
});

/* переключатель состава модов: выключение/включение Workshop-элемента */
$("modsBody").addEventListener("change", async (e) => {
  const sw = e.target.closest("input[data-ws]");
  if (!sw || sw.disabled) return;
  const ws = sw.dataset.ws;
  const enable = sw.checked;
  const file = (modsData && modsData.file) || "";
  sw.disabled = true;
  try {
    const res = await api("/api/mods-config", { method: "POST", body: { file, workshopId: ws, enable } });
    if (res.ok === false || res.error) throw new Error(res.error || "не удалось изменить конфиг");
    toast(enable ? "Мод включён в конфиг — заработает после рестарта"
                 : "Мод выключен из конфига — заработает после рестарта", "ok");
    refreshMods(file);
  } catch (err) {
    toast(err.message || String(err), "error");
    sw.checked = !enable;
  } finally {
    sw.disabled = false;
  }
});
$("modsFilters").addEventListener("click", (e) => {
  const btn = e.target.closest(".chip[data-mf]");
  if (!btn) return;
  modsFilter = btn.dataset.mf;
  document.querySelectorAll("#modsFilters .chip").forEach((c) => c.setAttribute("aria-pressed", String(c === btn)));
  renderModsFiltered();
});
$("modsSort").addEventListener("change", () => {
  modsSort = $("modsSort").value;
  renderModsFiltered();
});

async function refreshMods(file) {
  try {
    file = file || window.ConfigEditor?.file;
    const q = file ? `?file=${encodeURIComponent(file)}` : "";
    renderMods(await api(`/api/mods${q}`));
  } catch (e) { /* тихо */ }
}

$("modsFile").addEventListener("change", () => {
  const sel = $("modsFile");
  sel.dataset.current = "";
  refreshMods(sel.value);
});

/* ───────────────────────── бэкапы ───────────────────────── */

function renderBackups(data) {
  const body = $("backupsBody");
  if (!data.ok) {
    body.dataset.state = "error";
    body.innerHTML = `<p class="list-error">${esc(data.error || "нет данных")}</p>`;
    return;
  }
  renderBkSchedule(data);
  renderBkJournal(data.journal || []);
  const items = data.items || [];
  S.backupsItems = items;
  renderHealth();
  renderKpis();
  if (!items.length) {
    body.dataset.state = "empty";
    body.innerHTML = `<p class="list-empty"><strong>Бэкапов ещё нет.</strong> Нажмите «Создать» — мир и конфиги уйдут в архив.</p>`;
    return;
  }
  body.dataset.state = "ok";
  body.innerHTML = items.map((b) => `
    <div class="backup-row">
      <span class="b-name mono" title="${esc(b.name)} — создан ${esc(b.mtime)}" data-copy="${esc(b.name)}">${esc(b.name)}</span>
      <span class="b-size mono" title="размер архива">${esc(b.sizeText || fmtBytes(b.size))}</span>
      <span class="b-age mono" title="создан ${esc(b.mtime)}">${esc(relTime(b.mtime))}</span>
      <button class="icon-btn" data-b="dl" data-name="${esc(b.name)}" title="Скачать" aria-label="Скачать ${esc(b.name)}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11m0 0 4-4m-4 4-4-4M5 20h14"/></svg>
      </button>
      <button class="icon-btn" data-b="verify" data-name="${esc(b.name)}" title="Проверить архив" aria-label="Проверить ${esc(b.name)}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.4-2.9 8.2-7 10-4.1-1.8-7-5.6-7-10V6l7-3z"/><path d="M9 12l2 2 4-4"/></svg>
      </button>
      <button class="icon-btn" data-b="restore" data-name="${esc(b.name)}" title="Восстановить" aria-label="Восстановить из ${esc(b.name)}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9a8 8 0 1 1 2 6"/><path d="M4 4v5h5"/></svg>
      </button>
      <button class="icon-btn danger" data-b="del" data-name="${esc(b.name)}" title="Удалить" aria-label="Удалить ${esc(b.name)}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V5h6v2m-8 0 1 13h8l1-13"/></svg>
      </button>
    </div>`).join("");

  body.querySelectorAll(".icon-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.name;
      if (btn.dataset.b === "dl") {
        window.location.href = `/api/backup/download?name=${encodeURIComponent(name)}`;
      } else if (btn.dataset.b === "verify") {
        action("verify-backup", { name });
      } else if (btn.dataset.b === "restore") {
        confirmRestore(name);
      } else {
        confirmDeleteBackup(name);
      }
    });
  });
  updateButtons();
}

function renderBkSchedule(data) {
  const ab = data.autoBackup || {};
  const sw = $("bkAutoSwitch");
  if (sw && !sw.matches(":focus")) sw.checked = !!ab.enabled;
  const t = $("bkAutoTime");
  if (t && !t.matches(":focus")) t.value = ab.time || "03:00";
  const keep = $("bkAutoKeep");
  if (keep && !keep.matches(":focus")) keep.value = data.maxBackups ?? 7;
  const stop = $("bkAutoStop");
  if (stop && !stop.matches(":focus")) stop.checked = !!ab.stopServer;
  const next = $("bkAutoNext");
  if (!next) return;
  if (ab.enabled && ab.nextRun) {
    next.hidden = false;
    const d = new Date(ab.nextRun);
    next.textContent = `Следующий запуск: ${d.toLocaleString("ru-RU",
      { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })} (${relTime(ab.nextRun)})`;
  } else {
    next.hidden = true;
  }
}

function renderBkJournal(journal) {
  const body = $("bkJournalBody");
  if (!body) return;
  if (!journal.length) {
    body.dataset.state = "empty";
    body.innerHTML = `<p class="list-empty">Запусков ещё не было — журнал наполнится после первого бэкапа.</p>`;
    return;
  }
  body.dataset.state = "ok";
  const trig = { manual: "вручную", scheduled: "по расписанию" };
  body.innerHTML = journal.map((j) => `
    <div class="journal-row${j.status === "error" ? " j-err" : ""}">
      <span class="j-date mono" title="${esc(j.ts)}">${esc((j.ts || "").slice(0, 16).replace("T", " "))}</span>
      <span class="j-trig">${esc(trig[j.trigger] || j.trigger || "")}</span>
      <span class="j-name mono" title="${esc(j.name || j.error || "")}">${esc(j.name || "—")}</span>
      <span class="j-size mono">${j.status === "error" ? "—" : esc(fmtBytes(j.size))}</span>
      <span class="j-status" title="${esc(j.error || "")}">${j.status === "error" ? "ошибка" : "готово"}</span>
    </div>`).join("");
}

function confirmRestore(name) {
  modal.open({
    title: "Восстановление из бэкапа",
    danger: true,
    okLabel: "Восстановить",
    bodyHTML: `
      <p>Текущий мир и конфиги будут <b>полностью заменены</b> содержимым архива
      <span class="mono">${esc(name)}</span>.</p>
      <p>Сервер будет остановлен через RCON с предупреждением игрокам, затем запущен снова.</p>
      <label class="check"><input type="checkbox" id="restoreAck" /> Я понимаю, что текущее состояние мира будет потеряно</label>
    `,
    onConfirm: async () => {
      if (!$("restoreAck") || !$("restoreAck").checked) throw new Error("Подтвердите замену мира флажком");
      await action("restore", { name });
    },
  });
  $("restoreAck")?.addEventListener("change", () => { $("modalOk").disabled = !$("restoreAck").checked; });
}

function confirmDeleteBackup(name) {
  modal.open({
    title: "Удалить бэкап?",
    danger: true,
    okLabel: "Удалить",
    bodyHTML: `<p>Архив <span class="mono">${esc(name)}</span> будет удалён без возможности восстановления.</p>`,
    onConfirm: async () => {
      const res = await api(`/api/backup?name=${encodeURIComponent(name)}`, { method: "DELETE" });
      if (res.error) throw new Error(res.error);
      toast("Бэкап удалён", "ok");
      refreshBackups();
    },
  });
}

function openBackupModal() {
  modal.open({
    title: "Создать бэкап",
    okLabel: "Создать",
    bodyHTML: `
      <p>Архив собирается из каталога данных сервера (мир, конфиги, whitelist). Логи в бэкап не входят.</p>
      <label class="check"><input type="checkbox" id="bkStop" /> Остановить сервер на время бэкапа (надёжнее для целостности)</label>
      <p>Без остановки пульт сначала отправит команду <span class="mono">save</span> через RCON.</p>
    `,
    onConfirm: async () => {
      await action("backup", { stopServer: $("bkStop")?.checked || false });
    },
  });
}

$("btnBackup").addEventListener("click", openBackupModal);
$("btnNudgeBackup").addEventListener("click", openBackupModal);

/* ───────────────────────── события ───────────────────────── */

const EVENT_LABELS = {
  start: "Запуск", stop: "Остановка", restart: "Рестарт", backup: "Бэкап",
  restore: "Восстановление", update: "Обновление", "update-check": "Проверка",
  auto: "Автообновление", console: "Консоль", warn: "Внимание", delete: "Удаление",
  "rcon-error": "RCON", error: "Ошибка", docker: "Docker", "backup-delete": "Бэкап",
  mods: "Моды",
};

/* фильтры страницы событий: категории группируют типы журнала */
const EVENT_GROUPS = {
  ops: ["start", "stop", "restart", "console", "docker"],
  backup: ["backup", "backup-delete", "restore"],
  update: ["update", "update-check", "auto", "mods"],
  problems: ["error", "rcon-error", "warn", "delete"],
};
let eventsFilter = "all";
let eventsData = null;

function renderEvents(data) {
  eventsData = data;
  const body = $("eventsBody");
  if (!data.ok) {
    body.dataset.state = "error";
    body.innerHTML = `<p class="list-error">${esc(data.error || "нет данных")}</p>`;
    return;
  }
  const items = data.items || [];
  renderRecent(items);
  const visible = eventsFilter === "all"
    ? items
    : items.filter((ev) => (EVENT_GROUPS[eventsFilter] || []).includes(ev.type));
  if (!visible.length) {
    body.dataset.state = "empty";
    body.innerHTML = eventsFilter === "all"
      ? `<p class="list-empty"><strong>Пока тихо.</strong> Здесь появятся рестарты, бэкапы и обновления.</p>`
      : `<p class="list-empty"><strong>Пусто.</strong> Событий этой категории пока не было.</p>`;
    return;
  }
  body.dataset.state = "ok";
  const today = new Date();
  const yest = new Date(today.getTime() - 86400000);
  const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  let html = "";
  let lastDay = "";
  for (const ev of visible) {
    const d = new Date(ev.ts);
    const key = isNaN(d) ? "" : d.toDateString();
    if (key && key !== lastDay) {
      lastDay = key;
      const label = sameDay(d, today) ? "Сегодня" : sameDay(d, yest) ? "Вчера" : dayFmt.format(d);
      html += `<div class="event-day">${esc(label)}</div>`;
    }
    html += `<div class="event-row" data-kind="${esc(ev.type)}">
      <span class="e-time mono" title="${esc(ev.ts)}">${esc(relTime(ev.ts) || fmtTime(ev.ts))}</span>
      <span class="e-text"><b>${esc(EVENT_LABELS[ev.type] || ev.type)}.</b> ${esc(ev.text)}</span>
    </div>`;
  }
  body.innerHTML = html;
}

$("eventFilters").addEventListener("click", (e) => {
  const btn = e.target.closest(".chip[data-ef]");
  if (!btn) return;
  eventsFilter = btn.dataset.ef;
  document.querySelectorAll("#eventFilters .chip").forEach((c) => c.setAttribute("aria-pressed", String(c === btn)));
  if (eventsData) renderEvents(eventsData);
});

/* лента последних событий на обзоре: те же данные, только первые 5 строк */
function renderRecent(items) {
  const body = $("recentBody");
  if (!body) return;
  const slice = (items || []).slice(0, 5);
  if (!slice.length) {
    body.dataset.state = "empty";
    body.innerHTML = `<p class="list-empty"><strong>Пока тихо.</strong> Здесь появятся рестарты, бэкапы и обновления.</p>`;
    return;
  }
  body.dataset.state = "ok";
  body.innerHTML = slice.map((ev) => `
    <div class="event-row" data-kind="${esc(ev.type)}">
      <span class="e-time mono" title="${esc(ev.ts)}">${esc(relTime(ev.ts) || fmtTime(ev.ts))}</span>
      <span class="e-text"><b>${esc(EVENT_LABELS[ev.type] || ev.type)}.</b> ${esc(ev.text)}</span>
    </div>`).join("");
}

/* ───────────────────────── консоль ───────────────────────── */

function consoleAppend(text, cls = "") {
  const out = $("consoleOut");
  const line = document.createElement("span");
  if (cls) line.className = cls;
  line.textContent = text;
  out.appendChild(line);
  out.appendChild(document.createTextNode("\n"));
  while (out.childNodes.length > 800) out.removeChild(out.firstChild);
  out.scrollTop = out.scrollHeight;
  return line;
}

$("consoleForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("consoleInput");
  const cmd = input.value.trim();
  if (!cmd) return;
  if (consoleHistory.items[consoleHistory.items.length - 1] !== cmd) consoleHistory.items.push(cmd);
  consoleHistory.idx = consoleHistory.items.length;
  input.value = "";
  consoleAppend(`> ${cmd}`, "c-dim");
  try {
    const res = await api("/api/rcon", { method: "POST", body: { command: cmd } });
    if (res.error) consoleAppend(`Ошибка: ${res.error}`, "c-err");
    else consoleAppend(res.output || "(без ответа)");
  } catch (err) {
    consoleAppend(`Ошибка: ${err.message || err}`, "c-err");
  }
});

const consoleHistory = { items: [], idx: 0 };

$("consoleInput").addEventListener("keydown", (e) => {
  if (e.key === "ArrowUp" && consoleHistory.items.length) {
    e.preventDefault();
    consoleHistory.idx = consoleHistory.idx === consoleHistory.items.length
      ? consoleHistory.items.length - 1
      : Math.max(0, consoleHistory.idx - 1);
    $("consoleInput").value = consoleHistory.items[consoleHistory.idx] || "";
  } else if (e.key === "ArrowDown" && consoleHistory.items.length) {
    e.preventDefault();
    consoleHistory.idx = Math.min(consoleHistory.items.length, consoleHistory.idx + 1);
    $("consoleInput").value = consoleHistory.idx === consoleHistory.items.length
      ? "" : consoleHistory.items[consoleHistory.idx];
  }
});

document.querySelectorAll("#quickCmds .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    if (chip.dataset.cmd) {
      $("consoleInput").value = chip.dataset.cmd;
      $("consoleForm").requestSubmit();
    } else if (chip.dataset.act === "broadcast") {
      modal.open({
        title: "Объявление игрокам",
        okLabel: "Отправить",
        bodyHTML: `
          <p>Текст уйдёт в игровой чат командой <span class="mono">servermsg</span>.</p>
          <label class="field">Текст объявления
            <input type="text" id="bcText" maxlength="200" style="height:38px;color:var(--ink);background:var(--bg-deep);border:1px solid var(--line-strong);border-radius:8px;padding:0 12px;" />
          </label>
        `,
        onConfirm: async () => {
          const text = ($("bcText")?.value || "").trim();
          if (!text) throw new Error("Введите текст объявления");
          const safe = text.replace(/"/g, "'");
          $("consoleInput").value = `servermsg "${safe}"`;
          $("consoleForm").requestSubmit();
        },
      });
    }
  });
});

/* ───────────────────────── логи ───────────────────────── */

/* docker-таймстемп «2026-09-08T11:36:05.990769902Z » → локальное «15:36:05 » */
const LOG_LEVEL_ERR = /ERROR|SEVERE|Exception/i;
const LOG_LEVEL_WARN = /WARN/i;

function classifyLog(line) {
  if (LOG_LEVEL_ERR.test(line)) return "error";
  if (LOG_LEVEL_WARN.test(line)) return "warn";
  return "";
}

function parseLogs(text) {
  return (text || "").split("\n").map((raw) => {
    const m = raw.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z\s/);
    let view = raw;
    if (m) {
      const ms = (m[3] || "0").slice(0, 3).padEnd(3, "0");
      const dt = new Date(`${m[1]}T${m[2]}.${ms}Z`);
      view = (isNaN(dt) ? m[2] : timeFullFmt.format(dt)) + " " + raw.slice(m[0].length);
    }
    return { raw, view, level: classifyLog(raw) };
  });
}

function renderLogs(data) {
  if (S.overview && S.overview.mode === "remote") {
    $("logsOut").textContent = "Логи контейнера доступны только при запуске пульта на хосте сервера.";
    return;
  }
  if (!data.ok) {
    $("logsError").textContent = (data.error || "Логи недоступны") + (S.logsUpdatedAt ? ` · последние данные: ${fmtTime(S.logsUpdatedAt)}` : "");
    $("logsError").hidden = false;
    if (!S.logsUpdatedAt) $("logsOut").textContent = "Нет данных логов";
    return;
  }
  $("logsError").hidden = true;
  $("logsLimit").hidden = !data.truncated;
  S.logsUpdatedAt = Date.now();
  S.logsLines = parseLogs(data.text);
  renderLogsFiltered();
}

function renderLogsFiltered() {
  const pre = $("logsOut");
  const scrollTop = pre.scrollTop;
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
  const f = ($("logsFilter")?.value || "").trim().toLowerCase();
  const level = S.logsLevel || "all";
  const lines = (S.logsLines || []).filter((l) =>
    (level === "all" || l.level === level) && (!f || l.view.toLowerCase().includes(f)) &&
    (!S.logsSince || Date.parse(l.raw.slice(0, l.raw.indexOf(" "))) >= S.logsSince) &&
    (!S.logsUntil || Date.parse(l.raw.slice(0, l.raw.indexOf(" "))) <= S.logsUntil));
  $("logsScope").hidden = !S.logsSince;
  $("logsPeriod").textContent = S.logsSince ? `Логи операции${S.logsProfile ? ` · ${S.logsProfile}` : ""} · ${fmtTime(S.logsSince)}${S.logsUntil ? ` — ${fmtTime(S.logsUntil)}` : " · продолжается"}` : "";
  // подряд идущий спам (WARN с разными счётчиками/секундами) сжимается в одну строку с бейджем ×N:
  // ключ игнорирует ведущее время и числовые ряды ≥3 цифр
  const merged = [];
  for (const l of lines) {
    const key = l.view.replace(/^\d{2}:\d{2}:\d{2} /, "").replace(/\d{3,}/g, "#");
    const last = merged[merged.length - 1];
    if (last && last.key === key) last.n += 1;
    else merged.push({ view: l.view, level: l.level, key, n: 1 });
  }
  pre.innerHTML = merged.map((l) => {
    const cls = l.level === "error" ? ' class="l-err"' : l.level === "warn" ? ' class="l-warn"' : "";
    const dup = l.n > 1 ? `<span class="l-dup">× ${l.n}</span>` : "";
    return `<span${cls}>${esc(l.view)}${dup}</span>`;
  }).join("\n");
  if (atBottom && S.logsAuto) pre.scrollTop = pre.scrollHeight;
  else pre.scrollTop = scrollTop;
}

$("logsFilter").addEventListener("input", renderLogsFiltered);
$("logsAuto").addEventListener("change", () => { S.logsAuto = $("logsAuto").checked; });
$("logsClearPeriod").addEventListener("click", () => {
  S.logsSince = S.logsUntil = 0;
  S.logsProfile = "";
  renderLogsFiltered();
  refreshLogs();
});
$("logLevels").addEventListener("click", (e) => {
  const btn = e.target.closest(".chip[data-level]");
  if (!btn) return;
  S.logsLevel = btn.dataset.level;
  document.querySelectorAll("#logLevels .chip").forEach((c) => c.setAttribute("aria-pressed", String(c === btn)));
  renderLogsFiltered();
});

/* ───────────────────────── действия и подтверждения ───────────────────────── */

const WARN_OPTIONS = `
  <label class="field">Предупредить игроков
    <select id="warnSel">
      <option value="300" selected>за 5 минут</option>
      <option value="600">за 10 минут</option>
      <option value="60">за 1 минуту</option>
      <option value="0">без предупреждения</option>
    </select>
  </label>`;

$("btnStart").addEventListener("click", () => action("start"));
$("btnCancelMods").addEventListener("click", async () => {
  const btn = $("btnCancelMods");
  btn.disabled = true;
  try {
    const res = await api("/api/action", { method: "POST", body: { op: "cancel-mods-update" } });
    if (res.error) throw new Error(res.error);
    if (S.op?.active?.op === "mods-restart") {
      S.op.active.cancelRequested = true;
      S.op.active.cancellable = false;
    }
    toast("Запрошена отмена автообновления модов", "ok");
  } catch (e) {
    toast(e.message || String(e), "error");
  } finally {
    updateButtons();
  }
});
$("btnStop").addEventListener("click", () => {
  modal.open({
    title: "Остановить сервер?",
    danger: true,
    okLabel: "Остановить",
    bodyHTML: `<p>Мир будет сохранён (RCON <span class="mono">quit</span>), затем контейнер остановится.</p>${WARN_OPTIONS}`,
    onConfirm: async () => action("stop", { warnSeconds: Number($("warnSel").value) }),
  });
});
$("btnRestart").addEventListener("click", () => {
  modal.open({
    title: "Перезапустить сервер?",
    okLabel: "Перезапустить",
    bodyHTML: `<p>Мир будет сохранён, контейнер остановится и запустится снова.</p>${WARN_OPTIONS}`,
    onConfirm: async () => action("restart", { warnSeconds: Number($("warnSel").value) }),
  });
});
$("btnSaveWorld").addEventListener("click", async () => {
  $("consoleInput").value = "save";
  $("consoleForm").requestSubmit();
});
$("btnCheckUpd").addEventListener("click", async () => {
  const btn = $("btnCheckUpd");
  btn.disabled = true;
  try {
    const res = await api("/api/action", { method: "POST", body: { op: "check-update" }, timeout: 25000 });
    if (res.error) throw new Error(res.error);
    const c = res.check || {};
    if (c.available) toast("Доступно обновление образа", "ok");
    else if (c.error) toast(c.error, "error");
    else toast("Обновлений нет — образ актуален", "ok");
    refreshOverview();
  } catch (e) {
    toast(e.message || String(e), "error");
  } finally {
    btn.disabled = false;
  }
});
$("btnApplyUpd").addEventListener("click", () => {
  modal.open({
    title: "Обновить сервер?",
    okLabel: "Обновить",
    bodyHTML: `
      <p>Новый образ скачается заранее, затем при несовпадении digest сервер
      сохранит мир, предупредит игроков и перезапустится на новой версии.</p>
      ${WARN_OPTIONS}
    `,
    onConfirm: async () => action("apply-update", { warnSeconds: Number($("warnSel").value) }),
  });
});

/* ─────────────────────── проверка модов (RCON) ─────────────────────── */

$("btnCheckMods").addEventListener("click", async () => {
  const btn = $("btnCheckMods");
  btn.disabled = true;
  try {
    const res = await api("/api/action", { method: "POST", body: { op: "check-mods-update" } });
    if (res.error) throw new Error(res.error);
    toast("Проверка модов запущена — результат появится в карточке", "ok");
  } catch (e) {
    toast(e.message || String(e), "error");
  } finally {
    btn.disabled = false;
  }
});
$("btnApplyMods").addEventListener("click", () => {
  modal.open({
    title: "Перезапустить для обновления модов?",
    okLabel: "Перезапустить",
    bodyHTML: `
      <p>Сервер предупредит игроков, сохранит мир и перезапустится — при старте
      Steam докачает свежие версии модов из Workshop.</p>
      ${WARN_OPTIONS}
    `,
    onConfirm: async () => action("apply-mods-update", { warnSeconds: Number($("warnSel").value) }),
  });
});

/* рестарт после правок состава модов */
$("btnModsRestart").addEventListener("click", () => {
  modal.open({
    title: "Перезапустить сервер?",
    okLabel: "Перезапустить",
    bodyHTML: `
      <p>Состав модов, включённый в конфиге, заработает после рестарта: игрокам
      придёт предупреждение, мир сохранится (RCON <span class="mono">quit</span>).</p>
      ${WARN_OPTIONS}
    `,
    onConfirm: async () => action("restart", { warnSeconds: Number($("warnSel").value) }),
  });
});

/* ───────────────────────── настройки автообновления ───────────────────────── */

async function pushSettings() {
  const body = {
    autoUpdate: {
      enabled: $("autoSwitch").checked,
      intervalHours: Number($("autoInterval").value),
      warnSeconds: Number($("autoWarn").value),
      backupBeforeUpdate: $("buBackup").checked,
    },
    watchdog: {
      enabled: $("wdSwitch").checked,
      thresholdMin: Number($("wdThreshold").value),
      autoRestart: $("wdRestart").checked,
    },
    modsUpdate: {
      enabled: $("modsAutoSwitch").checked,
      intervalHours: Number($("modsAutoInterval").value),
      restartOnUpdate: $("modsAutoAction").value === "restart",
      warnSeconds: Number($("modsAutoWarn").value),
    },
    telegram: {
      enabled: $("tgSwitch").checked,
      botToken: $("tgToken").value.trim(),
      chatId: $("tgChat").value.trim(),
      groups: {
        ops: $("tgOps").checked,
        backup: $("tgBackup").checked,
        update: $("tgUpdate").checked,
        problems: $("tgProblems").checked,
      },
    },
  };
  try {
    const res = await api("/api/settings", { method: "POST", body });
    if (res.error) throw new Error(res.error);
    toast("Настройки автообновления сохранены", "ok");
    refreshOverview();
  } catch (e) {
    toast(e.message || String(e), "error");
  }
}

$("autoSwitch").addEventListener("change", pushSettings);

/* расписание бэкапов: отдельная карточка на странице «Бэкапы» */
async function pushBkSettings() {
  const body = {
    autoBackup: {
      enabled: $("bkAutoSwitch").checked,
      time: $("bkAutoTime").value || "03:00",
      stopServer: $("bkAutoStop").checked,
    },
    backup: { maxBackups: Number($("bkAutoKeep").value) },
  };
  try {
    const res = await api("/api/settings", { method: "POST", body });
    if (res.error) throw new Error(res.error);
    toast("Расписание бэкапов сохранено", "ok");
    refreshBackups();
  } catch (e) {
    toast(e.message || String(e), "error");
  }
}

$("bkAutoSwitch").addEventListener("change", pushBkSettings);
$("bkAutoTime").addEventListener("change", pushBkSettings);
$("bkAutoKeep").addEventListener("change", pushBkSettings);
$("bkAutoStop").addEventListener("change", pushBkSettings);
$("autoInterval").addEventListener("change", pushSettings);
$("autoWarn").addEventListener("change", pushSettings);
$("buBackup").addEventListener("change", pushSettings);
$("wdSwitch").addEventListener("change", pushSettings);
$("wdThreshold").addEventListener("change", pushSettings);
$("wdRestart").addEventListener("change", pushSettings);
$("modsAutoSwitch").addEventListener("change", pushSettings);
$("modsAutoInterval").addEventListener("change", pushSettings);
$("modsAutoAction").addEventListener("change", pushSettings);
$("modsAutoWarn").addEventListener("change", pushSettings);
$("tgSwitch").addEventListener("change", pushSettings);
$("tgChat").addEventListener("change", pushSettings);
$("tgToken").addEventListener("change", () => {
  pushSettings().then(() => { $("tgToken").value = ""; });
});
["tgOps", "tgBackup", "tgUpdate", "tgProblems"].forEach((id) => $(id).addEventListener("change", pushSettings));
/* «Найти чаты бота»: getUpdates показывает, где бот реально состоит,
   и подставляет настоящий chat id вместо копипасты с ошибками */
$("btnTgChats").addEventListener("click", async () => {
  const btn = $("btnTgChats");
  const body = $("tgChatsBody");
  try {
    btn.disabled = true;
    await pushSettings();          // токен мог быть введён только что
    const res = await api("/api/telegram-chats");
    if (res.error) throw new Error(res.error);
    const chats = res.chats || [];
    body.hidden = false;
    if (!chats.length) {
      body.innerHTML = `<p class="hint">Пока не нашёл ни одного чата: напишите что-нибудь в нужный чат (в группе — любое сообщение боту) и нажмите кнопку ещё раз. Обновления Telegram хранит сутки.</p>`;
      return;
    }
    body.innerHTML = chats.map((c) => `
      <button type="button" class="chat-chip" data-id="${esc(c.id)}" title="Подставить в Chat ID">
        <span>${esc(c.title)}</span><span class="chat-id mono">${esc(c.id)}</span>
      </button>`).join("");
    body.querySelectorAll(".chat-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        $("tgChat").value = chip.dataset.id;
        pushSettings();
        toast(`Chat id ${chip.dataset.id} подставлен и сохранён`, "ok");
      });
    });
  } catch (e) {
    toast(e.message || String(e), "error");
  } finally {
    btn.disabled = false;
  }
});

$("btnTgTest").addEventListener("click", async () => {
  const btn = $("btnTgTest");
  btn.disabled = true;
  try {
    // если токен/chat ввели и сразу нажали «Проверить» — сначала дожимаем сохранение,
    // иначе проверка уйдёт со старыми настройками и скажет «не задан токен»
    await pushSettings();
    const res = await api("/api/notify-test", { method: "POST", body: {} });
    if (res.error) throw new Error(res.error);
    toast("Отправлено — проверьте чат Telegram", "ok");
  } catch (e) {
    toast(e.message || String(e), "error");
  } finally {
    btn.disabled = false;
  }
});

/* ───────────────────────── опрос ───────────────────────── */

let connFailStreak = 0;

function markConnFail() {
  connFailStreak++;
  if (!S.demo) $("connBanner").hidden = connFailStreak < 2;
}

function applyOverview(o) {
  if (o.error && !o.serverName) { markConnFail(); return; }
  connFailStreak = 0;
  $("connBanner").hidden = true;
  if (consoleBootLine) {
    consoleBootLine.textContent = "Консоль готова — команды уходят на сервер по RCON.";
    consoleBootLine = null;
  }
  S.lastDataOk = Date.now();
  updateFreshness();
  renderOverview(o);
}

function applyPlayers(data) { renderPlayers(data); renderKpis(); }
function applyStats(data) { renderStats(data); renderKpis(); }
const applyOps = renderOp;
const applyBackups = renderBackups;
const applyEvents = renderEvents;
const applyMods = renderMods;

function applyLogs(data) {
  renderLogs(data);
}

async function refreshOverview() {
  try { applyOverview(await api("/api/overview")); }
  catch (e) { markConnFail(); }
}

async function refreshPlayers() {
  try { applyPlayers(await api("/api/players")); } catch (e) { /* тихо */ }
}

async function refreshStats() {
  try { applyStats(await api("/api/stats")); } catch (e) { /* тихо */ }
}

async function refreshLogs() {
  const since = S.logsSince, until = S.logsUntil;
  const params = new URLSearchParams();
  if (since) params.set("since", new Date(since).toISOString());
  if (until) params.set("until", new Date(until).toISOString());
  if (since) params.set("tail", "10000");
  try {
    const data = await api("/api/logs" + (params.size ? `?${params}` : ""));
    if (since === S.logsSince && until === S.logsUntil) applyLogs(data);
  } catch (e) {
    if (since === S.logsSince && until === S.logsUntil) applyLogs({ ok: false, error: "Не удалось получить логи. Повторное подключение идёт автоматически." });
  }
}

async function refreshBackups() {
  try { applyBackups(await api("/api/backups")); } catch (e) { /* тихо */ }
}

async function refreshEvents() {
  try { applyEvents(await api("/api/events?limit=200")); } catch (e) { /* тихо */ }
}

async function refreshOps() {
  try { applyOps(await api("/api/ops")); } catch (e) { /* тихо */ }
}

function refreshAll() {
  refreshOverview(); refreshPlayers(); refreshStats(); refreshBackups(); refreshEvents(); refreshPlayersHistory(); refreshStatsHistory(); refreshMods();
}

/* свежесть данных в шапке: время последнего успешного опроса, warn при пропаже связи */
function updateFreshness() {
  const el = $("freshness");
  if (!el) return;
  if (S.demo || !S.lastDataOk) { el.textContent = ""; el.title = ""; el.classList.remove("stale"); return; }
  const updated = timeFullFmt.format(S.lastDataOk);
  const age = Date.now() - S.lastDataOk;
  if (age < 15000) {
    el.classList.remove("stale");
    el.textContent = "Данные обновлены " + updated;
    el.title = "Время последнего успешного получения данных сервера";
  } else {
    el.classList.add("stale");
    const mins = Math.floor(age / 60000);
    el.textContent = "нет данных " + (mins >= 1 ? mins + " мин" : Math.floor(age / 1000) + " с");
    el.title = "Последние данные получены в " + updated;
  }
}

function startPolling() {
  refreshAll();
  setInterval(refreshOverview, 3000);
  setInterval(refreshPlayers, 5000);
  setInterval(refreshStats, 5000);
  setInterval(refreshLogs, 5000);
  setInterval(refreshBackups, 10000);
  setInterval(refreshEvents, 12000);
  setInterval(refreshOps, 1500);
  setInterval(refreshPlayersHistory, 60000);
  setInterval(refreshStatsHistory, 30000);
  setInterval(refreshMods, 60000);
}

/* ───────────────────────── запуск ───────────────────────── */

// Keep fixed navigation, draft controls and notifications clear of each other
// when text wraps, the viewport changes or a device has a bottom safe area.
const layoutObserver = new ResizeObserver(() => {
  for (const [selector, variable] of [[".topbar", "--header-height"], [".nav", "--nav-height"], ["#draftBar", "--draft-height"]]) {
    const height = document.querySelector(selector)?.getBoundingClientRect().height || 0;
    document.documentElement.style.setProperty(variable, `${Math.ceil(height)}px`);
  }
  const focused = document.activeElement, draftBar = $("draftBar");
  if (!draftBar.hidden && focused?.matches("#configFields [data-key], #mapList")) {
    const focusArea = focused.closest("#mapEdit") || focused;
    const overlap = focusArea.getBoundingClientRect().bottom - draftBar.getBoundingClientRect().top + 12;
    if (overlap > 0) window.scrollBy({ top: overlap, behavior: "instant" });
  }
});
for (const selector of [".topbar", ".nav", "#draftBar"]) layoutObserver.observe(document.querySelector(selector));

/* ───────────────────── роутер страниц ───────────────────── */

const VIEWS = {
  overview: "Обзор",
  players: "Игроки",
  mods: "Моды",
  settings: "Настройки сервера",
  maintenance: "Обслуживание",
  backups: "Бэкапы",
  events: "События",
  console: "Консоль",
};

let activeView = null;
let consoleBootLine = null;

function currentRoute() {
  const h = location.hash.replace(/^#\/?/, "");
  return VIEWS[h] ? h : "overview";
}

function applyRoute() {
  const r = currentRoute();
  if (r === activeView) return;
  activeView = r;
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== "view-" + r; });
  document.querySelectorAll(".nav a").forEach((a) => {
    if (a.dataset.route === r) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  if (!["overview", "players", "mods"].includes(r)) $("navMore").setAttribute("aria-current", "page");
  else $("navMore").removeAttribute("aria-current");
  document.title = `${VIEWS[r]} · PZ Пульт`;
  window.scrollTo(0, 0);
  const view = $("view-" + r);
  if (view) view.focus({ preventScroll: true });
  if (r === "mods" && modsPending) renderModsFiltered();
  window.ConfigEditor?.route(r);
}

window.addEventListener("hashchange", applyRoute);

/* ───────────────────── SSE: живой поток данных ───────────────────── */

function startSse() {
  if (typeof EventSource === "undefined") { startPolling(); return; }
  const es = new EventSource("/api/stream");
  es.addEventListener("auth-expired", () => { es.close(); requireLogin(); });
  let messages = 0;
  let errors = 0;
  let fellBack = false;
  const fallback = () => {
    if (fellBack) return;
    fellBack = true;
    es.close();
    startPolling();
  };
  const bind = (name, apply) => es.addEventListener(name, (e) => {
    messages++;
    errors = 0;
    connFailStreak = 0;
    $("connBanner").hidden = true;
    S.lastDataOk = Date.now();
    updateFreshness();
    try { apply(JSON.parse(e.data)); } catch (err) { /* битый кадр пропускаем */ }
  });
  bind("overview", applyOverview);
  bind("players", applyPlayers);
  bind("stats", applyStats);
  bind("logs", data => { if (S.logsSince) refreshLogs(); else applyLogs(data); });
  bind("backups", applyBackups);
  bind("events", applyEvents);
  bind("ops", applyOps);
  bind("stats-history", (d) => renderStatsHistory(d.points || []));
  bind("players-history", (d) => renderPlayersHistory(d.points || []));
  bind("mods", applyMods);
  // браузер сам переподключается; откат на опрос — если поток так и не ожил
  // или умер уже после того, как работал
  es.onerror = () => {
    api("/api/auth/session").catch(() => {});
    errors++;
    if (errors >= 3 && Date.now() - S.lastDataOk > 20000) fallback();
  };
  setTimeout(() => { if (messages === 0) fallback(); }, 9000);
}

async function boot() {
  // тикер свежести живёт всегда: в SSE-режиме при молчащем потоке шапка
  // честно показывает «нет данных N мин», а не замирает на старом времени
  setInterval(updateFreshness, 5000);
  consoleBootLine = consoleAppend("Пульт подключается к серверу…", "c-dim");
  applyRoute();
  if (location.protocol === "file:" || new URLSearchParams(location.search).get("demo") === "1") {
    enterDemo();
    return;
  }
  try {
    const h = await api("/api/health", { timeout: 3500 });
    if (!h.ok) throw new Error("no health");
  } catch (e) {
    $("connBanner").hidden = false;
    $("connBanner").textContent = "Нет связи с пультом. Последние данные сохраняются; повторное подключение идёт автоматически.";
    startPolling();
    return;
  }
  startSse();
}

function enterDemo() {
  S.demo = true;
  $("demoBadge").hidden = false;
  $("demoBanner").hidden = false;
  if (consoleBootLine) {
    consoleBootLine.textContent = "Консоль недоступна — это демо-предпросмотр.";
    consoleBootLine = null;
  }
  consoleAppend("Демо-режим: данные вымышленные, операции отключены.", "c-dim");
  startPolling();
}

boot();
