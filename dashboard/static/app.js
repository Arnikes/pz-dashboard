/* PZ Пульт · V4 — логика интерфейса.
   Данные приходят с бэкенда пульта; при отсутствии API включается демо-режим.
   Режим «remote»: пульт вне хоста сервера — управление только по RCON. */
"use strict";

/* ───────────────────────── утилиты ───────────────────────── */

const $ = (id) => document.getElementById(id);

const esc = (s) => String(s ?? "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

const timeFmt = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" });
const dateFmt = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

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

  function open({ title, bodyHTML, okLabel = "Подтвердить", danger = false, onConfirm }) {
    $("modalTitle").textContent = title;
    $("modalTitle").classList.toggle("danger", danger);
    $("modalBody").innerHTML = bodyHTML;
    const okBtn = $("modalOk");
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
  }

  $("modalCancel").addEventListener("click", close);
  $("modalBackdrop").addEventListener("click", close);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !root.hidden) close(); });
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
  cpuHistory: [],
  logsAuto: true,
  lastOpActive: false,
};

function demoNow() { return new Date().toISOString(); }

const DEMO = {
  overview: () => ({
    ok: true, serverName: "Кастом-Нокс (демо)", container: "pzserver",
    image: "indifferentbrokkoli/pzserver:latest", docker: true, compose: true,
    rconConfigured: true, rcon: { state: "ok", error: null, at: demoNow() },
    containerInfo: { status: "running", running: true, startedAt: new Date(Date.now() - 569000 * 1000).toISOString(), image: "indifferentbrokkoli/pzserver:latest", uptimeSec: 569000 },
    update: { at: new Date(Date.now() - 2 * 3600 * 1000).toISOString(), local: "sha256:9f21a4c0e7b2d8f1a3c5e7b9d1f3a5c7e9b1d3f5a7c9e1b3d5f7a9c1e3b5d7f9", remote: "sha256:9f21a4c0e7b2d8f1a3c5e7b9d1f3a5c7e9b1d3f5a7c9e1b3d5f7a9c1e3b5d7f9", available: false, error: null },
    settings: { autoUpdate: { enabled: true, intervalHours: 6, warnSeconds: 300, backupBeforeUpdate: true }, backup: { stopServer: false, maxBackups: 10 }, watchdog: { enabled: true, thresholdMin: 5, autoRestart: false }, nextCheck: Date.now() / 1000 + 3600 * 4 },
    watchdog: { lastProbeAt: demoNow(), lastResult: "ok", lastError: null, consecutiveFailures: 0, alerted: false },
    backupsCount: 2, now: demoNow(),
  }),
  players: () => ({ ok: true, names: ["Дмитрий", "Sledge", "Katya_V"], raw: "Дмитрий\nSledge\nKatya_V", count: 3 }),
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
    ok: true, maxBackups: 10,
    items: [
      { name: "pz-backup-20260901-040000.tar.gz", size: 684000000, sizeText: "652.3 МБ", mtime: "2026-09-01T04:00:00" },
      { name: "pz-backup-20260831-040000.tar.gz", size: 672000000, sizeText: "640.9 МБ", mtime: "2026-08-31T04:00:00" },
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

async function api(path, opts = {}) {
  if (S.demo) {
    if (opts.method && opts.method !== "GET") {
      throw new Error("Демо-режим: операции недоступны");
    }
    await new Promise((r) => setTimeout(r, 120));
    if (path.startsWith("/api/overview")) return DEMO.overview();
    if (path.startsWith("/api/players/history")) return DEMO.history();
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
      headers: opts.body ? { "Content-Type": "application/json" } : undefined,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      signal: ctrl.signal,
    });
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
    toast(`Операция «${op}» запущена`, "ok");
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
  if (!o.rconConfigured) setPill("pillRcon", "bad", "RCON: нет пароля");
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
  $("updRemote").textContent = shortDigest(u.remote);
  $("updChecked").textContent = u.at ? fmtTime(u.at) : "никогда";

  // автообновление
  const au = o.settings?.autoUpdate || {};
  if (!$("autoSwitch").matches(":focus")) $("autoSwitch").checked = !!au.enabled;
  if (!$("autoInterval").matches(":focus")) $("autoInterval").value = String(au.intervalHours ?? 6);
  if (!$("autoWarn").matches(":focus")) $("autoWarn").value = String(au.warnSeconds ?? 300);
  if (!$("buBackup").matches(":focus")) $("buBackup").checked = au.backupBeforeUpdate !== false;
  const wdCfg = o.settings?.watchdog || {};
  if (!$("wdSwitch").matches(":focus")) $("wdSwitch").checked = !!wdCfg.enabled;
  if (!$("wdThreshold").matches(":focus")) $("wdThreshold").value = String(wdCfg.thresholdMin ?? 5);
  if (!$("wdRestart").matches(":focus")) $("wdRestart").checked = !!wdCfg.autoRestart;
  const wds = o.watchdog || {};
  if (wdCfg.enabled) {
    const fails = wds.consecutiveFailures || 0;
    $("wdStatus").hidden = false;
    $("wdStatus").className = "hint mono " + (fails ? "bad" : "ok");
    $("wdStatus").textContent = `проба ${fmtTime(wds.lastProbeAt)} · сбоев подряд: ${fails}` +
      (fails && wds.lastError ? ` · ${wds.lastError}` : "");
  } else {
    $("wdStatus").hidden = true;
  }
  const next = o.settings?.nextCheck;
  $("autoNext").hidden = !(au.enabled && next);
  if (au.enabled && next) $("autoNext").textContent = `Следующая проверка: ${new Date(next * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}`;

  updateButtons();
}

function updateButtons() {
  const o = S.overview;
  const busy = !!(S.op && S.op.active);
  const remote = !!(o && o.mode === "remote");
  const rconOk = !!(o && o.rcon && o.rcon.state === "ok");
  const running = !remote && !!(o && o.containerInfo && o.containerInfo.running);
  const found = !!(o && o.containerInfo);
  const hostHint = "Доступно только при запуске пульта на хосте сервера";
  const consoleLive = busy ? false : (remote ? rconOk : running);
  $("btnStart").disabled = busy || remote || !found || running;
  $("btnStop").disabled = busy || remote || !running;
  $("btnRestart").disabled = busy || remote || !running;
  $("btnSaveWorld").disabled = !consoleLive;
  $("btnCheckUpd").disabled = busy || remote;
  $("btnApplyUpd").disabled = busy || remote || (o && o.compose === false);
  for (const id of ["btnStart", "btnStop", "btnRestart", "btnCheckUpd", "btnApplyUpd", "btnBackup"]) {
    $(id).title = remote ? hostHint : (id === "btnApplyUpd" && o && o.compose === false
      ? "Недоступен плагин docker compose в контейнере пульта" : "");
  }
  $("btnBackup").disabled = busy || remote;
  $("wdRestart").disabled = busy || remote;
  $("buBackup").disabled = busy;
  $("wdSwitch").disabled = busy;
  $("wdThreshold").disabled = busy;
  document.querySelectorAll("#backupsBody .icon-btn").forEach((b) => { b.disabled = busy || remote; });
  document.querySelectorAll("#quickCmds .chip").forEach((b) => { b.disabled = !consoleLive; });
  $("consoleInput").disabled = !consoleLive;
  $("consoleForm").querySelector("button").disabled = !consoleLive;
}

function renderOp(op) {
  const active = op && op.active;
  if (active) {
    $("opbar").hidden = false;
    $("opPhase").textContent = `${active.op}: ${active.phase}`;
    $("opMsg").textContent = active.message || "";
  } else {
    $("opbar").hidden = true;
  }
  if (S.lastOpActive && !active && op && op.history && op.history[0]) {
    const h = op.history[0];
    toast(h.ok ? `Готово: ${h.message || h.op}` : `Не удалось: ${h.message || h.op}`, h.ok ? "ok" : "error", 8000);
    refreshAll();
  }
  S.lastOpActive = !!(active);
  S.op = op;
  updateButtons();
}

/* ───────────────────────── игроки ───────────────────────── */

function renderPlayers(data) {
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
  body.innerHTML = data.names.map((n) => `<div class="player-row"><span class="dot"></span>${esc(n)}</div>`).join("");
}

/* ───────────────────────── график онлайна за сутки ───────────────────────── */

function renderPlayersHistory(points) {
  const svg = $("phSpark");
  if (!points || points.length < 2) {
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
}

async function refreshPlayersHistory() {
  try { renderPlayersHistory((await api("/api/players/history")).points || []); } catch (e) { /* тихо */ }
}

/* ───────────────────────── метрики ───────────────────────── */

function pushCpu(v) {
  S.cpuHistory.push(v);
  if (S.cpuHistory.length > 60) S.cpuHistory.shift();
  const pts = S.cpuHistory.map((val, i) => `${(i / 59) * 120},${28 - Math.max(1, Math.min(28, (val / 100) * 28))}`).join(" ");
  $("cpuSparkLine").setAttribute("points", pts);
}

function renderStats(st) {
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
  pushCpu(cpu);
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

/* ───────────────────────── бэкапы ───────────────────────── */

function renderBackups(data) {
  const body = $("backupsBody");
  if (!data.ok) {
    body.dataset.state = "error";
    body.innerHTML = `<p class="list-error">${esc(data.error || "нет данных")}</p>`;
    return;
  }
  const items = data.items || [];
  if (!items.length) {
    body.dataset.state = "empty";
    body.innerHTML = `<p class="list-empty"><strong>Бэкапов ещё нет.</strong> Нажмите «Создать» — мир и конфиги уйдут в архив.</p>`;
    return;
  }
  body.dataset.state = "ok";
  body.innerHTML = items.map((b) => `
    <div class="backup-row">
      <span class="b-name mono" title="${esc(b.name)}">${esc(b.name)}</span>
      <span class="b-size">${esc(b.sizeText || fmtBytes(b.size))}</span>
      <button class="icon-btn" data-b="dl" data-name="${esc(b.name)}" title="Скачать" aria-label="Скачать ${esc(b.name)}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11m0 0 4-4m-4 4-4-4M5 20h14"/></svg>
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
      } else if (btn.dataset.b === "restore") {
        confirmRestore(name);
      } else {
        confirmDeleteBackup(name);
      }
    });
  });
  updateButtons();
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

$("btnBackup").addEventListener("click", () => {
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
});

/* ───────────────────────── события ───────────────────────── */

const EVENT_LABELS = {
  start: "Запуск", stop: "Остановка", restart: "Рестарт", backup: "Бэкап",
  restore: "Восстановление", update: "Обновление", "update-check": "Проверка",
  auto: "Автообновление", console: "Консоль", warn: "Внимание", delete: "Удаление",
  "rcon-error": "RCON", error: "Ошибка", docker: "Docker", "backup-delete": "Бэкап",
};

function renderEvents(data) {
  const body = $("eventsBody");
  if (!data.ok) {
    body.dataset.state = "error";
    body.innerHTML = `<p class="list-error">${esc(data.error || "нет данных")}</p>`;
    return;
  }
  const items = data.items || [];
  if (!items.length) {
    body.dataset.state = "empty";
    body.innerHTML = `<p class="list-empty"><strong>Пока тихо.</strong> Здесь появятся рестарты, бэкапы и обновления.</p>`;
    return;
  }
  body.dataset.state = "ok";
  body.innerHTML = items.map((ev) => `
    <div class="event-row" data-kind="${esc(ev.type)}">
      <span class="e-time mono">${esc(fmtTime(ev.ts))}</span>
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
}

$("consoleForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("consoleInput");
  const cmd = input.value.trim();
  if (!cmd) return;
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

function renderLogs(data) {
  if (S.overview && S.overview.mode === "remote") {
    $("logsOut").textContent = "Логи контейнера доступны только при запуске пульта на хосте сервера.";
    return;
  }
  if (!data.ok) {
    $("logsOut").textContent = data.error || "Логи недоступны";
    return;
  }
  const pre = $("logsOut");
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
  pre.innerHTML = data.text.split("\n").map((line) => {
    if (/ERROR|SEVERE|Exception/i.test(line)) return `<span class="l-err">${esc(line)}</span>`;
    if (/WARN/i.test(line)) return `<span class="l-warn">${esc(line)}</span>`;
    return esc(line);
  }).join("\n");
  if (atBottom && S.logsAuto) pre.scrollTop = pre.scrollHeight;
}

$("logsAuto").addEventListener("change", () => { S.logsAuto = $("logsAuto").checked; });

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
$("autoInterval").addEventListener("change", pushSettings);
$("autoWarn").addEventListener("change", pushSettings);
$("buBackup").addEventListener("change", pushSettings);
$("wdSwitch").addEventListener("change", pushSettings);
$("wdThreshold").addEventListener("change", pushSettings);
$("wdRestart").addEventListener("change", pushSettings);

/* ───────────────────────── опрос ───────────────────────── */

let connFailStreak = 0;

async function refreshOverview() {
  try {
    const o = await api("/api/overview");
    if (o.error && !o.serverName) throw new Error(o.error);
    connFailStreak = 0;
    $("connBanner").hidden = true;
    renderOverview(o);
  } catch (e) {
    connFailStreak++;
    if (!S.demo) $("connBanner").hidden = connFailStreak < 2;
  }
}

async function refreshPlayers() {
  try { renderPlayers(await api("/api/players")); } catch (e) { /* тихо */ }
}

async function refreshStats() {
  try { renderStats(await api("/api/stats")); } catch (e) { /* тихо */ }
}

async function refreshLogs() {
  if (!S.logsAuto && !$("logsOut").textContent.startsWith("Загрузка")) return;
  try { renderLogs(await api("/api/logs")); } catch (e) { /* тихо */ }
}

async function refreshBackups() {
  try { renderBackups(await api("/api/backups")); } catch (e) { /* тихо */ }
}

async function refreshEvents() {
  try { renderEvents(await api("/api/events")); } catch (e) { /* тихо */ }
}

async function refreshOps() {
  try { renderOp(await api("/api/ops")); } catch (e) { /* тихо */ }
}

function refreshAll() {
  refreshOverview(); refreshPlayers(); refreshStats(); refreshBackups(); refreshEvents(); refreshPlayersHistory();
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
}

function startClock() {
  const tick = () => { $("clock").textContent = new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }); };
  tick();
  setInterval(tick, 15000);
}

/* ───────────────────────── запуск ───────────────────────── */

async function boot() {
  startClock();
  consoleAppend("Пульт подключается к серверу…", "c-dim");
  if (location.protocol === "file:") {
    enterDemo();
    return;
  }
  try {
    const h = await api("/api/health", { timeout: 3500 });
    if (!h.ok) throw new Error("no health");
  } catch (e) {
    enterDemo();
    return;
  }
  startPolling();
}

function enterDemo() {
  S.demo = true;
  $("demoBadge").hidden = false;
  $("demoBanner").hidden = false;
  consoleAppend("Демо-режим: данные вымышленные, операции отключены.", "c-dim");
  startPolling();
}

boot();
