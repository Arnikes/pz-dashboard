/* PZ Пульт · V19 — логика интерфейса.
   V19: подпись события «mods» в журнале; host-only автонастройки глушатся в
   remote-режиме; предупреждение о потере связи работает и в SSE-режиме.
   Мультистраничный каркас: hash-роутинг (#/overview, #/mods, …), 7 страниц,
   SSE-поток /api/stream живёт между переключениями; при недоступности — опрос.
   При отсутствии API включается демо-режим.
   Режим «remote»: пульт вне хоста сервера — управление только по RCON. */
"use strict";

/* ───────────────────────── утилиты ───────────────────────── */

const $ = (id) => document.getElementById(id);

function setDomProperty(target, property, value) {
  if (typeof target[property] === "boolean") value = !!value;
  if (target[property] !== value) target[property] = value;
}
function setDomAttribute(target, attribute, value) {
  if (target.getAttribute(attribute) !== String(value)) target.setAttribute(attribute, value);
}
const renderedMarkup = new WeakMap();
function setStaticMarkup(target, html) {
  if (renderedMarkup.get(target) === html) return;
  target.innerHTML = html;
  renderedMarkup.set(target, html);
}

const esc = (s) => String(s ?? "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

let helpSequence = 0;
function helpTip(text, label, id = `help-tip-${++helpSequence}`) {
  return `<span class="help-tip"><button type="button" class="help-trigger" data-help="${esc(id)}" aria-label="${esc(label)}" aria-describedby="${esc(id)}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 11v6M12 7v2"/></svg></button><span id="${esc(id)}" class="help-content" role="tooltip" popover="manual" hidden>${esc(text)}</span></span>`;
}
(() => {
  let active = null, pinned = false, leaveTimer = null;
  const close = () => {
    clearTimeout(leaveTimer);
    const tip = active && $(active.dataset.help);
    if (tip) { tip.hidePopover(); tip.hidden = true; }
    active = null; pinned = false;
  };
  const position = () => {
    const tip = active && $(active.dataset.help);
    if (!tip || !active.isConnected) { close(); return; }
    const rect = active.getBoundingClientRect();
    const width = document.documentElement.clientWidth;
    const bar = $("draftBar"), nav = document.querySelector(".nav");
    const height = Math.min(document.documentElement.clientHeight,
      bar && !bar.hidden ? bar.getBoundingClientRect().top : Infinity,
      matchMedia("(max-width:740px)").matches ? nav.getBoundingClientRect().top : Infinity);
    if (rect.bottom <= 0 || rect.top >= height) { close(); return; }
    tip.style.maxWidth = `${Math.max(0, Math.min(360, width - 24))}px`;
    // Keep long help scrollable without covering its own trigger.
    tip.style.maxHeight = `${Math.max(0, Math.max(rect.top - 18, height - rect.bottom - 18))}px`;
    const box = tip.getBoundingClientRect();
    tip.style.left = `${Math.max(12, Math.min(rect.left, width - box.width - 12))}px`;
    const below = rect.bottom + 6;
    tip.style.top = `${Math.max(12, Math.min(height - box.height - 12, below + box.height <= height - 12 ? below : rect.top - box.height - 6))}px`;
  };
  const open = button => {
    clearTimeout(leaveTimer);
    if (active !== button) close();
    active = button;
    const tip = $(button.dataset.help);
    if (!tip) { close(); return; }
    tip.hidden = false;
    // The top layer keeps viewport coordinates valid inside animated views.
    tip.showPopover();
    position();
  };
  document.addEventListener("pointerover", event => {
    if (active && active.closest(".help-tip").contains(event.target)) clearTimeout(leaveTimer);
    const button = event.target.closest("[data-help]");
    if (button && !pinned && event.pointerType !== "touch") open(button);
  });
  document.addEventListener("pointerout", event => {
    if (active && !pinned && !active.closest(".help-tip").contains(event.relatedTarget) && document.activeElement !== active) leaveTimer = setTimeout(close, 150);
  });
  document.addEventListener("focusin", event => {
    const button = event.target.closest("[data-help]");
    if (button) open(button);
    else close();
  });
  document.addEventListener("focusout", event => {
    if (active && !active.closest(".help-tip").contains(event.relatedTarget)) close();
  });
  document.addEventListener("click", event => {
    const button = event.target.closest("[data-help]");
    if (button) {
      if (active === button && pinned) close();
      else { open(button); pinned = true; }
    } else if (active && !active.closest(".help-tip").contains(event.target)) close();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && active) { close(); event.preventDefault(); }
  });
  window.addEventListener("scroll", event => {
    const tip = active && $(active.dataset.help);
    if (tip && !(event.target instanceof Node && tip.contains(event.target))) position();
  }, true);
  window.addEventListener("resize", close);
  window.addEventListener("hashchange", close);
})();

const timeFmt = new Intl.DateTimeFormat(I18n.locale, { hour: "2-digit", minute: "2-digit" });
const dateFmt = new Intl.DateTimeFormat(I18n.locale, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
const timeFullFmt = new Intl.DateTimeFormat(I18n.locale, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const dayFmt = new Intl.DateTimeFormat(I18n.locale, { day: "2-digit", month: "2-digit" });
const relativeDayFmt = new Intl.RelativeTimeFormat(I18n.locale, { numeric: "auto" });

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  return dateFmt.format(d);
}

function fmtUptime(sec) {
  if (sec == null) return "—";
  if (sec < 60) return I18n.msg`${sec} с`;
  const m = Math.floor(sec / 60);
  if (m < 60) return I18n.msg`${m} мин`;
  const h = Math.floor(m / 60);
  if (h < 24) return I18n.msg`${h} ч ${m % 60} мин`;
  return I18n.msg`${Math.floor(h / 24)} д ${h % 24} ч`;
}

function fmtBytes(n) {
  if (n == null) return "—";
  const units = [I18n.t("Б"), I18n.t("КБ"), I18n.t("МБ"), I18n.t("ГБ"), I18n.t("ТБ")];
  let v = Number(n) || 0, u = 0;
  while (v >= 1024 && u < units.length - 1) { v /= 1024; u++; }
  return `${I18n.number(v, { minimumFractionDigits: u === 0 ? 0 : 1, maximumFractionDigits: u === 0 ? 0 : 1 })} ${units[u]}`;
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
    if (f < 3600) return I18n.msg`через ${Math.max(1, Math.round(f / 60))} мин`;
    if (f < 86400) return I18n.msg`через ${Math.round(f / 3600)} ч`;
    return fmtTime(iso);
  }
  if (s < 45) return I18n.t("только что");
  if (s < 3600) return I18n.msg`${Math.max(1, Math.round(s / 60))} мин назад`;
  if (s < 86400) return I18n.msg`${Math.round(s / 3600)} ч назад`;
  if (s < 172800) return I18n.t("вчера");
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

function copyIcon() {
  return `<svg class="copy-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><g class="copy-icon-default"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V4H4v12h4"/></g><path class="copy-icon-check" d="m5 12 4 4L19 6"/></svg>`;
}

function copyValue(value, { text = value, className = "", title = value, label = I18n.msg`Скопировать ${value}` } = {}) {
  return `<button type="button" class="copy-value ${esc(className)}" data-copy="${esc(value)}" title="${esc(title)}" aria-label="${esc(label)}"><span class="copy-label">${esc(text)}</span>${copyIcon()}</button>`;
}

document.querySelectorAll(".copy-value").forEach(button => button.insertAdjacentHTML("beforeend", copyIcon()));
const copyFeedback = new WeakMap();
const copyStatus = document.createElement("span");
copyStatus.className = "sr-only";
copyStatus.setAttribute("role", "status");
document.body.appendChild(copyStatus);

function resetCopyFeedback(button) {
  const state = copyFeedback.get(button);
  if (state) {
    clearTimeout(state.timer);
    button.setAttribute("aria-label", state.label);
    button.title = state.title;
    copyFeedback.delete(button);
  }
  delete button.dataset.copied;
}

async function copyText(text, button = null) {
  let state;
  if (button) {
    resetCopyFeedback(button);
    state = { label: button.getAttribute("aria-label"), title: button.title };
    copyFeedback.set(button, state);
    copyStatus.textContent = "";
  }
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
    if (button) {
      // Ignore an older request or a field whose value changed while copying.
      if (copyFeedback.get(button) !== state || button.dataset.copy !== text || !button.isConnected) return;
      button.dataset.copied = "true";
      button.setAttribute("aria-label", I18n.t("Скопировано"));
      button.title = I18n.t("Скопировано");
      copyStatus.textContent = I18n.t("Скопировано");
      state.timer = setTimeout(() => resetCopyFeedback(button), 2000);
    } else {
      toast(I18n.t("Скопировано"), "ok", 2000);
    }
  } catch (e) {
    if (button && copyFeedback.get(button) === state) resetCopyFeedback(button);
    toast(I18n.t("Не удалось скопировать"), "error", 2000);
  }
}

document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-copy]");
  if (t && !t.disabled && t.dataset.copy) copyText(t.dataset.copy, t);
});

function setCopyTarget(id, value, text = value || "—") {
  const button = $(id);
  if (button.dataset.copy !== (value || "")) resetCopyFeedback(button);
  button.disabled = !value;
  if (value) button.dataset.copy = value;
  else delete button.dataset.copy;
  setDomProperty(button.querySelector(".copy-label"), "textContent", text);
}

function syncTabAccessibility(id) {
  const tabs = [...$(id).querySelectorAll('[role="tab"]')];
  for (const tab of tabs) {
    const selected = tab.getAttribute("aria-selected") === "true";
    tab.id = `${id}-${tab.dataset.tab}`;
    tab.tabIndex = selected ? 0 : -1;
    const panel = document.getElementById(tab.getAttribute("aria-controls"));
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", tab.id);
    panel.tabIndex = 0;
    panel.hidden = !selected;
    if (selected && id === "configTabs" && ["server", "world", "custom"].includes(tab.dataset.tab)) {
      const fields = $("configSharedFields");
      if (fields.parentElement !== panel) panel.appendChild(fields);
    }
  }
}
for (const id of ["configTabs", "modTabs"]) syncTabAccessibility(id);

/* ───────────────────────── тосты ───────────────────────── */

function notificationIcon(kind) {
  const paths = {
    ok: '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
    error: '<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6m0-6-6 6"/>',
    warning: '<path d="m12 3 10 18H2L12 3Z"/><path d="M12 9v5m0 3v1"/>',
    pending: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v1"/>',
  };
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[kind] || paths.info}</svg>`;
}

// One inbox for local feedback and server results. Stable operation IDs survive SSE replay.
const Notifications = (() => {
  const storageKey = "pz-notifications-v1";
  const panel = $("notificationCenter"), trigger = $("btnNotifications"), list = $("notificationList");
  let items = [], seen = [], ignoredBefore = 0, filter = "all";
  const kindTitles = () => ({ ok: I18n.t("Готово"), error: I18n.t("Ошибка"), warning: I18n.t("Отменено"), pending: I18n.t("Выполняется"), info: I18n.t("Информация") });
  const matches = item => filter === "all" || (filter === "unread" ? !item.read : item.kind === filter);
  function load(value) {
    try {
      const data = JSON.parse(value);
      if (!data || !Array.isArray(data.items) || !Array.isArray(data.seen)) return;
      items = data.items.filter(item => item && typeof item.id === "string" && typeof item.message === "string" && typeof item.title === "string" && ["ok", "error", "warning", "info"].includes(item.kind) && Number.isFinite(new Date(item.createdAt).getTime())).slice(0, 100);
      seen = data.seen.filter(receipt => typeof receipt?.id === "string" && Number.isFinite(receipt.time)).slice(-500);
      ignoredBefore = Number.isFinite(data.ignoredBefore) ? data.ignoredBefore : 0;
    } catch { /* A corrupt or unavailable browser store must not break the dashboard. */ }
  }
  try { load(localStorage.getItem(storageKey)); } catch { /* Keep an in-memory inbox. */ }
  function save() {
    while (seen.length > 500) ignoredBefore = Math.max(ignoredBefore, seen.shift().time);
    try { localStorage.setItem(storageKey, JSON.stringify({ items: items.filter(item => item.kind !== "pending"), seen, ignoredBefore })); } catch { /* Storage can be blocked or full. */ }
  }
  function updateControls() {
    const unread = items.filter(item => !item.read).length;
    const count = $("notificationCount");
    count.hidden = !unread;
    count.textContent = unread > 99 ? "99+" : String(unread);
    trigger.setAttribute("aria-label", unread ? I18n.msg`Уведомления: непрочитанных ${unread}` : I18n.t("Уведомления"));
    $("notificationClear").disabled = !items.length;
    $("notificationReadAll").disabled = !unread;
  }
  function render() {
    updateControls();
    const visible = items.filter(matches);
    const fragment = document.createDocumentFragment();
    for (const item of visible) {
      const row = document.createElement("article");
      row.className = "notification-item";
      row.dataset.id = item.id;
      row.dataset.kind = item.kind;
      row.dataset.read = String(!!item.read);
      row.innerHTML = `<span class="notification-icon">${notificationIcon(item.kind)}</span><div class="notification-content"><div class="notification-item-heading"><strong>${esc(item.title || kindTitles()[item.kind])}</strong>${!item.read ? '<span class="notification-unread" aria-hidden="true"></span>' : ""}</div><p>${esc(item.message)}</p><div class="notification-meta"><span>${esc(kindTitles()[item.kind])}</span><time datetime="${new Date(item.createdAt).toISOString()}">${esc(fmtTime(new Date(item.createdAt).toISOString()))}</time></div></div>`;
      const content = row.querySelector(".notification-content");
      if (item.operation) {
        const actions = document.createElement("div");
        actions.className = "notification-actions";
        actions.innerHTML = `<a href="#/console">${esc(I18n.t("Посмотреть логи"))}</a><a href="#/events">${esc(I18n.t("События"))}</a>`;
        actions.addEventListener("click", () => { markRead([item.id]); panel.hidePopover(); });
        content.append(actions);
      }
      if (!item.read) {
        const read = document.createElement("button");
        read.type = "button";
        read.className = "notification-read";
        read.setAttribute("aria-label", I18n.msg`Прочитать уведомление: ${item.title || item.message}`);
        read.innerHTML = notificationIcon("ok");
        read.addEventListener("click", () => { markRead([item.id]); $("notificationReadAll").disabled ? $("notificationClose").focus() : $("notificationReadAll").focus(); });
        row.append(read);
      }
      fragment.append(row);
    }
    list.replaceChildren(fragment);
    $("notificationEmpty").hidden = !!visible.length;
    $("notificationEmpty").textContent = I18n.t(!items.length ? "Пока нет уведомлений" : filter === "unread" ? "Все уведомления прочитаны" : "Нет уведомлений в этом фильтре");
  }
  function markRead(ids) {
    const selected = new Set(ids);
    items.forEach(item => { if (selected.has(item.id)) item.read = true; });
    save(); render();
    showToast.dismissIds(selected);
  }
  function readVisible() {
    // Capture the current filter before marking so the unread filter empties predictably.
    markRead(items.filter(matches).map(item => item.id));
  }
  function position() {
    if (!panel.matches(":popover-open")) return;
    const rect = trigger.getBoundingClientRect();
    const width = document.documentElement.clientWidth;
    const bottom = Math.min(innerHeight - 12, document.querySelector(".site-footer")?.getBoundingClientRect().top || innerHeight - 12,
      !$("draftBar").hidden && getComputedStyle($("draftBar")).position === "fixed" ? $("draftBar").getBoundingClientRect().top - 12 : innerHeight - 12,
      matchMedia("(max-width:740px)").matches ? document.querySelector(".nav").getBoundingClientRect().top - 12 : innerHeight - 12);
    panel.style.width = `${Math.min(440, width - 24)}px`;
    panel.style.maxHeight = `${Math.max(100, bottom - rect.bottom - 10)}px`;
    panel.style.top = `${rect.bottom + 8}px`;
    panel.style.left = `${Math.max(12, Math.min(rect.right - panel.offsetWidth, width - panel.offsetWidth - 12))}px`;
  }
  panel.addEventListener("beforetoggle", event => {
    if (event.newState === "open") { render(); readVisible(); }
  });
  panel.addEventListener("toggle", event => {
    trigger.setAttribute("aria-expanded", String(event.newState === "open"));
    if (event.newState === "open") { position(); $("notificationClose").focus({ preventScroll: true }); }
    if (event.newState === "closed" && (panel.contains(document.activeElement) || document.activeElement === document.body)) trigger.focus({ preventScroll: true });
  });
  $("notificationClose").addEventListener("click", () => panel.hidePopover());
  $("notificationReadAll").addEventListener("click", () => { markRead(items.map(item => item.id)); $("notificationClose").focus(); });
  $("notificationClear").addEventListener("click", () => {
    showToast.clear();
    items = []; save(); render(); $("notificationClose").focus();
  });
  panel.querySelectorAll("[data-notification-filter]").forEach(button => button.addEventListener("click", () => {
    filter = button.dataset.notificationFilter;
    panel.querySelectorAll("[data-notification-filter]").forEach(control => control.setAttribute("aria-pressed", String(control === button)));
    render(); readVisible();
  }));
  window.addEventListener("resize", position);
  window.addEventListener("scroll", position, true);
  window.addEventListener("hashchange", () => { if (panel.matches(":popover-open")) panel.hidePopover(); });
  window.addEventListener("storage", event => {
    if (event.key !== storageKey) return;
    const previous = items.map(item => item.id);
    if (event.newValue === null) { items = []; seen = []; ignoredBefore = 0; }
    else load(event.newValue);
    showToast.dismissIds(new Set(previous.filter(id => !items.some(item => item.id === id && !item.read))));
    render();
  });
  render();
  return {
    publish({ id = `local:${Date.now()}:${Math.random().toString(36).slice(2)}`, title = "", message = "", kind = "info", operation = false, createdAt = Date.now(), popup = true, duration = 5200 } = {}) {
      const previous = items.find(item => item.id === id);
      if (previous && previous.kind !== "pending") return previous;
      if (!previous && operation && (seen.some(receipt => receipt.id === id) || (createdAt > 0 && createdAt <= ignoredBefore))) return null;
      const item = { id, title: String(title), message: String(message), kind, operation, createdAt, read: false };
      if (previous) items.splice(items.indexOf(previous), 1);
      items.unshift(item); items = items.slice(0, 100);
      if (operation && kind !== "pending") seen.push({ id, time: createdAt });
      save(); render();
      if (popup && kind !== "pending") showToast(item, duration);
      return item;
    },
    markRead,
  };
})();

function toast(text, kind = "info", ms = 5200) {
  return Notifications.publish({ message: text, kind, duration: ms });
}

const showToast = (() => {
  const root = $("toasts");
  const entries = [];
  let focused = document.hasFocus(), hovered = false, keyboard = false, expandedByTouch = false;
  let returnFocus = null;

  function pause(entry) {
    if (entry.startedAt === null) return;
    entry.remaining = Math.max(0, entry.remaining - (performance.now() - entry.startedAt));
    clearTimeout(entry.timer);
    entry.startedAt = null;
  }

  function dismiss(entry, read = false) {
    const index = entries.indexOf(entry);
    if (index < 0) return;
    const hadFocus = entry.box.contains(document.activeElement);
    pause(entry);
    entries.splice(index, 1);
    entry.box.remove();
    if (read) Notifications.markRead([entry.id]);
    if (!entries.length) expandedByTouch = false;
    sync();
    if (hadFocus) {
      const next = entries[Math.min(index, entries.length - 1)];
      if (next) next.close.focus({ preventScroll: true });
      else if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
    }
  }

  function sync() {
    keyboard = root.contains(document.activeElement);
    const expanded = hovered || keyboard || expandedByTouch;
    root.dataset.expanded = String(expanded);
    const front = entries.at(-1);
    if (front) root.style.setProperty("--toast-height", `${front.box.offsetHeight}px`);
    entries.forEach((entry, index) => {
      const depth = entries.length - index - 1;
      entry.box.style.setProperty("--toast-depth", Math.min(depth, 2));
      entry.box.style.zIndex = index + 1;
      entry.box.dataset.front = String(entry === front);
      entry.box.dataset.hidden = String(!expanded && depth > 2);
      entry.box.inert = !expanded && entry !== front;
      // Covered messages wait their turn; interacting with the stack pauses reading time.
      const running = focused && !document.hidden && !expanded && entry === front;
      if (!running) pause(entry);
      else if (entry.startedAt === null) {
        entry.startedAt = performance.now();
        entry.timer = setTimeout(() => {
          if (document.hidden || !document.hasFocus()) {
            focused = false;
            sync();
          } else dismiss(entry);
        }, entry.remaining);
      }
    });
  }

  window.addEventListener("blur", () => { focused = false; sync(); });
  window.addEventListener("focus", () => { focused = document.hasFocus(); sync(); });
  document.addEventListener("visibilitychange", () => { focused = document.hasFocus(); sync(); });
  window.addEventListener("pagehide", () => { focused = false; sync(); });
  window.addEventListener("pageshow", () => { focused = document.hasFocus(); sync(); });
  root.addEventListener("pointerenter", event => {
    if (event.pointerType === "mouse") { hovered = true; sync(); }
  });
  root.addEventListener("pointerleave", event => {
    if (event.pointerType === "mouse") { hovered = false; sync(); }
  });
  root.addEventListener("focusin", event => {
    if (event.relatedTarget && !root.contains(event.relatedTarget)) returnFocus = event.relatedTarget;
    sync();
  });
  root.addEventListener("focusout", () => queueMicrotask(sync));
  document.addEventListener("pointerdown", event => {
    if (!root.contains(event.target)) {
      expandedByTouch = false;
      sync();
    } else if (event.pointerType === "touch" && !event.target.closest("button")) {
      expandedByTouch = !expandedByTouch;
      sync();
    }
  });
  const resize = new ResizeObserver(() => {
    const front = entries.at(-1);
    if (front) root.style.setProperty("--toast-height", `${front.box.offsetHeight}px`);
  });
  resize.observe(root);

  const show = (notification, ms = 5200) => {
    const { id, title, message: text, kind, operation } = notification;
    const box = document.createElement("div");
    box.className = "toast";
    box.dataset.kind = kind;
    const icon = document.createElement("span");
    icon.className = "toast-icon";
    icon.innerHTML = notificationIcon(kind);
    const message = document.createElement("div");
    message.className = "toast-message";
    if (title) {
      const heading = document.createElement("strong");
      heading.className = "toast-title";
      heading.textContent = title;
      message.append(heading);
    }
    const description = document.createElement("span");
    description.className = "toast-description";
    description.textContent = text;
    message.append(description);
    if (operation) {
      const action = document.createElement("a");
      action.className = "toast-action";
      action.href = "#/console";
      action.textContent = I18n.t("Посмотреть логи");
      action.addEventListener("click", () => dismiss(entry, true));
      message.append(action);
    }
    const close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("aria-label", I18n.t("Закрыть"));
    close.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>';
    box.append(icon, message, close);
    const entry = { id, box, close, remaining: ms, startedAt: null, timer: null };
    close.addEventListener("click", () => dismiss(entry, true));
    entries.push(entry);
    root.appendChild(box);
    sync();
  };
  show.dismissIds = ids => entries.slice().forEach(entry => { if (ids.has(entry.id)) dismiss(entry); });
  show.clear = () => entries.slice().forEach(entry => dismiss(entry));
  return show;
})();

/* ───────────────────────── модальное окно ───────────────────────── */

const modal = (() => {
  const root = $("modalRoot");
  let onOk = null;
  let returnFocus = null;
  let submitting = false;

  function open({ title, bodyHTML, okLabel = I18n.t("Подтвердить"), danger = false, onConfirm }) {
    if (submitting) return;
    returnFocus = document.activeElement;
    $("modalTitle").textContent = title;
    $("modalTitle").classList.toggle("danger", danger);
    $("modalBody").innerHTML = bodyHTML;
    $("modalError").hidden = true;
    const okBtn = $("modalOk");
    okBtn.disabled = false;
    okBtn.textContent = okLabel;
    okBtn.className = "btn " + (danger ? "solid-danger" : "primary");
    onOk = onConfirm || null;
    root.hidden = false;
    (danger ? $("modalCancel") : okBtn).focus();
  }

  function close() {
    if (submitting) return;
    root.hidden = true;
    $("modalBody").innerHTML = "";
    onOk = null;
    if (returnFocus?.isConnected && !returnFocus.disabled) returnFocus.focus({ preventScroll: true });
    else if (document.activeElement === document.body || root.contains(document.activeElement)) document.querySelector(`#view-${activeView}`)?.focus({ preventScroll: true });
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
    if (submitting) return;
    if (!onOk) return close();
    const okBtn = $("modalOk");
    okBtn.disabled = true;
    submitting = true;
    $("modalError").hidden = true;
    $("modalCancel").disabled = true;
    try {
      const accepted = await onOk();
      submitting = false;
      if (accepted !== false) close();
    } catch (e) {
      error(e);
      toast(e.message || String(e), "error");
    } finally {
      submitting = false;
      okBtn.disabled = !!$("restoreAck") && !$("restoreAck").checked;
      $("modalCancel").disabled = false;
    }
  });

  function error(e) {
    $("modalError").textContent = I18n.msg`${e.message || e} Параметры сохранены — повторите действие.`;
    $("modalError").hidden = false;
  }
  return { open, close, error };
})();

/* ───────────────────────── API / демо-режим ───────────────────────── */

const S = {
  // Establish intent before editor.js can issue its initial profile request.
  demo: location.protocol === "file:" || new URLSearchParams(location.search).get("demo") === "1",
  overview: null,
  stats: null,
  players: null,
  op: null,
  logsAuto: true,
  lastOpActive: false,
  opsLoaded: false,
  localResultId: null,
  actionPending: false,
  settingsVersion: null,
  serverSettings: null,
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

const demoScenarioAt = Date.now();
function demoArchive(daysAgo) {
  const mtime = new Date(Date.parse(demoNextBackup()) - (daysAgo + 1) * 86400e3).toISOString();
  return { name: `pz-backup-${mtime.slice(0, 10).replaceAll("-", "")}-${mtime.slice(11, 19).replaceAll(":", "")}.tar.gz`, mtime, size: 684000000 - daysAgo * 12000000 };
}
function demoNextBackup() {
  const next = new Date(demoScenarioAt); next.setUTCHours(3, 0, 0, 0);
  if (next.getTime() <= demoScenarioAt) next.setUTCDate(next.getUTCDate() + 1);
  return next.toISOString();
}

const DEMO = {
  overview: () => ({
    ok: true, serverName: I18n.t("Кастом-Нокс (демо)"), container: "pzserver",
    image: "indifferentbrokkoli/pzserver:latest", docker: true, compose: true,
    rconConfigured: true, rcon: { state: "ok", error: null, at: demoNow() },
    containerInfo: { status: "running", running: true, startedAt: new Date(Date.now() - 569000 * 1000).toISOString(), image: "indifferentbrokkoli/pzserver:latest", uptimeSec: 569000 },
    update: { at: new Date(Date.now() - 2 * 3600 * 1000).toISOString(), local: "sha256:9f21a4c0e7b2d8f1a3c5e7b9d1f3a5c7e9b1d3f5a7c9e1b3d5f7a9c1e3b5d7f9", remote: "sha256:9f21a4c0e7b2d8f1a3c5e7b9d1f3a5c7e9b1d3f5a7c9e1b3d5f7a9c1e3b5d7f9", available: false, error: null },
    modsCheck: { at: new Date(Date.now() - 30 * 60 * 1000).toISOString(), state: "up-to-date", items: [], error: null, source: "auto" },
    settings: { autoUpdate: { enabled: true, intervalHours: 6, warnSeconds: 300, backupBeforeUpdate: true }, modsUpdate: { enabled: true, intervalHours: 6, restartOnUpdate: true, warnSeconds: 600 }, backup: { stopServer: false, maxBackups: 10 }, watchdog: { enabled: true, thresholdMin: 5, autoRestart: false }, playerNotifications: { language: "en" }, telegram: { enabled: true, language: "en", botTokenMasked: "•••A1b2", chatId: "-1001234567890", groups: { ops: true, backup: true, update: true, problems: true } }, nextCheck: Date.now() / 1000 + 3600 * 4, nextModsCheck: Date.now() / 1000 + 3600 * 2 },
    watchdog: { lastProbeAt: demoNow(), lastResult: "ok", lastError: null, consecutiveFailures: 0, alerted: false },
    notify: { at: demoNow(), ok: true, error: null },
    backupsCount: 2, now: demoNow(),
  }),
  players: () => ({ ok: true, names: [I18n.t("Дмитрий"), "Sledge", "Katya_V"], raw: I18n.t("Дмитрий\nSledge\nKatya_V"), count: 3 }),
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
    text: `${demoNow()} PZ SERVER: v42.21.0 MP dedicated\n${demoNow()} [save] World saved (demo line)\n${demoNow()} INFO: 3 players online\n`,
  }),
  backups: () => ({
    ok: true, maxBackups: 7,
    autoBackup: { enabled: true, time: "03:00", stopServer: false, nextRun: demoNextBackup(), timeZone: "UTC" },
    items: [demoArchive(0), demoArchive(1)],
    journal: [...[0, 1].map(day => ({ ...demoArchive(day), ts: demoArchive(day).mtime, trigger: "scheduled", type: "full", status: "success", duration: 42.5 })),
      { ts: demoArchive(2).mtime, trigger: "scheduled", type: "full", name: "", size: 0, status: "error", error: I18n.t("Каталог данных PZ пуст или не смонтирован"), duration: 0.4 }],
  }),
  events: () => ({
    ok: true,
    items: [
      { ts: demoNow(), type: "update-check", text: I18n.t("Плановая проверка обновлений: обновлений нет") },
      { ts: new Date(Date.now() - 7200e3).toISOString(), type: "restart", text: I18n.t("Сервер перезапущен") },
      { ts: demoArchive(0).mtime, type: "backup", text: I18n.msg`Бэкап создан: ${demoArchive(0).name} (${fmtBytes(demoArchive(0).size)})` },
    ].sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts)),
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
    if (!response.ok && response.status !== 401) throw new Error(I18n.t("Не удалось выйти"));
    requireLogin();
  } catch (error) {
    toast(error.message || I18n.t("Не удалось выйти"), "error");
    button.disabled = false;
  }
});

let pendingMutations = 0;
function operationBusy() {
  return !!S.op?.active || S.actionPending;
}

function operationAvailabilityReason() {
  return S.demo ? I18n.t("Демо: операции отключены. Откройте пульт своего сервера для управления.")
    : S.op?.active ? I18n.t("Идёт операция: ") + (OP_TITLES[S.op.active.op] || S.op.active.op) + I18n.t(". Дождитесь результата; прогресс показан над разделом.")
    : S.actionPending ? I18n.t("Запрос отправляется. Дождитесь принятия или сообщения об ошибке.")
    : "";
}

async function api(path, opts = {}) {
  // Recheck when sending: a dialog or queued edit may predate an SSE lock.
  const changing = opts.method && opts.method !== "GET";
  const inspection = path === "/api/config-validate" && !opts.body?.prepare
    || path === "/api/modpack" && !opts.body?.pack;
  if (changing && operationBusy() && path !== "/api/action" && !path.startsWith("/api/auth/") && !inspection) {
    throw new Error(operationAvailabilityReason());
  }
  if (S.demo) {
    if (opts.method && opts.method !== "GET") {
      throw new Error(I18n.t("Демо-режим: операции недоступны"));
    }
    await new Promise((r) => setTimeout(r, 120));
    if (path.startsWith("/api/overview")) return DEMO.overview();
    if (path.startsWith("/api/players/history")) return DEMO.history();
    if (path.startsWith("/api/stats/history")) return DEMO.statsHistory();
    if (path.startsWith("/api/mods")) return DEMO.mods();
    if (path.startsWith("/api/players")) return DEMO.players();
    if (path.startsWith("/api/stats")) return DEMO.stats();
    if (path.startsWith("/api/logs")) return DEMO.logs();
    if (path.startsWith("/api/backups/journal")) {
      const params = new URL(path, location.origin).searchParams;
      const offset = Number(params.get("offset") || 0), limit = Number(params.get("limit") || 25);
      const journal = DEMO.backups().journal;
      return { ok: true, items: journal.slice(offset, offset + limit), hasMore: journal.length > offset + limit };
    }
    if (path.startsWith("/api/backups")) return DEMO.backups();
    if (path.startsWith("/api/events")) return DEMO.events();
    if (path.startsWith("/api/ops")) return { ok: true, active: null, history: [] };
    throw new Error(I18n.t("Демо-режим: нет данных"));
  }
  const mutation = !!opts.method && opts.method !== "GET";
  if (mutation && !navigator.onLine) throw new Error(I18n.t("Нет сети. Восстановите связь и повторите действие."));
  if (mutation) pendingMutations++;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), opts.timeout || 9000);
  try {
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers: { "X-PZ-Request": "1", "X-PZ-Language": I18n.language, ...(opts.body ? { "Content-Type": "application/json" } : {}) },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      signal: opts.signal ? AbortSignal.any([ctrl.signal, opts.signal]) : ctrl.signal,
    });
    if (res.status === 401) {
      requireLogin();
      throw new Error(I18n.t("Сессия завершена. Войдите снова"));
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok && !data.error) data.error = `HTTP ${res.status}`;
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error(I18n.t("Сервер не ответил вовремя. Проверьте состояние операции перед повтором."));
    if (error instanceof TypeError) throw new Error(I18n.t("Нет соединения с пультом. Восстановите связь и повторите запрос."));
    throw error;
  } finally {
    clearTimeout(timer);
    if (mutation) pendingMutations--;
  }
}

async function requestOperation(body, { timeout } = {}) {
  if (operationBusy()) throw new Error(operationAvailabilityReason());
  S.actionPending = true;
  updateButtons();
  try {
    const res = await api("/api/action", { method: "POST", body, timeout });
    if (res.error || res.ok === false) throw new Error(res.error || I18n.t("Запрос не принят"));
    await refreshOps();
    return res;
  } finally {
    S.actionPending = false;
    updateButtons();
  }
}

async function action(op, extra = {}) {
  try {
    await requestOperation({ op, ...extra });
    toast(I18n.msg`Операция «${OP_TITLES[op] || op}» запущена`, "ok");
    return true;
  } catch (e) {
    if (!$("modalRoot").hidden) modal.error(e);
    else showActionError(op, e);
    if (!$("modalRoot").hidden) toast(e.message || String(e), "error");
    return false;
  }
}

function showActionError(op, error) {
  showLocalResult("error", I18n.msg`${OP_TITLES[op] || op} — запрос не принят`, I18n.msg`${error.message || error} Проверьте соединение и события, затем повторите действие.`);
}

function showLocalResult(state, title, message) {
  const id = S.localResultId || `request:${Date.now()}:${Math.random().toString(36).slice(2)}`;
  S.localResultId = state === "pending" ? id : null;
  Notifications.publish({ id, kind: state, title, message, operation: true, duration: 8000 });
}

/* ───────────────────────── отрисовка: обзор ───────────────────────── */

function setPill(id, state, text) {
  const p = $(id);
  if (!p) return;
  p.dataset.state = state;
  if (text) p.textContent = text;
}

function renderOverview(o) {
  if (o.settings) o = { ...o, settings: acceptSettings(o.settings) };
  S.overview = o;
  const rc = o.rcon || {};

  const remote = o.mode === "remote";
  const c = o.containerInfo;
  const lamp = $("stateLamp"), label = $("stateLabel");
  if (remote) {
    const rs = rc.state || "unknown";
    $("absentHint").hidden = false;
    $("absentHint").textContent =
      I18n.t("Пульт запущен вне хоста сервера: активны RCON-консоль, игроки, объявления и сохранение мира. ") +
      I18n.t("Управление контейнером, бэкапы и обновление заработают при запуске пульта на сервере (README, вариант Б).");
    if (rs === "ok") {
      lamp.dataset.state = "ok";
      label.textContent = I18n.t("Работает (RCON)");
      $("uptime").textContent = I18n.t("удалённое управление по RCON");
    } else if (rs === "error") {
      lamp.dataset.state = "bad";
      label.textContent = I18n.t("Нет ответа RCON");
      $("uptime").textContent = I18n.t("проверьте сервер, порт и пароль");
    } else {
      lamp.dataset.state = "warn";
      label.textContent = I18n.t("Проверка…");
      $("uptime").textContent = "…";
    }
  } else {
    $("absentHint").hidden = !!c;
    if (!c) {
      lamp.dataset.state = "bad";
      label.textContent = I18n.t("Контейнер не найден");
      $("uptime").textContent = "—";
    } else if (c.running) {
      lamp.dataset.state = "ok";
      label.textContent = I18n.t("Работает");
      $("uptime").textContent = I18n.msg`в работе ${fmtUptime(c.uptimeSec)}`;
    } else if (c.status === "restarting") {
      lamp.dataset.state = "warn";
      label.textContent = I18n.t("Перезапускается");
      $("uptime").textContent = "—";
    } else {
      lamp.dataset.state = "bad";
      label.textContent = I18n.t("Остановлен");
      $("uptime").textContent = "—";
    }
  }

  // блок обновлений
  const u = o.update || {};
  const pill = $("updPill");
  if (remote) {
    pill.dataset.state = "unknown";
    pill.textContent = I18n.t("только на хосте сервера");
    $("updNote").textContent = I18n.t("Сравнение digest требует доступа к локальному образу — обновление выполняется с хоста сервера.");
  } else if (u.available === true) {
    pill.dataset.state = "warn"; pill.textContent = I18n.t("есть обновление");
    $("updNote").textContent = I18n.t("Сверяется digest локального образа с Docker Hub.");
  } else if (u.available === false) {
    pill.dataset.state = "ok"; pill.textContent = I18n.t("актуально");
    $("updNote").textContent = I18n.t("Сверяется digest локального образа с Docker Hub.");
  } else if (u.error) {
    pill.dataset.state = "bad"; pill.textContent = I18n.t("ошибка проверки");
    $("updNote").textContent = u.error;
  } else if (u.note) {
    pill.dataset.state = "unknown"; pill.textContent = "—";
    $("updNote").textContent = u.note;
  } else {
    pill.dataset.state = "unknown"; pill.textContent = I18n.t("не проверялось");
    $("updNote").textContent = I18n.t("Сверяется digest локального образа с Docker Hub.");
  }
  $("updNote").hidden = !(remote || u.error || (u.note && u.available == null));
  const same = u.local && u.remote && u.local === u.remote;
  $("updRemote").classList.toggle("ok-same", !!same);
  $("updHubDate").textContent = u.hubUpdated ? I18n.t("собрана ") + fmtTime(u.hubUpdated) : "—";
  setCopyTarget("updLocalCopy", u.local, shortDigest(u.local));
  setCopyTarget("updRemoteCopy", u.remote, same ? I18n.t("совпадает") : shortDigest(u.remote));

  const du = o.dashboardUpdate || {};
  setPill("dashboardUpdPill", du.error ? "bad" : du.available === true ? "warn" : du.available === false ? "ok" : "unknown",
    du.error ? I18n.t("ошибка проверки") : du.available === true ? I18n.t("есть обновление") : du.available === false ? I18n.t("актуально") : I18n.t("не проверялось"));
  $("dashboardUpdImage").textContent = du.image || "—";
  $("dashboardUpdNote").textContent = du.error || du.note || "";
  $("dashboardUpdNote").hidden = !$("dashboardUpdNote").textContent;
  setCopyTarget("dashboardUpdLocalCopy", du.local, shortDigest(du.local));
  setCopyTarget("dashboardUpdRemoteCopy", du.remote, du.local && du.local === du.remote ? I18n.t("совпадает") : shortDigest(du.remote));

  // автообновление
  const au = o.settings?.autoUpdate || {};
  if (settingsCanRender("autoSwitch")) $("autoSwitch").checked = !!au.enabled;
  if (settingsCanRender("autoInterval")) $("autoInterval").value = String(au.intervalHours ?? 6);
  if (settingsCanRender("autoWarn")) $("autoWarn").value = String(au.warnSeconds ?? 300);
  if (settingsCanRender("buBackup")) $("buBackup").checked = au.backupBeforeUpdate !== false;
  const wdCfg = o.settings?.watchdog || {};
  if (settingsCanRender("wdSwitch")) $("wdSwitch").checked = !!wdCfg.enabled;
  if (settingsCanRender("wdThreshold")) $("wdThreshold").value = String(wdCfg.thresholdMin ?? 5);
  if (settingsCanRender("wdGracePeriod")) $("wdGracePeriod").value = String(wdCfg.gracePeriodMin ?? 5);
  if (settingsCanRender("wdRestart")) $("wdRestart").checked = !!wdCfg.autoRestart;
  const wds = o.watchdog || {};
  if (wdCfg.enabled) {
    const fails = wds.consecutiveFailures || 0;
    const skipped = wds.lastResult === "skipped" && !fails;
    const grace = wds.graceRemainingSec > 0;
    setPill("wdPill", grace || skipped ? "unknown" : fails ? "bad" : "ok",
      grace ? I18n.msg`пауза: ${Math.ceil(wds.graceRemainingSec / 60)} мин` : fails ? I18n.msg`сбои: ${fails}` : skipped ? I18n.t("ожидание") : I18n.t("следит"));
  } else {
    setPill("wdPill", "unknown", I18n.t("выкл"));
  }

  // уведомления Telegram
  const tg = o.settings?.telegram || {};
  const tgEnabled = !!tg.enabled;
  if (settingsCanRender("tgSwitch")) $("tgSwitch").checked = tgEnabled;
  if (settingsCanRender("tgChat")) $("tgChat").value = tg.chatId || "";
  if (settingsCanRender("playerLanguage")) $("playerLanguage").value = o.settings?.playerNotifications?.language === "ru" ? "ru" : "en";
  if (settingsCanRender("tgLanguage")) $("tgLanguage").value = tg.language === "ru" ? "ru" : "en";
  const groups = tg.groups || {};
  if (settingsCanRender("tgOps")) $("tgOps").checked = groups.ops !== false;
  if (settingsCanRender("tgBackup")) $("tgBackup").checked = groups.backup !== false;
  if (settingsCanRender("tgUpdate")) $("tgUpdate").checked = groups.update !== false;
  if (settingsCanRender("tgProblems")) $("tgProblems").checked = groups.problems !== false;
  if (tgEnabled) {
    const ns = o.notify || {};
    if (ns.ok === false && ns.error) {
      setPill("tgPill", "bad", I18n.t("ошибка отправки"));
    } else if (ns.ok) {
      setPill("tgPill", "ok", I18n.t("вкл"));
    } else {
      setPill("tgPill", "ok", I18n.t("вкл"));
    }
  } else {
    setPill("tgPill", "unknown", I18n.t("выкл"));
  }

  // проверка модов (RCON checkModsNeedUpdate)
  const mc = o.modsCheck || {};
  if (mc.state === "up-to-date") {
    setPill("modsPill", "ok", I18n.t("актуальны"));
    $("modsCheckNote").textContent = "";
  } else if (mc.state === "needs-update") {
    const n = (mc.items || []).length;
    setPill("modsPill", "warn", n ? I18n.msg`обновить: ${n}` : I18n.t("есть обновления"));
    $("modsCheckNote").textContent = I18n.t("Для загрузки обновлений нужен рестарт.");
  } else if (mc.state === "inconclusive") {
    setPill("modsPill", "warn", I18n.t("нет ответа"));
    $("modsCheckNote").textContent = I18n.t("Сервер не вернул результат вовремя. Повторите проверку позже.");
  } else {
    setPill("modsPill", "unknown", I18n.t("не проверялись"));
    $("modsCheckNote").textContent = "";
  }
  $("modsCheckNote").hidden = !$("modsCheckNote").textContent;
  const items = mc.items || [];
  const needList = $("modsNeedList");
  needList.hidden = !(mc.state === "needs-update" && items.length);
  setStaticMarkup(needList, items.map((it) => `
    <div class="mod-need-row">
      <span class="m-title">${it.url
        ? `<a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.title || it.workshopId)}</a>`
        : esc(it.raw || I18n.t("мод требует обновления"))}</span>
      ${it.workshopId ? copyValue(it.workshopId, { className: "wid mono" }) : ""}
    </div>`).join(""));
  $("btnApplyMods").hidden = mc.state !== "needs-update";
  const mu = o.settings?.modsUpdate || {};
  if (settingsCanRender("modsAutoSwitch")) $("modsAutoSwitch").checked = !!mu.enabled;
  if (settingsCanRender("modsAutoInterval")) $("modsAutoInterval").value = String(mu.intervalHours ?? 6);
  if (settingsCanRender("modsAutoAction")) $("modsAutoAction").value = mu.restartOnUpdate === false ? "notify" : "restart";
  if (settingsCanRender("modsAutoWarn")) $("modsAutoWarn").value = String(mu.warnSeconds ?? 600);

  renderSummaries();
  updateButtons();
}

/* Строка обзора: онлайн и последний бэкап; CPU и RAM показаны рядом с графиками. */
function renderKpis() {
  const o = S.overview;
  const remote = !!(o && o.mode === "remote");

  const p = S.players;
  const online = p && p.ok ? p.count : null;
  $("kpiOnline").textContent = online == null ? "–" : String(online);
  if (remote) $("kpiOnlineSub").textContent = "RCON";
  else if (S.phPoints && S.phPoints.length) {
    const peak = S.phPoints.reduce((m, x) => Math.max(m, x.count || 0), 0);
    $("kpiOnlineSub").textContent = I18n.msg`пик за сутки: ${peak}`;
  } else $("kpiOnlineSub").textContent = "…";

  const last = S.backupsItems && S.backupsItems[0];
  $("kpiBackup").title = "";
  if (remote) {
    $("kpiBackup").textContent = "—";
    $("kpiBackupSub").textContent = I18n.t("на хосте");
  } else if (!last) {
    $("kpiBackup").textContent = I18n.t("Нет копий");
    $("kpiBackupSub").textContent = I18n.t("архивов ещё нет");
  } else {
    const timestamp = Date.parse(last.mtime), days = Math.floor((Date.now() - timestamp) / 86400000);
    $("kpiBackup").textContent = !Number.isFinite(timestamp) ? I18n.t("Дата неизвестна") : timestamp > Date.now() ? I18n.t("Дата в будущем") : days >= 2 ? relativeDayFmt.format(-days, "day") : relTime(last.mtime);
    $("kpiBackup").title = fmtTime(last.mtime);
    $("kpiBackupSub").textContent = last.size != null ? fmtBytes(last.size) : last.sizeText || "—";
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
}

function setAvailability(id, message) {
  const element = $(id);
  if (element.textContent !== message) element.textContent = message;
  if (element.hidden !== !message) element.hidden = !message;
}

function updateButtons() {
  const o = S.overview;
  const busy = operationBusy() || S.demo || document.body.classList.contains("is-booting");
  const remote = !!(o && o.mode === "remote");
  const rconOk = !!(o && o.rcon && o.rcon.state === "ok");
  const running = !remote && !!(o && o.containerInfo && o.containerInfo.running);
  const found = !!(o && o.containerInfo);
  const hostHint = I18n.t("Доступно только при запуске пульта на хосте сервера");
  const consoleLive = busy ? false : (remote ? rconOk : running);
  $("btnStart").disabled = busy || remote || !found || running;
  $("btnStop").disabled = busy || S.demo || remote || !running;
  $("btnRestart").disabled = busy || S.demo || remote || !running;
  $("btnSaveWorld").disabled = !consoleLive;
  $("btnCheckUpd").disabled = busy || S.demo || remote;
  $("btnApplyUpd").disabled = busy || remote || o?.compose === false || o?.update?.available === false;
  $("btnCheckDashboardUpd").disabled = busy || remote || !o?.dashboardUpdate?.supported;
  $("btnApplyDashboardUpd").disabled = busy || remote || !o?.dashboardUpdate?.supported || o?.compose === false || o?.dashboardUpdate?.available === false;
  $("btnCheckMods").disabled = busy || S.demo || remote;
  $("btnApplyMods").disabled = busy || S.demo || remote;
  const modsRestart = S.op?.active?.op === "mods-restart";
  const cancelPending = modsRestart && !!S.op.active.cancelRequested;
  $("btnCancelMods").hidden = !modsRestart || (!S.op.active.cancellable && !cancelPending);
  $("btnCancelMods").disabled = remote || cancelPending || !S.op?.active?.cancellable;
  $("btnCancelMods").textContent = cancelPending ? I18n.t("Отмена…") : I18n.t("Отменить обновление модов");
  // автонастройки и watchdog пишут в настройки и работают только с хоста —
  // в remote-режиме они тихо ничего не делают, честно их глушим
  const hostOnly = busy || S.demo || remote;
  for (const [group, definition] of Object.entries(SETTING_GROUPS)) {
    const disabled = ["telegram", "playerNotifications"].includes(group) ? busy : hostOnly;
    for (const id of definition.ids) setDomProperty($(id), "disabled", disabled);
  }
  setDomProperty($("btnTgChats"), "disabled", busy || !!S.telegramChatsPending);
  setDomProperty($("btnTgTest"), "disabled", busy || !!S.telegramTestPending);
  document.querySelectorAll(".settings-feedback button, #tgChatsBody .chat-chip").forEach(button => {
    setDomProperty(button, "disabled", busy || remote && !button.closest("#sec-notify, #playerNotifications"));
  });
  for (const id of ["btnStart", "btnStop", "btnRestart", "btnCheckUpd", "btnApplyUpd", "btnBackup", "btnCheckMods", "btnApplyMods",
                    "autoSwitch", "autoInterval", "autoWarn", "buBackup", "wdSwitch", "wdThreshold", "wdGracePeriod", "wdRestart",
                    "modsAutoSwitch", "modsAutoInterval", "modsAutoAction", "modsAutoWarn",
                    "bkAutoSwitch", "bkAutoTime", "bkAutoKeep", "bkAutoStop"]) {
    $(id).title = remote ? hostHint : (id === "btnApplyUpd" && o && o.compose === false
      ? I18n.t("Недоступен плагин docker compose в контейнере пульта") : "");
  }
  $("btnBackup").disabled = busy || S.demo || remote;
  $("logsDownload").style.display = (remote || S.demo) ? "none" : "";
  $("logsFilter").disabled = remote || S.demo;
  document.querySelectorAll("#playersBody .p-actions .icon-btn").forEach((b) => { if (b.disabled !== !consoleLive) b.disabled = !consoleLive; });
  document.querySelectorAll("#backupsBody .icon-btn").forEach((b) => { const disabled = busy || S.demo || remote; if (b.disabled !== disabled) b.disabled = disabled; });
  document.querySelectorAll("#quickCmds .chip").forEach((b) => { b.disabled = !consoleLive; });
  $("consoleInput").disabled = !consoleLive;
  $("consoleForm").querySelector("button").disabled = !consoleLive;
  const commonReason = operationAvailabilityReason();
  const hostReason = commonReason || (remote ? I18n.t("Remote: управление контейнером, обновления и архивы доступны в пульте на хосте сервера. Здесь доступны RCON и игроки.") : "");
  setAvailability("operationAvailability", hostReason || (!found ? I18n.t("Контейнер не найден или его состояние ещё не получено. Проверьте подключение и имя контейнера.")
    : !running ? I18n.t("Сервер остановлен. Для остановки и перезапуска сначала запустите его.") : ""));
  setAvailability("maintenanceAvailability", hostReason || (o?.compose === false ? I18n.t("Обновление образа недоступно: установите docker compose в контейнере пульта и проверьте подключение.") : ""));
  setAvailability("backupAvailability", hostReason);
  setAvailability("playersAvailability", commonReason || (!consoleLive ? I18n.t("RCON недоступен. Проверьте запуск сервера, пароль и порт RCON; последние ответы сохранены.") : ""));
  setAvailability("modsAvailability", commonReason);
  setAvailability("settingsAvailability", commonReason);
  setAvailability("consoleAvailability", commonReason || (!consoleLive ? I18n.t("RCON недоступен. Проверьте запуск сервера, пароль и порт RCON; последние ответы сохранены.") : ""));
  if (consoleBootLine?.isConnected) {
    const consoleStatus = commonReason || (!o ? I18n.t("Пульт подключается к серверу…")
      : !consoleLive ? I18n.t("Ожидаем доступность RCON. Последние ответы сохранены.")
      : I18n.t("Консоль доступна — команды отправляются на сервер по RCON."));
    setDomProperty(consoleBootLine, "textContent", consoleStatus);
  }
  for (const id of ["btnStart", "btnStop", "btnRestart", "btnSaveWorld"]) $(id).setAttribute("aria-describedby", "operationAvailability");
  for (const id of ["btnCheckUpd", "btnApplyUpd"]) $(id).setAttribute("aria-describedby", "maintenanceAvailability");
  for (const id of ["btnCheckDashboardUpd", "btnApplyDashboardUpd"]) $(id).setAttribute("aria-describedby", "maintenanceAvailability dashboardUpdNote");
  $("btnBackup").setAttribute("aria-describedby", "backupAvailability");
  $("consoleInput").setAttribute("aria-describedby", "consoleAvailability");
  window.ConfigEditor?.operationChanged();
  // The inert startup shell needs no temporary availability descriptions.
  if (document.body.classList.contains("is-booting")) return;
  for (const [view, description] of Object.entries({
    players: "playersAvailability", mods: "modsAvailability", settings: "settingsAvailability",
    maintenance: "maintenanceAvailability", backups: "backupAvailability", console: "consoleAvailability",
  })) {
    document.querySelectorAll(`#view-${view} button:disabled, #view-${view} input:disabled, #view-${view} select:disabled, #view-${view} textarea[readonly]`).forEach(control => {
      const ids = new Set((control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
      ids.add(description);
      setDomAttribute(control, "aria-describedby", [...ids].join(" "));
    });
  }
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
  for (const h of [...(op?.history || [])].reverse()) {
    const finished = Date.parse(h.finishedAt);
    if (!Number.isFinite(finished)) continue;
    Notifications.publish({
      id: `operation:${h.id || JSON.stringify([h.op, h.finishedAt, h.ok, !!h.cancelled, h.message])}`,
      kind: h.cancelled ? "warning" : h.ok ? "ok" : "error",
      title: `${OP_TITLES[h.op] || h.op} — ${h.cancelled ? I18n.t("отменено") : h.ok ? I18n.t("готово") : I18n.t("не удалось")}`,
      message: h.message || (h.ok ? I18n.t("Операция завершена. Подробности в событиях.") : I18n.t("Откройте логи, устраните причину и повторите действие.")),
      createdAt: finished, operation: true, popup: S.opsLoaded, duration: 8000,
    });
  }
  if (!active && S.lastOpActive) refreshAll();
  S.opsLoaded = true;
  S.lastOpActive = !!(active);
  S.op = op;
  if (activeView === "backups" && S.backupsItems?.length) renderBackupsPage();
  updateOperationElapsed();
  updateButtons();
}

function updateOperationElapsed() {
  const started = Date.parse(S.op?.active?.startedAt);
  $("opElapsed").textContent = Number.isFinite(started) ? I18n.msg`Прошло ${fmtUptime(Math.floor(Math.max(0, (Date.now() - started) / 1000)))}` : "";
}

/* человеческие названия операций для полосы прогресса и тостов */
const OP_TITLES = {
  start: I18n.t("Запуск"), stop: I18n.t("Остановка"), restart: I18n.t("Рестарт"),
  "check-update": I18n.t("Проверка обновлений"), "apply-update": I18n.t("Обновление сервера"),
  "check-dashboard-update": I18n.t("Проверка обновлений пульта"), "apply-dashboard-update": I18n.t("Обновление пульта"),
  "check-mods-update": I18n.t("Проверка модов"), "apply-mods-update": I18n.t("Обновление модов"),
  "mods-restart": I18n.t("Авторестарт модов"),
  backup: I18n.t("Бэкап"), restore: I18n.t("Восстановление"), "verify-backup": I18n.t("Проверка архива"),
  "apply-config": I18n.t("Применение конфигурации"), "prepare-workshop": I18n.t("Подготовка Workshop"),
};

/* ───────────────────────── игроки ───────────────────────── */

function setListMessage(body, state, html) {
  const focused = body.contains(document.activeElement);
  if (body.dataset.state !== state || body.innerHTML !== html) body.innerHTML = html;
  body.dataset.state = state;
  body.tabIndex = -1;
  if (focused && !body.contains(document.activeElement)) body.focus({ preventScroll: true });
}

// Keep identities and interactive children, even when data or row order changes.
function syncRows(body, items, keyOf, create, update = () => {}) {
  const focused = body.contains(document.activeElement) ? document.activeElement : null;
  const previousRows = [...body.children];
  const previousIndex = previousRows.indexOf(focused?.closest("[data-row-key]"));
  const actionKey = focused?.dataset.p || focused?.dataset.b;
  const rows = new Map(previousRows.filter((row) => row.dataset.rowKey !== undefined).map((row) => [row.dataset.rowKey, row]));
  if (body.dataset.state !== "ok") body.replaceChildren();
  if (body.dataset.state !== "ok") body.dataset.state = "ok";
  if (body.tabIndex !== -1) body.tabIndex = -1;
  const desired = items.map((item) => {
    const key = String(keyOf(item));
    let row = rows.get(key);
    if (!row) { row = create(item); row.dataset.rowKey = key; }
    rows.delete(key);
    update(row, item);
    return row;
  });
  rows.forEach((row) => row.remove());
  desired.forEach((row, index) => { if (body.children[index] !== row) body.insertBefore(row, body.children[index] || null); });
  if (focused && focused.isConnected) {
    if (document.activeElement !== focused) focused.focus({ preventScroll: true });
  } else if (focused) {
    const row = desired[Math.min(Math.max(0, previousIndex), desired.length - 1)];
    const next = row && [...row.querySelectorAll("button")].find((button) => !button.disabled && (button.dataset.p || button.dataset.b) === actionKey);
    (next || body).focus({ preventScroll: true });
  }
}

function renderPlayers(data) {
  S.players = data;
  const body = $("playersBody");
  if (!data.ok) {
    setListMessage(body, "error", `<p class="list-error">${esc(data.error || I18n.t("нет данных"))}</p>`);
    $("playersCount").textContent = "–";
    return;
  }
  $("playersCount").textContent = String(data.count);
  if (!data.names.length) {
    const raw = (data.raw || "").trim();
    setListMessage(body, "empty", I18n.msg`<p class="list-empty">Нет игроков онлайн.</p>` +
      (raw && !/players/i.test(raw) ? `<p class="list-empty mono">${esc(raw)}</p>` : ""));
    return;
  }
  syncRows(body, [...new Set(data.names)], (name) => name, (n) => {
    const template = document.createElement("template");
    template.innerHTML = I18n.msg`
    <div class="player-row">
      <span class="dot"></span>
      <span class="p-name" title="${esc(n)}">${esc(n)}</span>
      <span class="p-actions">
        <button class="icon-btn player-action" data-p="kick" data-name="${esc(n)}" title="Кикнуть" aria-label="Кикнуть ${esc(n)}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 3h6v18h-6M10 17l5-5-5-5M15 12H3"/></svg><span>Кикнуть</span>
        </button>
        <button class="icon-btn player-action danger" data-p="ban" data-name="${esc(n)}" title="Забанить" aria-label="Забанить ${esc(n)}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M5.5 5.5l13 13"/></svg><span>Забанить</span>
        </button>
      </span>
    </div>`;
    return template.content.firstElementChild;
  });
  updateButtons();
}

function confirmPlayerAction(kind, name) {
  const isKick = kind === "kick";
  const cmdBase = isKick ? "kickuser" : "banuser";
  modal.open({
    title: `${isKick ? I18n.t("Кикнуть") : I18n.t("Забанить")} «${name}»?`,
    danger: true,
    okLabel: isKick ? I18n.t("Кикнуть") : I18n.t("Забанить"),
    bodyHTML: I18n.msg`
      <p>Игрок будет ${isKick ? I18n.t("отключён от сервера") : I18n.t("заблокирован навсегда")} командой
      <span class="mono">${cmdBase}</span>.</p>
      <label class="field">Причина (необязательно)
        <input type="text" id="paReason" maxlength="120" style="height:38px;color:var(--ink);background:var(--bg-deep);border:1px solid var(--line-strong);border-radius:8px;padding:0 12px;" />
      </label>
    `,
    onConfirm: async () => {
      if (/["\r\n\x00-\x1f]/.test(name)) throw new Error(I18n.t("Пульт не может безопасно передать это имя игрока через RCON. Действие не отправлено; имя не подменяется."));
      const reason = ($("paReason")?.value || "").replace(/"/g, "'").trim();
      const cmd = `${cmdBase} "${name}"${reason ? ` "${reason}"` : ""}`;
      try {
        const res = await api("/api/rcon", { method: "POST", body: { command: cmd } });
        if (res.error) throw new Error(res.error);
        consoleAppend(`> ${cmd}`, "c-dim");
        consoleAppend(res.output || I18n.t("(без ответа)"));
        toast(`${isKick ? I18n.t("Кикнут") : I18n.t("Забанен")}: ${name}`, "ok");
        refreshPlayers();
      } catch (e) {
        toast(e.message || String(e), "error");
        modal.error(e);
        return false;
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
  $("phPeak").textContent = I18n.msg`пик: ${peak}`;
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
    tip.textContent = I18n.msg`${fmtTime(best.ts)} · ${best.count} игр.`;
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
    $("ramSub").textContent = I18n.t("метрики — только с хоста сервера");
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
  $("cpuVal").textContent = `${I18n.number(cpu, {minimumFractionDigits: 1, maximumFractionDigits: 1})}%`;
  setBar("cpuBar", cpu);
  const memPct = Math.max(0, Math.min(100, st.memPct || 0));
  $("ramVal").textContent = `${memPct.toFixed(0)}%`;
  setBar("ramBar", memPct);
  $("ramSub").textContent = I18n.msg`${fmtBytes(st.memUsed)} из ${fmtBytes(st.memLimit)}`;
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
  if (window.ConfigEditor) return; // Editors load their snapshot on entry and explicit actions.
  const body = $("modsBody");
  const sel = $("modsFile");
  if (!data.ok) {
    body.dataset.state = "error";
    setStaticMarkup(body, `<p class="list-error">${esc(data.error || I18n.t("нет данных"))}</p>`);
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
    setStaticMarkup(body, I18n.msg`<p class="list-empty"><strong>Модов нет.</strong> Параметры Mods= и WorkshopItems= в конфиге пустые.</p>`);
    return;
  }

  let html = "";
  const disabledList = (data.disabled || []);
  if (canManage) {
    const note = data.mappingSource === "container"
      ? I18n.t("Состав модов из конфига; названия модов читаются внутри контейнера.")
      : I18n.t("Выключатель убирает мод из конфига; изменения применяются рестартом.");
    html += `<p class="mods-note">${esc(note)}</p>`;
  }
  if (data.paired && (data.pairs || []).length) {
    // 1:1 — моды соответствуют Workshop-элементам по порядку
    html += I18n.msg`<p class="mods-note">Моды соответствуют Workshop-элементам по порядку.</p>`;
    html += data.pairs.map((p, i) => `
      <div class="mod-row">
        <span class="m-idx mono">${i + 1}</span>
        ${copyValue(p.mod, { className: "m-name mono" })}
        <span class="m-ws">
          ${p.url
            ? `<a href="${esc(p.url)}" target="_blank" rel="noopener" title="Steam Workshop · ${esc(p.workshopId)}">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6M20 4l-9 9M10 5H5v14h14v-5"/></svg>
                <span class="ws-title">${esc(p.title || p.workshopId)}</span>
              </a>
              ${copyValue(p.workshopId, { className: "wid mono", title: `Workshop ID: ${p.workshopId}` })}`
            : `<span class="wid mono">${esc(p.workshopId || "—")}</span>`}
        </span>
        ${canManage ? _wsSwitch(p.workshopId, true, I18n.t("Выключить мод в конфиге")) : ""}
      </div>`).join("");
  } else {
    // Общий случай: один Workshop-элемент может содержать несколько модов
    if (ws.length) {
      html += I18n.msg`<p class="mods-note">Workshop-элементы — ${ws.length}</p>`;
      if (ws.every((w) => !(w.mods || []).length)) {
        // у элементов нет модов на диске — компактная сетка строк вместо карточек
        html += `<div class="mods-grid">` + ws.map((w) => `
          <div class="mod-line">
            ${w.url
              ? `<a class="m-t" href="${esc(w.url)}" target="_blank" rel="noopener" title="${esc(w.title || w.workshopId)}">${esc(w.title || w.workshopId)}</a>`
              : `<span class="m-t">${esc(w.title || w.workshopId)}</span>`}
            ${copyValue(w.workshopId, { className: "wid mono", text: shortWsId(w.workshopId), title: `Workshop ID: ${w.workshopId}` })}
            ${canManage ? _wsSwitch(w.workshopId, true, I18n.t("Выключить мод в конфиге")) : ""}
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
            ${w.title ? copyValue(w.workshopId, { className: "wid mono", title: `Workshop ID: ${w.workshopId}` }) : ""}
            ${canManage ? _wsSwitch(w.workshopId, true, I18n.t("Выключить мод в конфиге")) : ""}
          </div>
          ${(w.mods || []).length
            ? `<div class="ws-mods">${w.mods.map((m) => copyValue(m, { className: "chip mono" })).join("")}</div>`
            : ""}
        </div>`).join("");
      }
    }
    if (mods.length) {
      html += I18n.msg`<p class="mods-note">Моды из конфига (Mods=) — ${mods.length}</p>`;
      html += `<div class="mods-chips">${mods.map((m) => copyValue(m, { className: "chip mono" })).join("")}</div>`;
    }
    if ((data.unbound || []).length && data.mappingSource === "disk") {
      html += I18n.msg`<p class="mods-note">Без привязки к Workshop — ${data.unbound.length}</p>`;
      html += `<div class="mods-chips">${data.unbound.map((m) => copyValue(m, { className: "chip mono" })).join("")}</div>`;
    }
    if (!ws.length) {
      html += I18n.msg`<p class="mods-note">Один Workshop-элемент может содержать несколько модов — сопоставление по конфигу невозможно.</p>`;
    }
  }
  if (disabledList.length) {
    html += I18n.msg`<p class="mods-note">Выключенные — ${disabledList.length}</p>`;
    html += disabledList.map((d) => `
      <div class="mod-row disabled-row">
        ${copyValue((d.modIds || [])[0] || d.workshopId, { className: "m-name mono", text: d.title || d.workshopId, title: (d.modIds || []).join(", ") })}
        <span class="m-ws">
          ${copyValue(d.workshopId, { className: "wid mono", text: shortWsId(d.workshopId), title: `Workshop ID: ${d.workshopId}` })}
        </span>
        ${canManage ? _wsSwitch(d.workshopId, false, I18n.t("Включить мод обратно")) : ""}
      </div>`).join("");
  }
  body.dataset.state = "ok";
  setStaticMarkup(body, html);
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
    ? String(a.title || a.workshopId || "").localeCompare(String(b.title || b.workshopId || ""), I18n.locale)
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
      note.textContent = I18n.msg`Показано ${shown[0]} из ${shown[1]}`;
    } else {
      note.hidden = true;
    }
  }

  if (shown && shown[0] === 0 && !(view.mods || []).length) {
    const body = $("modsBody");
    body.dataset.state = "empty";
    body.innerHTML = I18n.msg`<p class="list-empty"><strong>Ничего не найдено.</strong> Измените запрос или сбросьте фильтр.</p>`;
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
    if (res.ok === false || res.error) throw new Error(res.error || I18n.t("не удалось изменить конфиг"));
    toast(enable ? I18n.t("Мод включён в конфиг — заработает после рестарта")
                 : I18n.t("Мод выключен из конфига — заработает после рестарта"), "ok");
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

const backupsPager = { page: 0, size: 10 };

function renderBackups(data) {
  const body = $("backupsBody");
  if (!data.ok) {
    setDomProperty($("backupsPager"), "hidden", true);
    setListMessage(body, "error", `<p class="list-error">${esc(data.error || I18n.t("нет данных"))}</p>`);
    return;
  }
  renderBkSchedule(data);
  renderBkJournal(data.journal || [], !!data.journalHasMore);
  S.backupsItems = data.items || [];
  renderKpis();
  renderBackupsPage();
}

function renderBackupsPage() {
  const body = $("backupsBody"), items = S.backupsItems;
  backupsPager.page = Math.min(backupsPager.page, Math.max(0, Math.ceil(items.length / backupsPager.size) - 1));
  const start = backupsPager.page * backupsPager.size;
  const pageItems = items.slice(start, start + backupsPager.size);
  setDomProperty($("backupsPager"), "hidden", items.length <= backupsPager.size);
  setDomProperty($("backupsPrev"), "disabled", backupsPager.page === 0);
  setDomProperty($("backupsNext"), "disabled", start + pageItems.length >= items.length);
  setDomProperty($("backupsRange"), "textContent", items.length ? I18n.msg`${start + 1}–${start + pageItems.length} из ${items.length} · Страница ${backupsPager.page + 1}` : "");
  if (!items.length) {
    setListMessage(body, "empty", I18n.msg`<p class="list-empty">Бэкапов ещё нет.</p>`);
    return;
  }
  syncRows(body, pageItems, (item) => item.name, (b) => {
    const template = document.createElement("template");
    template.innerHTML = I18n.msg`
    <div class="backup-row">
      <span class="b-name mono" title="${esc(b.name)} — создан ${esc(b.mtime)}">${esc(b.name)}</span>
      <span class="b-size mono" title="размер архива">${esc(b.size != null ? fmtBytes(b.size) : b.sizeText || "—")}</span>
      <span class="b-age mono" title="создан ${esc(b.mtime)}">${esc(relTime(b.mtime))}</span>
      <div class="backup-actions"><button class="icon-btn backup-action" data-b="dl" data-name="${esc(b.name)}" title="Скачать" aria-label="Скачать ${esc(b.name)}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11m0 0 4-4m-4 4-4-4M5 20h14"/></svg><span>Скачать</span>
      </button>
      <button class="icon-btn backup-action" data-b="verify" data-name="${esc(b.name)}" title="Проверить архив" aria-label="Проверить ${esc(b.name)}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.4-2.9 8.2-7 10-4.1-1.8-7-5.6-7-10V6l7-3z"/><path d="M9 12l2 2 4-4"/></svg><span>Проверить</span>
      </button>
      <button class="icon-btn backup-action" data-b="restore" data-name="${esc(b.name)}" title="Восстановить" aria-label="Восстановить из ${esc(b.name)}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9a8 8 0 1 1 2 6"/><path d="M4 4v5h5"/></svg><span>Восстановить…</span>
      </button>
      <button class="icon-btn danger" data-b="del" data-name="${esc(b.name)}" title="Удалить" aria-label="Удалить ${esc(b.name)}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V5h6v2m-8 0 1 13h8l1-13"/></svg>
      </button>
      </div>
      <p class="backup-result hint" hidden></p>
    </div>`;
    return template.content.firstElementChild;
  }, (row, b) => {
    const size = row.querySelector(".b-size"), age = row.querySelector(".b-age"), name = row.querySelector(".b-name");
    const sizeText = b.size != null ? fmtBytes(b.size) : b.sizeText || "—", ageText = relTime(b.mtime), dateTitle = I18n.msg`создан ${b.mtime}`;
    if (size.textContent !== sizeText) size.textContent = sizeText;
    if (age.textContent !== ageText) age.textContent = ageText;
    if (age.title !== dateTitle) age.title = dateTitle;
    const title = `${b.name} — ${dateTitle}`;
    if (name.title !== title) name.title = title;
    const verified = S.op?.history?.find(h => h.op === "verify-backup" && h.ok && h.archive?.name === b.name);
    const result = row.querySelector(".backup-result");
    setDomProperty(result, "hidden", !verified);
    const notes = [];
    if (verified && !verified.archive.hasServerIni) notes.push(I18n.t("Нет конфигурации сервера (Server/*.ini)"));
    if (verified && !verified.archive.hasMapData) notes.push(I18n.t("Нет данных мира"));
    setDomProperty(result, "textContent", verified ? [I18n.msg`Проверен ${fmtTime(verified.finishedAt)} · файлов: ${verified.archive.files}`, ...notes].join(" · ") : "");
  });
  updateButtons();
}

$("backupsPrev").addEventListener("click", () => {
  if (backupsPager.page === 0) return;
  backupsPager.page--;
  renderBackupsPage();
});
$("backupsNext").addEventListener("click", () => {
  if ((backupsPager.page + 1) * backupsPager.size >= S.backupsItems.length) return;
  backupsPager.page++;
  renderBackupsPage();
});

$("backupsBody").addEventListener("click", (event) => {
      const btn = event.target.closest("button[data-b]");
      if (!btn || btn.disabled) return;
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

function renderBkSchedule(data) {
  if (staleSettings(data.settingsVersion)) return;
  const ab = data.autoBackup || {};
  const sw = $("bkAutoSwitch");
  if (sw && settingsCanRender("bkAutoSwitch")) sw.checked = !!ab.enabled;
  const t = $("bkAutoTime");
  if (t && settingsCanRender("bkAutoTime")) t.value = ab.time || "03:00";
  const keep = $("bkAutoKeep");
  if (keep && settingsCanRender("bkAutoKeep")) keep.value = data.maxBackups ?? 7;
  const stop = $("bkAutoStop");
  if (stop && settingsCanRender("bkAutoStop")) stop.checked = !!ab.stopServer;
  const offset = ab.nextRun?.match(/(?:Z|[+-]\d{2}:\d{2})$/)?.[0] || ab.timeZone;
  const zone = offset === "Z" || offset === "+00:00" ? "UTC" : offset?.startsWith("+") || offset?.startsWith("-") ? `UTC${offset}` : offset;
  const next = ab.enabled && ab.nextRun ? I18n.msg`Следующий запуск: ${fmtTime(ab.nextRun)} · ваше время` : ab.enabled ? I18n.t("Следующий запуск рассчитывается") : I18n.t("Расписание выключено");
  setDomProperty($("bkScheduleContext"), "textContent", next + (zone ? I18n.msg` · Часовой пояс сервера: ${zone}` : ""));
}

const bkJournal = { page: 0, size: 25, shownPage: 0, shownSize: 25, retryPage: 0, retrySize: 25, signature: null, request: 0, loading: false, hasMore: false };

function renderBkJournal(journal, hasMore = false) {
  const signature = JSON.stringify([journal, hasMore]);
  const changed = bkJournal.signature !== signature;
  bkJournal.signature = signature;
  if (!changed) return;
  if (bkJournal.page === 0 && bkJournal.size === 25) {
    // Supersede older HTTP pages when the live stream supplies the current page.
    bkJournal.request++;
    bkJournal.loading = false;
    renderBkJournalPage(journal.slice(0, 25), hasMore || journal.length > 25);
  } else {
    loadBkJournalPage();
  }
}

function updateBkJournalPager(count) {
  setDomProperty($("bkJournalPager"), "hidden", count === 0 && bkJournal.page === 0);
  setDomProperty($("bkJournalPrev"), "disabled", bkJournal.loading || bkJournal.page === 0);
  setDomProperty($("bkJournalNext"), "disabled", bkJournal.loading || !bkJournal.hasMore);
  setDomProperty($("bkJournalPageSize"), "disabled", bkJournal.loading);
  setDomProperty($("bkJournalBody"), "ariaBusy", String(bkJournal.loading));
}

function renderBkJournalPage(journal, hasMore) {
  const body = $("bkJournalBody");
  if (!body) return;
  bkJournal.shownPage = bkJournal.page;
  bkJournal.shownSize = bkJournal.size;
  bkJournal.hasMore = hasMore;
  setDomProperty($("bkJournalError"), "hidden", true);
  setDomProperty($("bkJournalRetry"), "hidden", true);
  updateBkJournalPager(journal.length);
  const start = bkJournal.page * bkJournal.size + 1;
  setDomProperty($("bkJournalRange"), "textContent", journal.length ? I18n.msg`${start}–${start + journal.length - 1} · Страница ${bkJournal.page + 1}` : "");
  if (!journal.length) {
    setDomProperty(body.dataset, "state", "empty");
    setStaticMarkup(body, I18n.msg`<p class="list-empty">Бэкапов ещё не было.</p>`);
    return;
  }
  setDomProperty(body.dataset, "state", "ok");
  const trig = { manual: I18n.t("вручную"), scheduled: I18n.t("по расписанию") };
  setStaticMarkup(body, journal.map((j) => `
    <div class="journal-row${j.status === "error" ? " j-err" : ""}">
      <span class="j-date mono" title="${esc(j.ts)}">${esc((j.ts || "").slice(0, 16).replace("T", " "))}</span>
      <span class="j-trig">${esc(trig[j.trigger] || j.trigger || "")}</span>
      <span class="j-name mono" title="${esc(j.name || j.error || "")}">${esc(j.name || "—")}</span>
      <span class="j-size mono">${j.status === "error" ? "—" : esc(fmtBytes(j.size))}</span>
      <span class="j-status" title="${esc(j.error || "")}">${j.status === "error" ? I18n.t("ошибка") : I18n.t("готово")}</span>
    </div>`).join(""));
}

async function loadBkJournalPage() {
  const request = ++bkJournal.request;
  const page = bkJournal.page, size = bkJournal.size;
  bkJournal.loading = true;
  $("bkJournalError").hidden = true;
  $("bkJournalRetry").hidden = true;
  updateBkJournalPager($("bkJournalBody").querySelectorAll(".journal-row").length);
  try {
    const data = await api(`/api/backups/journal?limit=${size}&offset=${page * size}`);
    if (request !== bkJournal.request) return;
    if (!data.ok) throw new Error(data.error || I18n.t("Не удалось загрузить журнал"));
    bkJournal.loading = false;
    const items = data.items || [];
    if (!items.length && page > 0) {
      bkJournal.page = 0;
      return loadBkJournalPage();
    }
    renderBkJournalPage(items, !!data.hasMore);
  } catch (error) {
    if (request !== bkJournal.request) return;
    bkJournal.loading = false;
    // Keep the loaded rows and their range visible if navigation fails.
    bkJournal.retryPage = page;
    bkJournal.retrySize = size;
    bkJournal.page = bkJournal.shownPage;
    bkJournal.size = bkJournal.shownSize;
    $("bkJournalPageSize").value = String(bkJournal.size);
    updateBkJournalPager($("bkJournalBody").querySelectorAll(".journal-row").length);
    $("bkJournalError").textContent = I18n.msg`${error.message || error}. Повторите загрузку.`;
    $("bkJournalError").hidden = false;
    $("bkJournalRetry").hidden = false;
  }
}

$("bkJournalPrev").addEventListener("click", () => { bkJournal.page--; loadBkJournalPage(); });
$("bkJournalNext").addEventListener("click", () => { bkJournal.page++; loadBkJournalPage(); });
$("bkJournalPageSize").addEventListener("change", () => {
  bkJournal.size = Number($("bkJournalPageSize").value);
  bkJournal.page = 0;
  loadBkJournalPage();
});
$("bkJournalRetry").addEventListener("click", () => {
  bkJournal.page = bkJournal.retryPage;
  bkJournal.size = bkJournal.retrySize;
  $("bkJournalPageSize").value = String(bkJournal.size);
  loadBkJournalPage();
});

function confirmRestore(name) {
  modal.open({
    title: I18n.t("Восстановление из бэкапа"),
    danger: true,
    okLabel: I18n.t("Восстановить"),
    bodyHTML: I18n.msg`
      <p>Текущий мир и конфиги будут <b>полностью заменены</b> содержимым архива
      <span class="mono">${esc(name)}</span>.</p>
      <p>Сервер будет остановлен через RCON с предупреждением игрокам, затем запущен снова.</p>
      <label class="check"><input type="checkbox" id="restoreAck" /> Я понимаю, что текущее состояние мира будет потеряно</label>
    `,
    onConfirm: async () => {
      if (!$("restoreAck") || !$("restoreAck").checked) throw new Error(I18n.t("Подтвердите замену мира флажком"));
      return action("restore", { name });
    },
  });
  $("restoreAck")?.addEventListener("change", () => { $("modalOk").disabled = !$("restoreAck").checked; });
  $("modalOk").disabled = true;
}

function confirmDeleteBackup(name) {
  modal.open({
    title: I18n.t("Удалить бэкап?"),
    danger: true,
    okLabel: I18n.t("Удалить"),
    bodyHTML: I18n.msg`<p>Архив <span class="mono">${esc(name)}</span> будет удалён без возможности восстановления.</p>`,
    onConfirm: async () => {
      const res = await api(`/api/backup?name=${encodeURIComponent(name)}`, { method: "DELETE" });
      if (res.error) throw new Error(res.error);
      toast(I18n.t("Бэкап удалён"), "ok");
      refreshBackups();
    },
  });
}

function openBackupModal() {
  modal.open({
    title: I18n.t("Создать бэкап"),
    okLabel: I18n.t("Создать"),
    bodyHTML: I18n.msg`
      <p>Архив собирается из каталога данных сервера (мир, конфиги, whitelist). Логи в бэкап не входят.</p>
      <label class="check"><input type="checkbox" id="bkStop" /> Остановить сервер на время бэкапа (надёжнее для целостности)</label>
      <p>Без остановки пульт сначала отправит команду <span class="mono">save</span> через RCON.</p>
    `,
    onConfirm: async () => {
      return action("backup", { stopServer: $("bkStop")?.checked || false });
    },
  });
}

$("btnBackup").addEventListener("click", openBackupModal);

/* ───────────────────────── события ───────────────────────── */

const EVENT_LABELS = {
  start: I18n.t("Запуск"), stop: I18n.t("Остановка"), restart: I18n.t("Рестарт"), backup: I18n.t("Бэкап"),
  restore: I18n.t("Восстановление"), update: I18n.t("Обновление"), "update-check": I18n.t("Проверка"),
  auto: I18n.t("Автообновление"), console: I18n.t("Консоль"), warn: I18n.t("Внимание"), delete: I18n.t("Удаление"),
  "rcon-error": "RCON", error: I18n.t("Ошибка"), docker: "Docker", "backup-delete": I18n.t("Бэкап"),
  mods: I18n.t("Моды"),
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
const eventsPager = { page: 0, size: 25 };

function renderEvents(data) {
  eventsData = data;
  const body = $("eventsBody");
  if (!data.ok) {
    setDomProperty($("eventsPager"), "hidden", true);
    setDomProperty(body.dataset, "state", "error");
    setStaticMarkup(body, `<p class="list-error">${esc(data.error || I18n.t("нет данных"))}</p>`);
    return;
  }
  const items = data.items || [];
  renderRecent(items);
  const visible = eventsFilter === "all"
    ? items
    : items.filter((ev) => (EVENT_GROUPS[eventsFilter] || []).includes(ev.type));
  eventsPager.page = Math.min(eventsPager.page, Math.max(0, Math.ceil(visible.length / eventsPager.size) - 1));
  const offset = eventsPager.page * eventsPager.size;
  const pageItems = visible.slice(offset, offset + eventsPager.size);
  setDomProperty($("eventsPager"), "hidden", visible.length <= eventsPager.size);
  setDomProperty($("eventsPrev"), "disabled", eventsPager.page === 0);
  setDomProperty($("eventsNext"), "disabled", offset + eventsPager.size >= visible.length);
  setDomProperty($("eventsRange"), "textContent", visible.length
    ? I18n.msg`${offset + 1}–${offset + pageItems.length} из ${visible.length} · Страница ${eventsPager.page + 1}` : "");
  if (!visible.length) {
    setDomProperty(body.dataset, "state", "empty");
    setStaticMarkup(body, eventsFilter === "all"
      ? I18n.msg`<p class="list-empty">Событий ещё нет.</p>`
      : I18n.msg`<p class="list-empty">Нет событий в этой категории.</p>`);
    return;
  }
  setDomProperty(body.dataset, "state", "ok");
  const today = new Date();
  const yest = new Date(today.getTime() - 86400000);
  const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  let html = "";
  let lastDay = "";
  for (const ev of pageItems) {
    const d = new Date(ev.ts);
    const key = isNaN(d) ? "" : d.toDateString();
    if (key && key !== lastDay) {
      lastDay = key;
      const label = sameDay(d, today) ? I18n.t("Сегодня") : sameDay(d, yest) ? I18n.t("Вчера") : dayFmt.format(d);
      html += `<div class="event-day">${esc(label)}</div>`;
    }
    html += `<div class="event-row" data-kind="${esc(ev.type)}">
      <span class="e-time mono" title="${esc(ev.ts)}">${esc(relTime(ev.ts) || fmtTime(ev.ts))}</span>
      <span class="e-text"><b>${esc(EVENT_LABELS[ev.type] || ev.type)}.</b> ${esc(ev.text)}</span>
    </div>`;
  }
  setStaticMarkup(body, html);
}

$("eventFilters").addEventListener("click", (e) => {
  const btn = e.target.closest(".chip[data-ef]");
  if (!btn) return;
  eventsFilter = btn.dataset.ef;
  eventsPager.page = 0;
  document.querySelectorAll("#eventFilters .chip").forEach((c) => c.setAttribute("aria-pressed", String(c === btn)));
  if (eventsData) renderEvents(eventsData);
});

$("eventsPrev").addEventListener("click", () => {
  if (eventsPager.page === 0) return;
  eventsPager.page--;
  if (eventsData) renderEvents(eventsData);
});
$("eventsNext").addEventListener("click", () => {
  if ($("eventsNext").disabled) return;
  eventsPager.page++;
  if (eventsData) renderEvents(eventsData);
});
$("eventsPageSize").addEventListener("change", () => {
  eventsPager.size = Number($("eventsPageSize").value);
  eventsPager.page = 0;
  if (eventsData) renderEvents(eventsData);
});

/* лента последних событий на обзоре: те же данные, только первые 5 строк */
function renderRecent(items) {
  const body = $("recentBody");
  if (!body) return;
  const slice = (items || []).slice(0, 5);
  if (!slice.length) {
    setDomProperty(body.dataset, "state", "empty");
    setStaticMarkup(body, I18n.msg`<p class="list-empty">Событий ещё нет.</p>`);
    return;
  }
  setDomProperty(body.dataset, "state", "ok");
  setStaticMarkup(body, slice.map((ev) => `
    <div class="event-row" data-kind="${esc(ev.type)}">
      <span class="e-time mono" title="${esc(ev.ts)}">${esc(relTime(ev.ts) || fmtTime(ev.ts))}</span>
      <span class="e-text"><b>${esc(EVENT_LABELS[ev.type] || ev.type)}.</b> ${esc(ev.text)}</span>
    </div>`).join(""));
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
  if (operationBusy() || $("consoleInput").disabled) return;
  const input = $("consoleInput");
  const cmd = input.value.trim();
  if (!cmd) return;
  if (consoleHistory.items[consoleHistory.items.length - 1] !== cmd) consoleHistory.items.push(cmd);
  consoleHistory.idx = consoleHistory.items.length;
  input.value = "";
  consoleAppend(`> ${cmd}`, "c-dim");
  try {
    const res = await api("/api/rcon", { method: "POST", body: { command: cmd } });
    if (res.error) consoleAppend(I18n.msg`Ошибка: ${res.error}`, "c-err");
    else consoleAppend(res.output || I18n.t("(без ответа)"));
  } catch (err) {
    consoleAppend(I18n.msg`Ошибка: ${err.message || err}`, "c-err");
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
        title: I18n.t("Объявление игрокам"),
        okLabel: I18n.t("Отправить"),
        bodyHTML: I18n.msg`
          <p>Текст уйдёт в игровой чат командой <span class="mono">servermsg</span>.</p>
          <label class="field">Текст объявления
            <input type="text" id="bcText" maxlength="200" style="height:38px;color:var(--ink);background:var(--bg-deep);border:1px solid var(--line-strong);border-radius:8px;padding:0 12px;" />
          </label>
        `,
        onConfirm: async () => {
          const text = ($("bcText")?.value || "").trim();
          if (!text) throw new Error(I18n.t("Введите текст объявления"));
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
  return (text || "").split("\n").filter(raw => raw.trim()).map((raw) => {
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
    S.logsText = null;
    $("logsResult").hidden = true;
    $("logsOut").textContent = I18n.t("Логи контейнера доступны только при запуске пульта на хосте сервера.");
    return;
  }
  if (!data.ok) {
    $("logsError").textContent = (data.error || I18n.t("Логи недоступны")) + (S.logsUpdatedAt ? I18n.msg` · последние данные: ${fmtTime(S.logsUpdatedAt)}` : "");
    $("logsError").hidden = false;
    if (!S.logsUpdatedAt) $("logsOut").textContent = I18n.t("Нет данных логов");
    return;
  }
  $("logsError").hidden = true;
  $("logsLimit").hidden = !data.truncated;
  S.logsUpdatedAt = Date.now();
  // Keep freshness/error recovery, but avoid parsing and rebuilding unchanged logs.
  const text = data.text || "";
  if (S.logsText === text) return;
  S.logsText = text;
  S.logsLines = parseLogs(text);
  renderLogsFiltered();
}

function renderLogsFiltered() {
  const pre = $("logsOut");
  const scrollTop = pre.scrollTop;
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
  const f = ($("logsFilter")?.value || "").trim().toLowerCase();
  const level = S.logsLevel || "all";
  const source = S.logsLines || [];
  const filtered = !!f || level !== "all" || !!S.logsSince || !!S.logsUntil;
  const lines = source.filter((l) =>
    (level === "all" || l.level === level) && (!f || l.view.toLowerCase().includes(f)) &&
    (!S.logsSince || Date.parse(l.raw.slice(0, l.raw.indexOf(" "))) >= S.logsSince) &&
    (!S.logsUntil || Date.parse(l.raw.slice(0, l.raw.indexOf(" "))) <= S.logsUntil));
  $("logsScope").hidden = !S.logsSince;
  $("logsPeriod").textContent = S.logsSince ? I18n.msg`Логи операции${S.logsProfile ? ` · ${S.logsProfile}` : ""} · ${fmtTime(S.logsSince)}${S.logsUntil ? ` — ${fmtTime(S.logsUntil)}` : I18n.t(" · продолжается")}` : "";
  $("logsResult").hidden = false;
  $("logsCount").textContent = I18n.msg`Строк: ${lines.length} из ${source.length}`;
  $("logsResetFilters").hidden = !filtered;
  // Only identical source records can share a row: identifiers and timestamps matter.
  const merged = [];
  for (const l of lines) {
    const key = l.raw;
    const last = merged[merged.length - 1];
    if (last && last.key === key) last.n += 1;
    else merged.push({ view: l.view, level: l.level, key, n: 1 });
  }
  pre.innerHTML = merged.length ? merged.map((l) => {
    const cls = l.level === "error" ? ' class="l-err"' : l.level === "warn" ? ' class="l-warn"' : "";
    const dup = l.n > 1 ? `<span class="l-dup">× ${l.n}</span>` : "";
    return `<span${cls}>${esc(l.view)}${dup}</span>`;
  }).join("\n") : esc(source.length ? I18n.t("Нет строк по выбранным фильтрам.") : I18n.t("Нет данных логов"));
  if (atBottom && S.logsAuto) pre.scrollTop = pre.scrollHeight;
  else pre.scrollTop = scrollTop;
}

$("logsFilter").addEventListener("input", renderLogsFiltered);
$("logsAuto").addEventListener("change", () => { S.logsAuto = $("logsAuto").checked; });
$("logsResetFilters").addEventListener("click", () => {
  $("logsFilter").value = "";
  S.logsLevel = "all";
  S.logsSince = S.logsUntil = 0;
  S.logsProfile = "";
  document.querySelectorAll("#logLevels .chip").forEach(c => c.setAttribute("aria-pressed", String(c.dataset.level === "all")));
  renderLogsFiltered();
  $("logsFilter").focus();
  refreshLogs();
});
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

const WARN_OPTIONS = I18n.msg`
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
    toast(I18n.t("Запрошена отмена автообновления модов"), "ok");
  } catch (e) {
    toast(e.message || String(e), "error");
  } finally {
    updateButtons();
  }
});
$("btnStop").addEventListener("click", () => {
  modal.open({
    title: I18n.t("Остановить сервер?"),
    danger: true,
    okLabel: I18n.t("Остановить"),
    bodyHTML: I18n.msg`<p>Мир будет сохранён (RCON <span class="mono">quit</span>), затем контейнер остановится.</p>${WARN_OPTIONS}`,
    onConfirm: async () => action("stop", { warnSeconds: Number($("warnSel").value) }),
  });
});
$("btnRestart").addEventListener("click", () => {
  modal.open({
    title: I18n.t("Перезапустить сервер?"),
    okLabel: I18n.t("Перезапустить"),
    bodyHTML: I18n.msg`<p>Мир будет сохранён, контейнер остановится и запустится снова.</p>${WARN_OPTIONS}`,
    onConfirm: async () => action("restart", { warnSeconds: Number($("warnSel").value) }),
  });
});
$("btnSaveWorld").addEventListener("click", async () => {
  $("consoleInput").value = "save";
  $("consoleForm").requestSubmit();
});
$("btnCheckUpd").addEventListener("click", async () => {
  if (S.actionPending || S.op?.active) return;
  S.actionPending = true; updateButtons();
  showLocalResult("pending", I18n.t("Проверка обновлений — выполняется"), I18n.t("Сверяем образ. Дождитесь ответа; отмена этой проверки не поддерживается."));
  try {
    const res = await api("/api/action", { method: "POST", body: { op: "check-update" }, timeout: 25000 });
    if (res.error || res.ok === false) throw new Error(res.error || I18n.t("Проверка не принята"));
    const c = res.check || {};
    if (c.error) throw new Error(c.error);
    showLocalResult("ok", I18n.t("Проверка обновлений — готово"), c.available ? I18n.t("Доступно обновление образа. Откройте обслуживание, чтобы проверить версию и применить обновление.") : I18n.t("Обновлений нет — образ актуален."));
    refreshOverview();
  } catch (e) {
    showActionError("check-update", e);
  } finally {
    S.actionPending = false; updateButtons();
  }
});
$("btnApplyUpd").addEventListener("click", () => {
  modal.open({
    title: I18n.t("Обновить сервер?"),
    okLabel: I18n.t("Обновить"),
    bodyHTML: I18n.msg`
      <p>Новый образ скачается заранее, затем при несовпадении digest сервер
      сохранит мир, предупредит игроков и перезапустится на новой версии.</p>
      ${WARN_OPTIONS}
    `,
    onConfirm: async () => action("apply-update", { warnSeconds: Number($("warnSel").value) }),
  });
});

/* ─────────────────────── проверка модов (RCON) ─────────────────────── */

$("btnCheckDashboardUpd").addEventListener("click", async () => {
  if (operationBusy()) return;
  S.actionPending = true; updateButtons();
  showLocalResult("pending", I18n.t("Проверка обновлений пульта"), I18n.t("Сверяем образ. Дождитесь ответа; отмена этой проверки не поддерживается."));
  try {
    const res = await api("/api/action", { method: "POST", body: { op: "check-dashboard-update" }, timeout: 65000 });
    if (res.error || res.ok === false || res.check?.error) throw new Error(res.error || res.check?.error || I18n.t("Проверка не принята"));
    if (S.overview) renderOverview({ ...S.overview, dashboardUpdate: res.check });
    showLocalResult("ok", I18n.t("Проверка обновлений пульта"), res.check?.available === true ? I18n.t("Доступна новая версия") : I18n.t("Образ пульта уже актуален"));
    refreshOverview();
  } catch (e) {
    showActionError("check-dashboard-update", e);
  } finally {
    S.actionPending = false; updateButtons();
  }
});
$("btnApplyDashboardUpd").addEventListener("click", () => {
  modal.open({
    title: I18n.t("Обновить контейнер пульта?"),
    okLabel: I18n.t("Обновить"),
    bodyHTML: `<p>${esc(I18n.t("Новый образ скачается заранее. Пульт перезапустится, затем соединение восстановится автоматически. Игровой сервер продолжит работу."))}</p>`,
    onConfirm: async () => action("apply-dashboard-update"),
  });
});

$("btnCheckMods").addEventListener("click", async () => {
  if (S.actionPending || S.op?.active) return;
  S.actionPending = true; updateButtons();
  showLocalResult("pending", I18n.t("Проверка модов — выполняется"), I18n.t("Ожидаем ответ RCON; отмена этой проверки не поддерживается."));
  try {
    const res = await api("/api/action", { method: "POST", body: { op: "check-mods-update" } });
    if (res.error || res.ok === false) throw new Error(res.error || I18n.t("Проверка не принята"));
    showLocalResult("ok", I18n.t("Проверка модов — запрос принят"), I18n.t("Результат сервера появится в разделе «Обслуживание». Принимаемый запрос ещё не подтверждает актуальность пакетов."));
    toast(I18n.t("Проверка модов запущена — результат появится в карточке"), "ok");
  } catch (e) {
    showActionError("check-mods-update", e);
  } finally {
    S.actionPending = false; updateButtons();
  }
});
$("btnApplyMods").addEventListener("click", () => {
  modal.open({
    title: I18n.t("Перезапустить для обновления модов?"),
    okLabel: I18n.t("Перезапустить"),
    bodyHTML: I18n.msg`
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
    title: I18n.t("Перезапустить сервер?"),
    okLabel: I18n.t("Перезапустить"),
    bodyHTML: I18n.msg`
      <p>Состав модов, включённый в конфиге, заработает после рестарта: игрокам
      придёт предупреждение, мир сохранится (RCON <span class="mono">quit</span>).</p>
      ${WARN_OPTIONS}
    `,
    onConfirm: async () => action("restart", { warnSeconds: Number($("warnSel").value) }),
  });
});

/* ───────────────────────── настройки автообновления ───────────────────────── */

const SETTING_GROUPS = {
  playerNotifications: { ids: ["playerLanguage"], card: "playerNotifications", title: I18n.t("Предупреждения игрокам") },
  autoUpdate: { ids: ["autoSwitch", "autoInterval", "autoWarn", "buBackup"], card: "sec-updates", title: I18n.t("Автообновление образа") },
  watchdog: { ids: ["wdSwitch", "wdThreshold", "wdGracePeriod", "wdRestart"], card: "sec-watchdog", title: "Watchdog RCON" },
  modsUpdate: { ids: ["modsAutoSwitch", "modsAutoInterval", "modsAutoAction", "modsAutoWarn"], card: "sec-mods-update", title: I18n.t("Автообновление модов") },
  telegram: { ids: ["tgSwitch", "tgToken", "tgChat", "tgLanguage", "tgOps", "tgBackup", "tgUpdate", "tgProblems"], card: "sec-notify", title: "Telegram" },
  autoBackup: { ids: ["bkAutoSwitch", "bkAutoTime", "bkAutoKeep", "bkAutoStop"], card: "sec-bkauto", title: I18n.t("Расписание бэкапов") },
};
const settingStates = new Map();
document.addEventListener("pz:before-update", (event) => {
  if (S.op?.active || S.actionPending || pendingMutations || [...settingStates.values()].some(state => state.pending || state.dirty)
      || !$('modalRoot').hidden || $('commandDialog').open || $('consoleInput').value.trim()) event.preventDefault();
});
let settingsQueue = Promise.resolve();

function settingGroup(id) {
  return Object.keys(SETTING_GROUPS).find((key) => SETTING_GROUPS[key].ids.includes(id));
}

function settingState(group) {
  if (!settingStates.has(group)) settingStates.set(group, { generation: 0, pending: 0, dirty: false });
  return settingStates.get(group);
}

function settingsCanRender(id) {
  const state = settingState(settingGroup(id));
  return !state.pending && !state.dirty && !$(id).matches(":focus");
}

function staleSettings(version) {
  return !!(version && S.settingsVersion && version.epoch === S.settingsVersion.epoch && version.revision < S.settingsVersion.revision);
}

function acceptSettings(settings) {
  if (staleSettings(settings.version) || (!settings.version && S.settingsVersion)) return S.serverSettings || settings;
  if (settings.version) S.settingsVersion = settings.version;
  S.serverSettings = settings;
  return settings;
}

function settingsFeedback(group, state, message) {
  const definition = SETTING_GROUPS[group];
  const card = $(definition.card) || $(definition.ids[0]).closest("section");
  let feedback = card.querySelector(":scope > .settings-feedback");
  if ((group === "autoBackup" || group === "telegram" || group === "playerNotifications") && state === "ok") {
    feedback?.remove();
    toast(message, "ok");
    return;
  }
  if (!feedback) {
    feedback = document.createElement("div");
    feedback.className = "settings-feedback";
    feedback.innerHTML = I18n.html('<p role="status"></p><button type="button" class="btn small" hidden>Повторить сохранение</button>');
    feedback.querySelector("button").addEventListener("click", () => group === "autoBackup" ? pushBkSettings() : pushSettings(group));
    card.appendChild(feedback);
  }
  feedback.dataset.state = state;
  feedback.querySelector("p").textContent = message;
  feedback.querySelector("button").hidden = state !== "error";
  updateButtons();
}

function saveSettingsGroup(group, body) {
  const state = settingState(group);
  const generation = ++state.generation;
  state.pending++;
  state.dirty = true;
  settingsFeedback(group, "pending", I18n.msg`${SETTING_GROUPS[group].title}: сохраняется…`);
  const run = async () => {
    try {
      const res = await api("/api/settings", { method: "POST", body });
      if (res.error || res.ok === false) throw new Error(res.error || I18n.t("Сохранение не принято"));
      // Only the latest local edit can release the fields or clear the secret.
      if (generation === state.generation) {
        if (group === "telegram" && $("tgToken").value.trim() === body.telegram.botToken) $("tgToken").value = "";
        state.dirty = false;
        if (res.settings) acceptSettings(res.settings);
        settingsFeedback(group, "ok", group === "telegram" && body.telegram.botToken
          ? I18n.t("Токен сохранён. Введите новый, чтобы заменить.")
          : I18n.msg`${SETTING_GROUPS[group].title}: сохранено`);
      }
      return true;
    } catch (e) {
      if (generation === state.generation) {
        settingsFeedback(group, "error", I18n.msg`${SETTING_GROUPS[group].title}: ${e.name === "AbortError" ? I18n.t("время ожидания истекло") : e.message || e}. Ввод сохранён — повторите сохранение.`);
      }
      return false;
    } finally {
      state.pending--;
    }
  };
  const result = settingsQueue.then(run);
  settingsQueue = result.then(() => undefined);
  return result;
}

function pushSettings(eventOrGroup) {
  const group = typeof eventOrGroup === "string" ? eventOrGroup : settingGroup(eventOrGroup?.target?.id) || "telegram";
  if (group === "watchdog" && !$("wdGracePeriod").reportValidity()) return Promise.resolve(false);
  const body = {
    playerNotifications: { language: $("playerLanguage").value },
    autoUpdate: {
      enabled: $("autoSwitch").checked,
      intervalHours: Number($("autoInterval").value),
      warnSeconds: Number($("autoWarn").value),
      backupBeforeUpdate: $("buBackup").checked,
    },
    watchdog: {
      enabled: $("wdSwitch").checked,
      thresholdMin: Number($("wdThreshold").value),
      gracePeriodMin: Number($("wdGracePeriod").value),
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
      language: $("tgLanguage").value,
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
  return saveSettingsGroup(group, { [group]: body[group] }).then((accepted) => {
    if (accepted && !settingState(group).pending) refreshOverview();
    return accepted;
  });
}

Object.values(SETTING_GROUPS).forEach(({ ids }) => ids.forEach((id) => {
  $(id).addEventListener("input", () => {
    const state = settingState(settingGroup(id));
    state.dirty = true;
    state.generation++;
  });
}));

$("autoSwitch").addEventListener("change", pushSettings);

/* расписание бэкапов: отдельная карточка на странице «Бэкапы» */
function pushBkSettings() {
  const body = {
    autoBackup: {
      enabled: $("bkAutoSwitch").checked,
      time: $("bkAutoTime").value || "03:00",
      stopServer: $("bkAutoStop").checked,
    },
    backup: { maxBackups: Number($("bkAutoKeep").value) },
  };
  return saveSettingsGroup("autoBackup", body).then((accepted) => {
    if (accepted && !settingState("autoBackup").pending) refreshBackups();
    return accepted;
  });
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
$("wdGracePeriod").addEventListener("change", pushSettings);
$("wdRestart").addEventListener("change", pushSettings);
$("modsAutoSwitch").addEventListener("change", pushSettings);
$("modsAutoInterval").addEventListener("change", pushSettings);
$("modsAutoAction").addEventListener("change", pushSettings);
$("modsAutoWarn").addEventListener("change", pushSettings);
$("tgSwitch").addEventListener("change", pushSettings);
$("tgLanguage").addEventListener("change", pushSettings);
$("playerLanguage").addEventListener("change", pushSettings);
$("tgChat").addEventListener("change", pushSettings);
$("tgToken").addEventListener("change", pushSettings);
["tgOps", "tgBackup", "tgUpdate", "tgProblems"].forEach((id) => $(id).addEventListener("change", pushSettings));
/* «Найти чаты бота»: getUpdates показывает, где бот реально состоит,
   и подставляет настоящий chat id вместо копипасты с ошибками */
$("btnTgChats").addEventListener("click", async () => {
  const btn = $("btnTgChats");
  const body = $("tgChatsBody");
  try {
    S.telegramChatsPending = true;
    btn.disabled = true;
    if (!await pushSettings()) return; // токен мог быть введён только что
    const res = await api("/api/telegram-chats");
    if (res.error) throw new Error(res.error);
    const chats = res.chats || [];
    body.hidden = false;
    if (!chats.length) {
      body.innerHTML = I18n.msg`<p class="hint">Пока не нашёл ни одного чата: напишите что-нибудь в нужный чат (в группе — любое сообщение боту) и нажмите кнопку ещё раз. Обновления Telegram хранит сутки.</p>`;
      return;
    }
    body.innerHTML = chats.map((c) => I18n.msg`
      <button type="button" class="chat-chip" data-id="${esc(c.id)}" title="Подставить в Chat ID">
        <span>${esc(c.title)}</span><span class="chat-id mono">${esc(c.id)}</span>
      </button>`).join("");
    body.querySelectorAll(".chat-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        $("tgChat").value = chip.dataset.id;
        pushSettings();
      });
    });
    updateButtons();
  } catch (e) {
    toast(e.message || String(e), "error");
  } finally {
    S.telegramChatsPending = false;
    updateButtons();
  }
});

$("btnTgTest").addEventListener("click", async () => {
  const btn = $("btnTgTest");
  btn.disabled = true;
  S.telegramTestPending = true;
  try {
    // если токен/chat ввели и сразу нажали «Проверить» — сначала дожимаем сохранение,
    // иначе проверка уйдёт со старыми настройками и скажет «не задан токен»
    if (!await pushSettings()) return;
    const res = await api("/api/notify-test", { method: "POST", body: {} });
    if (res.error) throw new Error(res.error);
    toast(I18n.t("Отправлено — проверьте чат Telegram"), "ok");
  } catch (e) {
    toast(e.message || String(e), "error");
  } finally {
    S.telegramTestPending = false;
    updateButtons();
  }
});

/* ───────────────────────── опрос ───────────────────────── */

let connFailStreak = 0;
const CONNECTION_RETRY_DELAY = 10000;
const connectionAlert = FloatingAlerts.create({
  id: "connBanner", className: "connection-alert", actionId: "connRetry",
  icon: '<span class="retry-timer" aria-hidden="true"><svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2"><circle class="retry-track" cx="16" cy="16" r="14"/><circle class="retry-progress" cx="16" cy="16" r="14" pathLength="100"/></svg><span class="retry-seconds"></span></span>',
});
let connectionAttempt = null;
let connectionRetryAt = 0;
let connectionTimer = null;

function renderConnectionWarning() {
  const busy = !!connectionAttempt;
  setDomAttribute(connectionAlert.element, "aria-label", I18n.t("Соединение с пультом"));
  setDomAttribute(connectionAlert.element, "aria-busy", busy);
  setDomProperty(connectionAlert.message, "textContent", I18n.t(busy ? "Проверяем соединение…" : "Нет связи с пультом"));
  setDomProperty(connectionAlert.label, "textContent", "Retry");
  setDomProperty(connectionAlert.action, "disabled", busy);
  setDomAttribute(connectionAlert.action, "aria-label", I18n.t(busy ? "Проверяем соединение…" : "Повторить подключение"));
  const remaining = Math.max(0, connectionRetryAt - Date.now());
  const seconds = connectionAlert.action.querySelector(".retry-seconds");
  setDomProperty(seconds, "textContent", busy ? "" : String(Math.ceil(remaining / 1000)));
  setDomAttribute(connectionAlert.action, "aria-description", busy ? "" : I18n.msg`Повторное подключение через ${Math.ceil(remaining / 1000)} с`);
  connectionAlert.element.style.setProperty("--retry-progress", String(1 - remaining / CONNECTION_RETRY_DELAY));
}

function clearConnectionRetry() {
  clearInterval(connectionTimer);
  connectionTimer = null;
  connectionRetryAt = 0;
}

function markConnectionHealthy() {
  connFailStreak = 0;
  S.lastDataOk = Date.now();
  connectionAttempt?.abort();
  connectionAttempt = null;
  clearConnectionRetry();
  connectionAlert.element.hidden = true;
}

function scheduleConnectionRetry() {
  clearConnectionRetry();
  connectionRetryAt = Date.now() + CONNECTION_RETRY_DELAY;
  renderConnectionWarning();
  connectionTimer = setInterval(() => {
    if (document.hidden) return;
    renderConnectionWarning();
    if (Date.now() >= connectionRetryAt) retryConnection();
  }, 100);
}

async function retryConnection() {
  if (S.demo || document.hidden || connectionAttempt) return;
  clearConnectionRetry();
  const attempt = new AbortController();
  connectionAttempt = attempt;
  renderConnectionWarning();
  try {
    // A fresh authenticated snapshot proves recovery; health alone is not enough.
    const overview = await api("/api/overview", { timeout: 5000, signal: attempt.signal });
    if (connectionAttempt !== attempt || document.hidden) return;
    if (!overview.ok || overview.error) throw new Error("Console unavailable");
    connectionAttempt = null;
    applyOverview(overview);
    if (!pollingStarted && !document.body.classList.contains("is-booting")) {
      liveSource?.close();
      liveSource = null;
      clearTimeout(sseStartupTimer);
      startSse();
    }
  } catch {
    if (connectionAttempt !== attempt || document.hidden) return;
    connectionAttempt = null;
    connectionAlert.element.hidden = false;
    scheduleConnectionRetry();
  }
}
connectionAlert.action.addEventListener("click", retryConnection);
window.addEventListener("online", retryConnection);
document.addEventListener("pz:language-changed", renderConnectionWarning);

function markConnFail() {
  connFailStreak++;
  updateConnectionWarning();
}

function applyOverview(o) {
  if (o.error && !o.serverName) { markConnFail(); return; }
  markConnectionHealthy();
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
    if (since === S.logsSince && until === S.logsUntil) applyLogs({ ok: false, error: I18n.t("Не удалось получить логи. Повторное подключение идёт автоматически.") });
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

/* Try recovery silently before exposing stale data as a connection notice. */
function updateConnectionWarning() {
  if (S.demo || document.hidden || connectionAttempt || !connectionAlert.element.hidden) return;
  const stale = S.lastDataOk && Date.now() - S.lastDataOk >= 15000;
  if (stale || connFailStreak >= 2) retryConnection();
}

let pollingStarted = false;
let liveSource = null;
let sseStartupTimer = null;
let logsPollingStarted = false;
let logsPollRunning = false;

async function pollVisibleLogs() {
  if (!logsPollingStarted || document.hidden || activeView !== "console" || logsPollRunning) return;
  logsPollRunning = true;
  try { await refreshLogs(); } finally { logsPollRunning = false; }
}

function startLogsPolling(initial = true) {
  if (logsPollingStarted) return;
  logsPollingStarted = true;
  setInterval(pollVisibleLogs, 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollVisibleLogs(); });
  if (initial) pollVisibleLogs();
}

function startPolling(initial = true) {
  if (pollingStarted) return;
  pollingStarted = true;
  startLogsPolling(initial);
  for (const [refresh, interval] of [
    [refreshOverview, 3000], [refreshPlayers, 5000], [refreshStats, 5000],
    [refreshBackups, 10000], [refreshEvents, 12000],
    [refreshOps, 1500], [refreshPlayersHistory, 60000], [refreshStatsHistory, 30000],
    [refreshMods, 60000],
  ]) {
    let running = false;
    const poll = async () => {
      if (document.hidden || running || (refresh === refreshOverview && (connectionAttempt || !connectionAlert.element.hidden))) return;
      running = true;
      try { await refresh(); } finally { running = false; }
    };
    if (initial) poll();
    setInterval(poll, interval);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  }
}

$("playersBody").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-p]");
  if (button && !button.disabled) confirmPlayerAction(button.dataset.p, button.dataset.name);
});

/* ───────────────────────── запуск ───────────────────────── */

// Keep fixed navigation, draft controls and notifications clear of each other
// when text wraps, the viewport changes or a device has a bottom safe area.
const layoutObserver = new ResizeObserver(() => {
  for (const [selector, variable] of [[".topbar", "--header-height"], [".nav", "--nav-height"], ["#draftBar", "--draft-height"], [".site-footer", "--footer-height"]]) {
    const height = document.querySelector(selector)?.getBoundingClientRect().height || 0;
    document.documentElement.style.setProperty(variable, `${Math.ceil(height)}px`);
  }
  const focused = document.activeElement, draftBar = $("draftBar");
  if (!draftBar.hidden && focused?.matches("#configFields [data-key], #mapList, #iniSource, #sandboxSource")) {
    const focusArea = focused.closest("#mapEdit") || focused;
    const overlap = focusArea.getBoundingClientRect().bottom - draftBar.getBoundingClientRect().top + 12;
    if (overlap > 0) window.scrollBy({ top: overlap, behavior: "instant" });
  }
});
for (const selector of [".topbar", ".nav", "#draftBar", ".site-footer"]) layoutObserver.observe(document.querySelector(selector));
const mobileLayout = matchMedia("(max-width: 740px)");
const tabletLayout = matchMedia("(max-width: 1180px)");
// Keep independent preferences so opening the desktop sidebar does not expand
// it on a tablet. Without a preference, tablets start with an icon rail.
function sidebarMode() { return tabletLayout.matches ? "tablet" : "desktop"; }
function renderSidebar(collapsed) {
  document.documentElement.dataset.sidebar = collapsed ? "collapsed" : "expanded";
  const toggle = $("navToggle");
  const label = I18n.t(collapsed ? "Развернуть навигацию" : "Свернуть навигацию");
  toggle.setAttribute("aria-expanded", String(!collapsed));
  toggle.setAttribute("aria-label", label);
  toggle.title = label;
  toggle.querySelector("span").textContent = I18n.t(collapsed ? "Развернуть" : "Свернуть");
  for (const link of document.querySelectorAll(".nav a")) {
    link.title = link.querySelector("span").textContent;
  }
}
const sidebarPreferences = {};
for (const mode of ["desktop", "tablet"]) {
  try { sidebarPreferences[mode] = localStorage.getItem(`pz-sidebar-${mode}`); }
  catch { /* Navigation still works when browser storage is unavailable. */ }
}
function adaptSidebar() {
  const preference = sidebarPreferences[sidebarMode()];
  renderSidebar(preference === "collapsed" || (preference !== "expanded" && tabletLayout.matches));
}
$("navToggle").addEventListener("click", () => {
  const collapsed = document.documentElement.dataset.sidebar !== "collapsed";
  const mode = sidebarMode(), preference = collapsed ? "collapsed" : "expanded";
  sidebarPreferences[mode] = preference;
  try { localStorage.setItem(`pz-sidebar-${mode}`, preference); }
  catch { /* Preserve the preference in memory for this visit. */ }
  renderSidebar(collapsed);
});
tabletLayout.addEventListener("change", adaptSidebar);
adaptSidebar();
function adaptConfigFlow() {
  const editing = /^(settings|mods)$/.test(location.hash.replace(/^#\/?/, ""));
  const main = document.querySelector(".main"), flow = $("configFlow");
  if (editing) {
    const card = $("view-" + location.hash.replace(/^#\/?/, "")).querySelector(".editor-card");
    card.querySelector(".editor-tabs").before(flow);
  } else main.querySelector(".view").before(flow);
}
adaptConfigFlow();
mobileLayout.addEventListener("change", adaptConfigFlow);

/* ───────────────────── роутер страниц ───────────────────── */

const VIEWS = {
  overview: I18n.t("Обзор"),
  players: I18n.t("Игроки"),
  mods: I18n.t("Моды"),
  settings: I18n.t("Настройки сервера"),
  maintenance: I18n.t("Обслуживание"),
  backups: I18n.t("Бэкапы"),
  events: I18n.t("События"),
  console: I18n.t("Консоль"),
};
$("sec-status").after($("editorAttention"));
for (const [route, label] of Object.entries(VIEWS)) {
  const view = $("view-" + route), heading = document.createElement("header");
  heading.className = "page-heading";
  heading.innerHTML = `<h1 id="page-${route}">${esc(label)}</h1>` + (route === "overview" ? I18n.html('<div class="page-actions"><a id="overviewDraft" class="btn" href="#/settings" hidden>Черновик</a></div>') : "");
  view.prepend(heading);
  view.setAttribute("aria-labelledby", "page-" + route);
}
$("view-settings").querySelector(".page-heading").append($("configVerifyHelp"));
const modSettingsLink = document.createElement("a");
modSettingsLink.className = "btn";
modSettingsLink.href = "#/settings";
modSettingsLink.textContent = I18n.t("Настройки модов");
modSettingsLink.addEventListener("click", async event => {
  event.preventDefault();
  try {
    await window.ConfigEditor.focusSettings("custom");
    location.hash = "#/settings";
  } catch (error) { toast(error.message, "error"); }
});
$("view-mods").querySelector(".page-heading").append(modSettingsLink);
// Primary editing routes share the same DOM, visual and keyboard order.
const primaryNav = document.querySelector(".nav");
for (const route of ["overview", "settings", "mods", "players", "maintenance", "backups", "events", "console"]) {
  primaryNav.insertBefore(primaryNav.querySelector(`[data-route="${route}"]`), $("navMore"));
}

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
  if (!["overview", "settings", "mods"].includes(r)) $("navMore").setAttribute("aria-current", "page");
  else $("navMore").removeAttribute("aria-current");
  document.title = I18n.msg`${VIEWS[r]} · PZ Пульт`;
  adaptConfigFlow();
  window.scrollTo(0, 0);
  const view = $("view-" + r);
  if (view && !document.body.classList.contains("is-booting")) view.focus({ preventScroll: true });
  if (r === "mods" && modsPending) renderModsFiltered();
  window.ConfigEditor?.route(r);
  pollVisibleLogs();
}

window.addEventListener("hashchange", applyRoute);

/* Fast access uses the existing routes, editors and confirmations. */
const commands = (() => {
  const dialog = $("commandDialog"), search = $("commandSearch"), results = $("commandResults");
  let matches = [], opener = null;
  const navigate = async (route) => {
    if (currentRoute() === route) return;
    await new Promise(resolve => {
      window.addEventListener("hashchange", resolve, { once: true });
      location.hash = "#/" + route;
    });
  };
  const settings = tab => async () => { await navigate("settings"); await window.ConfigEditor.focusSettings(tab); };
  const confirmedAction = id => async () => {
    await navigate("overview");
    const button = $(id);
    if (button.disabled) { toast(I18n.t("Действие сейчас недоступно. Проверьте состояние сервера."), "error"); button.closest("section").focus(); return; }
    button.focus(); button.click();
  };
  const entries = () => [
    ...Object.entries(VIEWS).map(([route, label]) => ({ label: I18n.t("Открыть: ") + label, hint: I18n.t("Раздел пульта"), run: () => navigate(route) })),
    { label: I18n.t("Найти настройку сервера"), hint: I18n.t("По названию или техническому ключу"), reason: !window.ConfigEditor?.file ? I18n.t("Выберите доступный профиль") : "", run: settings("server") },
    { label: I18n.t("Найти настройку мира"), hint: I18n.t("Параметры Sandbox"), reason: !window.ConfigEditor?.file ? I18n.t("Выберите доступный профиль") : "", run: settings("world") },
    { label: I18n.t("Найти мод в порядке загрузки"), hint: I18n.t("ModID или название; без изменения порядка"), reason: !window.ConfigEditor?.file ? I18n.t("Выберите доступный профиль") : "", run: async () => { await navigate("mods"); await window.ConfigEditor.focusModOrder(); } },
    { label: I18n.t("Найти строку в логах"), hint: I18n.t("Перейти к фильтру логов"), reason: $("logsFilter").disabled ? I18n.t("Логи контейнера недоступны в demo и remote") : "", run: async () => { await navigate("console"); $("logsFilter").focus(); } },
    { label: I18n.t("Посмотреть текущую операцию"), hint: I18n.t("Фаза, время и переход к логам"), reason: !S.op?.active ? I18n.t("Сейчас нет активной операции") : "", run: () => { $("opbar").tabIndex = -1; $("opbar").focus(); } },
    { label: I18n.t("Скопировать имя профиля"), hint: I18n.t("Имя файла конфигурации"), reason: !window.ConfigEditor?.file ? I18n.t("Выберите доступный профиль") : "", run: () => copyText(window.ConfigEditor.file) },
    { label: I18n.t("Скопировать digest образа"), hint: I18n.t("Полный идентификатор Docker"), reason: !$("updLocalCopy").dataset.copy ? I18n.t("Идентификатор ещё не получен") : "", run: () => copyText($("updLocalCopy").dataset.copy, $("updLocalCopy")) },
    { label: I18n.t("Остановить сервер…"), hint: I18n.t("Открыть подтверждение с предупреждением игроков"), reason: $("btnStop").disabled ? I18n.t("Недоступно при операции, в demo/remote или без работающего сервера") : "", run: confirmedAction("btnStop") },
    { label: I18n.t("Перезапустить сервер…"), hint: I18n.t("Открыть подтверждение с предупреждением игроков"), reason: $("btnRestart").disabled ? I18n.t("Недоступно при операции, в demo/remote или без работающего сервера") : "", run: confirmedAction("btnRestart") },
    { label: I18n.t("Восстановить мир из бэкапа…"), hint: I18n.t("Открыть список архивов и выбрать версию для подтверждения"), run: () => navigate("backups") },
  ];
  function render() {
    const query = search.value.trim().toLocaleLowerCase(I18n.locale);
    matches = entries().filter(item => (item.label + " " + item.hint).toLocaleLowerCase(I18n.locale).includes(query));
    results.innerHTML = matches.map((item, index) => `<button type="button" class="command-item" data-command="${index}" ${item.reason ? "disabled" : ""}><strong>${esc(item.label)}</strong><span>${esc(item.reason || item.hint)}</span></button>`).join("") || I18n.html('<div class="command-empty"><p class="hint">Команда не найдена. Попробуйте название раздела, «настройку», «мод» или «логи».</p><button type="button" class="btn" data-command-clear>Сбросить поиск</button></div>');
    results.scrollTop = 0;
    const available = matches.filter(item => !item.reason).length;
    $("commandCount").textContent = matches.length ? I18n.msg`Найдено: ${matches.length} · Доступно: ${available}` : I18n.t("Команда не найдена");
  }
  function close(restore = true) {
    dialog.close();
    if (restore && opener?.isConnected) opener.focus({ preventScroll: true });
  }
  async function execute(index) {
    const chosen = matches[index];
    if (!chosen || chosen.reason) return;
    const item = entries().find(entry => entry.label === chosen.label);
    if (!item || item.reason) { render(); search.focus(); if (item?.reason) toast(item.reason, "error"); return; }
    close(false);
    try { await item.run(); } catch (error) { toast(error.message, "error"); }
  }
  function open() {
    if (document.body.classList.contains("is-booting") || !$("modalRoot").hidden || dialog.open) return;
    opener = document.activeElement;
    search.value = ""; render(); dialog.showModal(); search.focus(); results.scrollTop = 0;
  }
  $("btnCommands").addEventListener("click", open);
  $("commandClose").addEventListener("click", () => close());
  dialog.addEventListener("cancel", event => { event.preventDefault(); close(); });
  dialog.addEventListener("click", event => { if (event.target === dialog && !event.target.closest(".command-head, input, .command-results, #commandHint")) { const r = dialog.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) close(); } });
  search.addEventListener("input", render);
  results.addEventListener("click", event => {
    if (event.target.closest("[data-command-clear]")) { search.value = ""; render(); search.focus(); return; }
    const button = event.target.closest("[data-command]");
    if (button) execute(Number(button.dataset.command));
  });
  dialog.addEventListener("keydown", event => {
    if (event.isComposing || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    const buttons = [...results.querySelectorAll("[data-command]:not(:disabled)")];
    if (event.key === "Enter" && event.target === search) { event.preventDefault(); if (buttons.length) execute(Number(buttons[0].dataset.command)); }
    if (!["ArrowDown", "ArrowUp"].includes(event.key) || !buttons.length || event.target === $("commandClose")) return;
    event.preventDefault();
    const index = buttons.indexOf(document.activeElement);
    if (event.target === search) { buttons[event.key === "ArrowDown" ? 0 : buttons.length - 1].focus(); return; }
    if (event.key === "ArrowUp" && index <= 0) search.focus();
    else buttons[(index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length].focus();
  });
  document.addEventListener("keydown", event => {
    if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey || event.key.toLowerCase() !== "k" || event.repeat || event.isComposing) return;
    if (event.target.closest("input, textarea, select, [contenteditable]:not([contenteditable=false]), dialog[open]") || !$("modalRoot").hidden) return;
    event.preventDefault(); open();
  });
  return { open };
})();

/* ───────────────────── SSE: живой поток данных ───────────────────── */

function startSse() {
  if (document.hidden || liveSource || pollingStarted) return;
  if (typeof EventSource === "undefined") { startPolling(); return; }
  startLogsPolling();
  const es = new EventSource("/api/stream?logs=0");
  liveSource = es;
  es.addEventListener("auth-expired", () => { es.close(); liveSource = null; clearTimeout(sseStartupTimer); requireLogin(); });
  let messages = 0;
  let errors = 0;
  let fellBack = false;
  const fallback = () => {
    if (fellBack || liveSource !== es) return;
    fellBack = true;
    es.close();
    liveSource = null;
    clearTimeout(sseStartupTimer);
    startPolling();
  };
  const bind = (name, apply) => es.addEventListener(name, (e) => {
    if (liveSource !== es || document.hidden) return;
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    if (!data || typeof data !== "object") return;
    if (name === "overview" && (data.ok === false || (data.error && !data.serverName))) { markConnFail(); return; }
    messages++;
    errors = 0;
    markConnectionHealthy();
    try { apply(data); } catch (err) { /* битый кадр пропускаем */ }
  });
  bind("overview", applyOverview);
  bind("players", applyPlayers);
  bind("stats", applyStats);
  bind("backups", applyBackups);
  bind("events", applyEvents);
  bind("ops", applyOps);
  bind("stats-history", (d) => renderStatsHistory(d.points || []));
  bind("players-history", (d) => renderPlayersHistory(d.points || []));
  bind("mods", applyMods);
  // браузер сам переподключается; откат на опрос — если поток так и не ожил
  // или умер уже после того, как работал
  es.onerror = () => {
    if (document.hidden || liveSource !== es) return;
    api("/api/auth/session").catch(() => {});
    errors++;
    updateConnectionWarning();
    if (errors >= 3 && Date.now() - S.lastDataOk > 20000) fallback();
  };
  sseStartupTimer = setTimeout(() => { if (messages === 0) fallback(); }, 9000);
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    connectionAttempt?.abort();
    connectionAttempt = null;
    clearConnectionRetry();
    connectionAlert.element.hidden = true;
    liveSource?.close();
    liveSource = null;
    clearTimeout(sseStartupTimer);
  } else if (!S.demo) {
    retryConnection();
  }
});

async function boot() {
  const slowTimer = setTimeout(() => { $("startupSlow").hidden = false; }, 15000);
  $("startupRetry").addEventListener("click", () => location.reload());
  setInterval(() => {
    if (document.hidden) return;
    updateConnectionWarning();
    updateOperationElapsed();
  }, 5000);
  consoleBootLine = consoleAppend(I18n.t("Пульт подключается к серверу…"), "c-dim");
  applyRoute();
  let liveReady = false;
  if (location.protocol === "file:" || new URLSearchParams(location.search).get("demo") === "1") {
    enterDemo();
  } else {
    try {
      const h = await api("/api/health", { timeout: 3500 });
      if (!h.ok) throw new Error("no health");
      liveReady = true;
    } catch (e) {
      // Complete the initial attempts before starting recurring recovery.
    }
  }
  // The initial snapshot cannot depend on the timing or completeness of SSE.
  // Load the visible route and shared state together; later updates stay live.
  const routeLoads = {
    overview: [refreshPlayers, refreshStats, refreshBackups, refreshEvents, refreshPlayersHistory, refreshStatsHistory],
    players: [refreshPlayers, refreshPlayersHistory],
    backups: [refreshBackups],
    events: [refreshEvents],
    console: [refreshLogs],
  };
  await Promise.allSettled([
    liveReady || S.demo ? refreshOverview() : retryConnection(), refreshOps(),
    ...(routeLoads[activeView] || []).map(load => load()),
    window.ConfigEditor?.ready,
  ]);
  clearTimeout(slowTimer);
  $("startupLoader").hidden = true;
  $("startupLoader").setAttribute("aria-busy", "false");
  $("dashboardShell").inert = false;
  document.body.classList.remove("is-booting");
  updateButtons();
  $("view-" + activeView)?.focus({ preventScroll: true });
  if (liveReady && typeof EventSource !== "undefined") startSse();
  else startPolling(false);
}

function enterDemo() {
  S.demo = true;
  $("demoBadge").hidden = false;
  $("demoBanner").hidden = false;
  if (consoleBootLine) {
    consoleBootLine.textContent = I18n.t("Консоль недоступна — это демо-предпросмотр.");
    consoleBootLine = null;
  }
  consoleAppend(I18n.t("Демо-режим: данные вымышленные, операции отключены."), "c-dim");
}

// All synchronous scripts, including the profile editor, must publish readiness.
window.addEventListener("DOMContentLoaded", boot, { once: true });
