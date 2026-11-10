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
const eventFullFmt = new Intl.DateTimeFormat(I18n.locale, { dateStyle: "long", timeStyle: "medium" });
const eventDayFmt = new Intl.DateTimeFormat(I18n.locale, { day: "numeric", month: "long" });
const eventYearDayFmt = new Intl.DateTimeFormat(I18n.locale, { day: "numeric", month: "long", year: "numeric" });
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
  const kindTitles = () => ({ ok: I18n.t("Готово"), error: I18n.t("Ошибка"), warning: I18n.t("Предупреждение"), pending: I18n.t("Выполняется"), info: I18n.t("Информация") });
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
    const active = document.activeElement;
    const focusedRow = active?.closest(".notification-item");
    const focusedId = focusedRow?.dataset.id;
    const focusedHref = active?.closest("a")?.getAttribute("href");
    const scrollTop = list.scrollTop;
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
    list.scrollTop = scrollTop;
    if (focusedId && list.contains(focusedRow) === false) {
      const row = [...list.children].find(node => node.dataset.id === focusedId);
      const replacement = focusedHref ? [...(row?.querySelectorAll("a") || [])].find(link => link.getAttribute("href") === focusedHref) : row?.querySelector(".notification-read");
      (replacement || $("notificationClose")).focus({ preventScroll: true });
    }
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
    render();
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
    publish({ id = `local:${Date.now()}:${Math.random().toString(36).slice(2)}`, title = "", message = "", kind = "info", operation = false, createdAt = Date.now(), popup = true, duration } = {}) {
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

function toast(text, kind = "info", ms) {
  return Notifications.publish({ message: text, kind, duration: ms });
}

const showToast = (() => {
  const root = $("toasts");
  const entries = [];
  const exits = new Map();
  let focused = document.hasFocus(), hovered = false, keyboard = false, expandedByTouch = false;
  let returnFocus = null, movingFocus = false;
  let layoutKey = "";
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

  const snapshot = () => {
    const active = document.activeElement, bounds = root.getBoundingClientRect();
    const focus = root.contains(active) ? active.getBoundingClientRect() : null;
    return {
      boxes: new Map(entries.map(entry => [entry, {
        ...entry.box.getBoundingClientRect().toJSON(), opacity: Number(getComputedStyle(entry.box).opacity),
      }])),
      focus: focus && focus.top >= bounds.top && focus.bottom <= bounds.bottom ? active : null,
    };
  };

  function exit(box, rect) {
    const bounds = root.getBoundingClientRect();
    if (reducedMotion.matches || getComputedStyle(box).visibility === "hidden" ||
        (root.dataset.expanded !== "true" && box.dataset.front !== "true") ||
        (root.dataset.expanded === "true" && (rect.top < bounds.top - 1 || rect.bottom > bounds.bottom + 1)) ||
        rect.bottom <= 0 || rect.top >= innerHeight) { box.remove(); return; }
    // Leave the live region and hit testing immediately, keeping only the visual exit.
    box.className = "toast-exit";
    box.inert = true;
    box.setAttribute("aria-hidden", "true");
    Object.assign(box.style, { position: "fixed", left: `${rect.left}px`, top: `${rect.top}px`,
      width: `${rect.width}px`, height: `${rect.height}px`, transform: "none", zIndex: "80" });
    document.body.append(box);
    const motion = box.animate([{ opacity: rect.opacity, translate: "0 0" }, { opacity: 0, translate: "0 6px" }],
      { duration: 130, easing: "ease-out" });
    exits.set(box, motion);
    const remove = () => { exits.delete(box); box.remove(); };
    motion.finished.then(remove, remove);
  }

  function pause(entry) {
    if (entry.startedAt === null) return;
    entry.remaining = Math.max(0, entry.remaining - (performance.now() - entry.startedAt));
    clearTimeout(entry.timer);
    entry.startedAt = null;
  }

  function dismiss(entry, read = false) {
    const index = entries.indexOf(entry);
    if (index < 0) return;
    const previous = snapshot();
    const hadFocus = entry.box.contains(document.activeElement);
    pause(entry);
    entries.splice(index, 1);
    entry.motion?.cancel();
    exit(entry.box, previous.boxes.get(entry));
    resize.unobserve(entry.box);
    if (!entries.length) { expandedByTouch = false; hovered = false; }
    if (hadFocus) {
      movingFocus = true;
      const next = entries[Math.min(index, entries.length - 1)];
      if (next) next.close.focus({ preventScroll: true });
      else if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
      movingFocus = false;
    }
    sync(previous);
    if (read) Notifications.markRead([entry.id]);
  }

  function sync(previous, revealNewest = false) {
    keyboard = root.contains(document.activeElement);
    const expanded = hovered || keyboard || expandedByTouch;
    const front = entries.at(-1);
    const key = () => `${expanded}:${root.clientWidth}:${entries.map(entry => `${entry.id}:${entry.box.offsetHeight}`).join("|")}`;
    if (layoutKey !== key()) {
      previous ||= snapshot();
      const opening = expanded && root.dataset.expanded !== "true";
      entries.forEach(entry => entry.motion?.cancel());
      root.dataset.expanded = String(expanded);
      if (front) root.style.setProperty("--toast-height", `${front.box.offsetHeight}px`);
      entries.forEach((entry, index) => {
        const depth = entries.length - index - 1;
        entry.box.style.setProperty("--toast-depth", Math.min(depth, 2));
        entry.box.style.zIndex = index + 1;
        entry.box.dataset.front = String(entry === front);
        entry.box.dataset.hidden = String(!expanded && depth > 2);
        entry.box.inert = !expanded && entry !== front;
        entry.box.style.transform = "none";
      });
      // Reveal the newest end of an overflowing list, keeping the front toast anchored.
      if (opening || (expanded && revealNewest)) root.scrollTop = root.scrollHeight;
      // A new arrival may follow the end only as far as the reader's focused control allows.
      // Keep both endpoints inside the clip so FLIP also keeps focus visible during motion.
      if (expanded && previous.focus === document.activeElement && root.contains(previous.focus)) {
        const bounds = root.getBoundingClientRect(), control = previous.focus.getBoundingClientRect();
        if (control.top < bounds.top + 4) root.scrollTop -= bounds.top + 4 - control.top;
        else if (control.bottom > bounds.bottom - 4) root.scrollTop += control.bottom - bounds.bottom + 4;
      }
      const targets = entries.map(entry => entry.box.getBoundingClientRect());
      entries.forEach(entry => entry.box.style.removeProperty("transform"));
      const transforms = entries.map(entry => getComputedStyle(entry.box).transform);
      if (!reducedMotion.matches) entries.forEach((entry, index) => {
        if (entry.box.dataset.hidden === "true") return;
        const before = previous.boxes.get(entry), target = targets[index];
        const transform = transforms[index];
        let frames;
        if (before) {
          // FLIP from the rendered position, including an interrupted transition.
          const x = before.x + before.width / 2 - target.x - target.width / 2;
          const y = before.bottom - target.bottom;
          const start = `translate(${x}px, ${y}px) scale(${before.width / target.width}, ${before.height / target.height})`;
          frames = [{ transform: start, opacity: before.opacity }, { transform, opacity: 1 }];
        } else frames = [{ transform, opacity: 0, translate: "0 6px" }, { transform, opacity: 1, translate: "0 0" }];
        entry.motion = entry.box.animate(frames, { duration: before ? 240 : 180, easing: "cubic-bezier(0.2, 0, 0, 1)" });
      });
      layoutKey = key();
    }
    entries.forEach(entry => {
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
    if (movingFocus) return;
    if (event.relatedTarget && !root.contains(event.relatedTarget)) returnFocus = event.relatedTarget;
    sync();
  });
  root.addEventListener("focusout", () => queueMicrotask(sync));
  root.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    expandedByTouch = false;
    hovered = false;
    if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
    else document.activeElement.blur();
    sync();
  });
  document.addEventListener("pointerdown", event => {
    if (!root.contains(event.target)) {
      expandedByTouch = false;
      sync();
    } else if (event.pointerType === "touch" && !event.target.closest("button, a") && entries.length > 1) {
      expandedByTouch = !expandedByTouch;
      sync();
    }
  });
  const resize = new ResizeObserver(() => sync());
  resize.observe(root);
  reducedMotion.addEventListener("change", () => {
    if (reducedMotion.matches) {
      entries.forEach(entry => entry.motion?.cancel());
      exits.forEach((motion, box) => { motion.cancel(); box.remove(); });
      exits.clear();
    }
  });

  const show = (notification, ms) => {
    const { id, title, message: text, kind, operation } = notification;
    // Longer feedback earns reading time; callers can still set an exact duration.
    const readingTime = Math.min(20000, Math.max(5200, 2000 + (title.length + text.length) * 55));
    const duration = Number.isFinite(ms) && ms >= 0 ? ms : readingTime;
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
    close.setAttribute("aria-label", I18n.msg`Закрыть уведомление: ${title || text}`);
    close.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>';
    box.append(icon, message, close);
    const entry = { id, box, close, remaining: duration, startedAt: null, timer: null };
    close.addEventListener("click", () => dismiss(entry, true));
    const previous = snapshot();
    const atEnd = root.scrollTop + root.clientHeight >= root.scrollHeight - 1;
    entries.push(entry);
    root.appendChild(box);
    sync(previous, atEnd);
    resize.observe(box);
  };
  show.dismissIds = ids => entries.slice().forEach(entry => { if (ids.has(entry.id)) dismiss(entry); });
  show.clear = () => entries.slice().forEach(entry => dismiss(entry));
  return show;
})();

/* ───────────────────────── модальное окно ───────────────────────── */

const modal = (() => {
  const root = $("modalRoot");
  let onOk = null;
  let onSecondary = null;
  let returnFocus = null;
  let submitting = false;

  function open({ title, bodyHTML, okLabel = I18n.t("Подтвердить"), danger = false, onConfirm, secondaryAction }) {
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
    const cancelBtn = $("modalCancel");
    cancelBtn.hidden = secondaryAction === null;
    cancelBtn.textContent = secondaryAction?.label || I18n.t("Отмена");
    cancelBtn.className = "btn" + (secondaryAction?.danger ? " danger" : "");
    onSecondary = secondaryAction?.onClick || null;
    onOk = onConfirm || null;
    root.hidden = false;
    (danger && !cancelBtn.hidden ? cancelBtn : okBtn).focus();
  }

  function close() {
    if (submitting) return;
    root.hidden = true;
    $("modalBody").innerHTML = "";
    onOk = null;
    onSecondary = null;
    if (returnFocus?.isConnected && !returnFocus.disabled) returnFocus.focus({ preventScroll: true });
    else if (document.activeElement === document.body || root.contains(document.activeElement)) document.querySelector(`#view-${activeView}`)?.focus({ preventScroll: true });
    returnFocus = null;
  }

  $("modalCancel").addEventListener("click", () => {
    if (submitting) return;
    const action = onSecondary;
    close();
    action?.();
  });
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
    $("modalError").textContent = e.outcomeUnknown ? e.message : I18n.msg`${e.message || e} Параметры сохранены — повторите действие.`;
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
  rconPending: false,
  operationUncertain: false,
  pendingRequest: null,
  pendingOperation: null,
  settingsVersion: null,
  serverSettings: null,
  logsLevel: "all",
  logsLines: [],
  logsUpdatedAt: 0,
  logsSince: 0,
  logsUntil: 0,
  logsProfile: "",
  logsRefreshing: false,
  logsManualPending: 0,
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
let updateGuardBusy = false;
let opsGeneration = 0;
let opsRefreshSequence = 0;
let opsAppliedRefresh = 0;
let opsEpochRefresh = null;
let opsSnapshot = null;
const retiredOpsEpochs = new Set();
const pendingCancellations = new Set();
let operationRecoveryTimer = null;

function operationIdentity(active) {
  return active?.id || (active ? `${active.op}:${active.startedAt || ""}` : "");
}

function validOperationState(op) {
  return !!op && !op.error && op.ok !== false && Array.isArray(op.history)
    && Object.hasOwn(op, "active") && (op.active === null || !!op.active && typeof op.active.op === "string" && !!op.active.op)
    && (!Object.hasOwn(op, "snapshot") || !!op.snapshot && typeof op.snapshot.epoch === "string" && !!op.snapshot.epoch
      && Number.isSafeInteger(op.snapshot.sequence) && op.snapshot.sequence >= 0);
}

function recoverOperationState() {
  clearTimeout(operationRecoveryTimer);
  if (!S.operationUncertain) return;
  operationRecoveryTimer = setTimeout(async () => {
    if (!document.hidden) await refreshOps();
    recoverOperationState();
  }, 1500);
}

function clearPendingOperation() {
  S.operationUncertain = false;
  S.actionPending = false;
  S.pendingOperation = null;
  S.pendingRequest = null;
  clearTimeout(operationRecoveryTimer);
}

function operationBusy() {
  return !!S.op?.active || S.actionPending;
}

function operationAvailabilityReason() {
  return S.demo ? I18n.t("Демо: операции отключены. Откройте пульт своего сервера для управления.")
    : S.op?.active ? I18n.t("Идёт операция: ") + (OP_TITLES[S.op.active.op] || S.op.active.op) + I18n.t(". Дождитесь результата; прогресс показан над разделом.")
    : S.actionPending ? I18n.t("Запрос отправляется. Дождитесь принятия или сообщения об ошибке.")
    : S.rconPending ? I18n.t("Ожидается ответ RCON. Дождитесь результата команды.")
    : "";
}

async function api(path, opts = {}) {
  // Recheck when sending: a dialog or queued edit may predate an SSE lock.
  const changing = opts.method && opts.method !== "GET";
  const inspection = path === "/api/config-validate" && !opts.body?.prepare
    || path === "/api/modpack" && !opts.body?.pack;
  if (changing && S.rconPending && path !== "/api/rcon" && !path.startsWith("/api/auth/") && !inspection) {
    throw new Error(operationAvailabilityReason());
  }
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
    const parsed = await res.json().catch(() => ({}));
    const data = parsed && typeof parsed === "object" ? parsed : {};
    Object.defineProperty(data, "httpStatus", { value: res.status });
    if (!res.ok && !data.error) data.error = `HTTP ${res.status}`;
    return data;
  } catch (error) {
    if (error.name === "AbortError" || error instanceof TypeError) {
      const failure = new Error(I18n.t(error.name === "AbortError"
        ? "Сервер не ответил вовремя. Проверьте состояние операции перед повтором."
        : "Нет соединения с пультом. Восстановите связь и повторите запрос."));
      failure.outcomeUnknown = mutation;
      throw failure;
    }
    throw error;
  } finally {
    clearTimeout(timer);
    if (mutation) {
      pendingMutations--;
      document.dispatchEvent(new Event("pz:update-guard-changed"));
    }
  }
}

async function requestOperation(body, { timeout } = {}) {
  if (operationBusy() || S.rconPending) throw new Error(operationAvailabilityReason());
  // Requests captured before this submission cannot describe its outcome.
  opsGeneration++;
  S.actionPending = true;
  S.operationUncertain = false;
  S.pendingOperation = body.op;
  updateButtons();
  try {
    if (!S.opsLoaded && !await refreshOps()) throw new Error(I18n.t("Запрос не принят"));
    if (S.op?.active) throw new Error(operationAvailabilityReason());
    if (opsSnapshot) {
      const requestId = [...crypto.getRandomValues(new Uint8Array(16))].map(value => value.toString(16).padStart(2, "0")).join("");
      S.pendingRequest = { requestId, requestEpoch: opsSnapshot.epoch };
    }
    const res = await api("/api/action", { method: "POST", body: { ...body, ...S.pendingRequest }, timeout });
    const started = res.started || res.operation;
    if (res.error || res.ok !== true || !started) {
      const failure = new Error(res.error || I18n.t("Запрос не принят"));
      failure.outcomeUnknown = res.httpStatus >= 500 || (res.ok !== false && !res.error);
      throw failure;
    }
    const hasReceipt = validOperationState(res.operationState);
    const hadActive = !!S.op?.active;
    if (hasReceipt) renderOp(res.operationState);
    // An accepted operation keeps its lock even if the first state refresh fails.
    const finished = res.operationId && S.op?.history?.some(row => row.id === res.operationId);
    if (finished && !hadActive && !S.op?.active) refreshAll();
    if (!S.op?.active && !finished && !hasReceipt) {
      S.op = { ...S.op, active: { id: res.operationId, op: started, phase: I18n.t("Подготовка…"), startedAt: new Date().toISOString() } };
      S.lastOpActive = true;
      opsGeneration++;
      updateButtons();
    }
    await refreshOps();
    return res;
  } catch (error) {
    if (error.outcomeUnknown) {
      S.operationUncertain = true;
      await refreshOps();
      recoverOperationState();
    }
    throw error;
  } finally {
    if (!S.operationUncertain) {
      clearPendingOperation();
    }
    updateButtons();
  }
}

async function action(op, extra = {}) {
  try {
    await requestOperation({ op, ...extra });
    return true;
  } catch (e) {
    if (!$("modalRoot").hidden) modal.error(e);
    else showActionError(op, e);
    if (!$("modalRoot").hidden) toast(e.message || String(e), "error");
    return false;
  }
}

function showActionError(op, error) {
  showLocalResult("error", error.outcomeUnknown ? OP_TITLES[op] || op : I18n.msg`${OP_TITLES[op] || op} — запрос не принят`, I18n.msg`${error.message || error} Проверьте соединение и события, затем повторите действие.`);
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
    $("updNote").textContent = I18n.t("Сверяется образ запущенного сервера с его реестром.");
  } else if (u.available === false) {
    pill.dataset.state = "ok"; pill.textContent = I18n.t("актуально");
    $("updNote").textContent = I18n.t("Сверяется образ запущенного сервера с его реестром.");
  } else if (u.error) {
    pill.dataset.state = "bad"; pill.textContent = I18n.t("ошибка проверки");
    $("updNote").textContent = u.error;
  } else if (u.note) {
    pill.dataset.state = "unknown"; pill.textContent = "—";
    $("updNote").textContent = u.note;
  } else {
    pill.dataset.state = "unknown"; pill.textContent = I18n.t("не проверялось");
    $("updNote").textContent = I18n.t("Сверяется образ запущенного сервера с его реестром.");
  }
  $("updNote").hidden = !(remote || u.error || (u.note && u.available == null));
  const same = u.local && u.remote && u.local === u.remote;
  $("updRemote").classList.toggle("ok-same", !!same);
  $("updHubDate").textContent = u.at ? fmtTime(u.at) : "—";
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
  if (settingsCanRender("tgMtproxySwitch")) {
    // Malformed persisted/env values must keep the protected route selected.
    const configured = Object.hasOwn(tg, "mtproxyEnabled");
    $("tgMtproxySwitch").checked = configured && tg.mtproxyEnabled !== false;
    $("tgMtproxyConfigHint").hidden = !configured || typeof tg.mtproxyEnabled === "boolean";
  }
  if (settingsCanRender("tgMtproxyHost")) $("tgMtproxyHost").value = tg.mtproxyHost || "";
  if (settingsCanRender("tgMtproxyPort")) $("tgMtproxyPort").value = tg.mtproxyPort ?? 443;
  if (settingsCanRender("tgApiId")) $("tgApiId").value = tg.apiId || "";
  if (!settingState("telegram").pending && !settingState("telegram").dirty) renderTelegramSecrets(tg);
  updateTelegramProxyFields();
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
  for (const [summaryId, sourceId] of [
    ["sumImagePill", "updPill"],
    ["sumDashboardPill", "dashboardUpdPill"],
    ["sumModsPill", "modsPill"],
  ]) {
    const summary = $(summaryId), source = $(sourceId);
    summary.dataset.state = source.dataset.state;
    summary.textContent = source.textContent;
  }
}

function setAvailability(id, message) {
  const element = $(id);
  if (operationBusy()) message = "";
  if (element.textContent !== message) element.textContent = message;
  if (element.hidden !== !message) element.hidden = !message;
}

function updateOperationWidgets() {
  const busy = operationBusy();
  if (updateGuardBusy !== busy) {
    updateGuardBusy = busy;
    document.dispatchEvent(new Event("pz:update-guard-changed"));
  }
  const operation = S.op?.active?.op || S.pendingOperation;
  const loadingLabel = operation ? I18n.msg`Идёт операция: ${OP_TITLES[operation] || operation}` : I18n.t("Идёт операция");
  if (!busy) {
    document.querySelectorAll('[aria-describedby~="opbar"]').forEach(control => {
      const descriptions = control.getAttribute("aria-describedby").split(/\s+/).filter(id => id && id !== "opbar");
      if (descriptions.length) setDomAttribute(control, "aria-describedby", descriptions.join(" "));
      else control.removeAttribute("aria-describedby");
    });
  }
  document.querySelectorAll("#sec-status, #sec-players, #view-settings .editor-card, #view-mods .editor-card, #sec-updates, #sec-dashboard-update, #sec-mods-update, #sec-watchdog, #sec-notify, #sec-bksched, #sec-backups, #sec-console").forEach(widget => {
    widget.classList.toggle("operation-loading", busy);
    setDomAttribute(widget, "aria-busy", String(busy));
    let loader = widget.querySelector(":scope > .operation-loader");
    if (busy && !loader) {
      loader = document.createElement("div");
      loader.className = "operation-loader";
      const spinner = document.createElement("span");
      spinner.className = "op-spin";
      spinner.setAttribute("aria-hidden", "true");
      const label = document.createElement("span");
      label.className = "operation-label";
      loader.append(spinner, label);
      widget.append(loader);
    }
    if (loader) {
      loader.hidden = !busy;
      const label = loader.querySelector(".operation-label");
      if (busy && label.textContent !== loadingLabel) label.textContent = loadingLabel;
    }
  });
}

function updateButtons() {
  const o = S.overview;
  const opening = document.body.classList.contains("is-booting");
  const busy = operationBusy() || S.rconPending || S.demo || opening;
  const remote = !!(o && o.mode === "remote");
  const rconOk = !!(o && o.rcon && o.rcon.state === "ok");
  const running = !remote && !!(o && o.containerInfo && o.containerInfo.running);
  const found = !!(o && o.containerInfo);
  const hostHint = I18n.t("Доступно только при запуске пульта на хосте сервера");
  const consoleLive = !rconAvailabilityReason();
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
  for (const [id, operation, label] of [
    ["btnCancelMods", "mods-restart", "Отменить обновление модов"],
    ["btnCancelUpdate", "apply-update", "Отменить обновление сервера"],
  ]) {
    const matching = S.op?.active?.op === operation;
    const cancelPending = matching && (!!S.op.active.cancelRequested || pendingCancellations.has(operationIdentity(S.op.active)));
    $(id).hidden = !matching || (!S.op.active.cancellable && !cancelPending);
    $(id).disabled = remote || S.demo || cancelPending || !S.op?.active?.cancellable;
    $(id).textContent = cancelPending ? I18n.t("Отмена…") : I18n.t(label);
  }
  // автонастройки и watchdog пишут в настройки и работают только с хоста —
  // в remote-режиме они тихо ничего не делают, честно их глушим
  const hostOnly = busy || S.demo || remote;
  for (const [group, definition] of Object.entries(SETTING_GROUPS)) {
    const disabled = ["telegram", "playerNotifications"].includes(group) ? busy : hostOnly;
    for (const id of definition.ids) setDomProperty($(id), "disabled", disabled);
  }
  setDomProperty($("btnTgChats"), "disabled", busy || !!S.telegramChatsPending || $("tgMtproxySwitch").checked);
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
  $("logsFilter").disabled = remote || S.demo || opening;
  updateLogsRefreshButton();
  $("logsAuto").disabled = remote || opening;
  $("logsLatest").disabled = remote || opening || !S.logsLines.length;
  document.querySelectorAll("#logLevels button").forEach(button => { button.disabled = remote || opening; });
  document.querySelectorAll("#playersBody .p-actions .icon-btn").forEach((b) => { if (b.disabled !== !consoleLive) b.disabled = !consoleLive; });
  document.querySelectorAll("#backupsBody .icon-btn").forEach((b) => { const disabled = busy || S.demo || remote || b.dataset.verified === "true"; if (b.disabled !== disabled) b.disabled = disabled; });
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
  setAvailability("consoleAvailability", rconAvailabilityReason({ includePending: false }) || (o && !rconOk ? I18n.t("Последняя проверка RCON не подтвердила связь. Можно отправить команду повторно; проверьте пароль и порт RCON.") : ""));
  if (consoleBootLine?.isConnected) {
    const consoleStatus = commonReason || (!o ? I18n.t("Пульт подключается к серверу…")
      : !consoleLive ? I18n.t("Ожидаем доступность RCON. Последние ответы сохранены.")
      : !rconOk ? I18n.t("Последняя проверка RCON не подтвердила связь. Можно отправить команду повторно; проверьте пароль и порт RCON.")
      : I18n.t("Консоль доступна — команды отправляются на сервер по RCON."));
    setDomProperty(consoleBootLine, "textContent", consoleStatus);
  }
  for (const id of ["btnStart", "btnStop", "btnRestart", "btnSaveWorld"]) $(id).setAttribute("aria-describedby", "operationAvailability");
  for (const id of ["btnCheckUpd", "btnApplyUpd"]) $(id).setAttribute("aria-describedby", "maintenanceAvailability");
  for (const id of ["btnCheckDashboardUpd", "btnApplyDashboardUpd"]) $(id).setAttribute("aria-describedby", "maintenanceAvailability dashboardUpdNote");
  $("btnBackup").setAttribute("aria-describedby", "backupAvailability");
  $("consoleInput").setAttribute("aria-describedby", "consoleAvailability consoleStatus");
  window.ConfigEditor?.operationChanged();
  renderActiveOperation();
  updateOperationWidgets();
  // The inert startup shell needs no temporary availability descriptions.
  if (document.body.classList.contains("is-booting")) return;
  for (const [view, description] of Object.entries({
    players: "playersAvailability", mods: "modsAvailability", settings: "settingsAvailability",
    maintenance: "maintenanceAvailability", backups: "backupAvailability", console: "consoleAvailability",
  })) {
    document.querySelectorAll(`#view-${view} button:disabled, #view-${view} input:disabled, #view-${view} select:disabled, #view-${view} textarea[readonly]`).forEach(control => {
      const ids = new Set((control.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
      ids.add(operationBusy() ? "opbar" : view === "console" && S.rconPending ? "consoleStatus" : description);
      if (!operationBusy()) ids.delete("opbar");
      setDomAttribute(control, "aria-describedby", [...ids].join(" "));
    });
  }
}

function renderActiveOperation() {
  const active = S.op?.active || (S.actionPending ? { op: S.pendingOperation, phase: I18n.t("Подготовка…") } : null);
  if (active) {
    setDomProperty($("opbar"), "hidden", false);
    setDomProperty($("opMsg"), "textContent", active.message || "");
    setDomProperty($("opMsg"), "hidden", !active.message);
    const preparing = ["Подготовка…", I18n.t("Подготовка…")];
    const done = ["Готово", I18n.t("Готово")];
    const phase = typeof active.phase === "string" ? active.phase.trim() : "";
    let stages = (Array.isArray(active.stages) ? active.stages : []).filter(stage => typeof stage === "string" && stage.trim() && !done.includes(stage));
    // Stages are visits, not a plan: keep repeated visits and append a newer phase.
    if (phase && !done.includes(phase) && stages.at(-1) !== phase) stages.push(phase);
    if (stages.some(stage => !preparing.includes(stage))) stages = stages.filter(stage => !preparing.includes(stage));
    if (!stages.length) stages = [I18n.t("Подготовка…")];
    // A nested worker may say Done before the outer operation finishes.
    // Its last real visit stays current until an authoritative terminal frame.
    const markup = stages.map((stage, index) => {
      const current = index === stages.length - 1;
      const label = `${stage}: ${current ? I18n.t("Выполняется") : I18n.t("Завершено")}`;
      const marker = current ? index + 1 : '<svg class="op-stage-check" viewBox="0 0 24 24"><path d="m5 12 4 4L19 6"/></svg>';
      return `<li class="route-step" data-state="${current ? "current" : "ok"}"${current ? ' aria-current="step"' : ' data-complete="true"'} aria-label="${esc(label)}"><div class="route-step-body"><span class="route-marker" aria-hidden="true">${marker}</span><span class="route-copy"><strong class="op-stage-label">${esc(stage)}</strong><span class="op-stage-state${current ? "" : " sr-only"}">${current ? I18n.t("Выполняется") : I18n.t("Завершено")}</span></span></div>${current ? "" : '<span class="route-connector" aria-hidden="true"></span>'}</li>`;
    }).join("");
    const title = OP_TITLES[active.op] || active.op;
    setDomProperty($("opPhase"), "textContent", title);
    if ($("opStages").innerHTML !== markup) $("opStages").innerHTML = markup;
    setDomProperty($("opStages").dataset, "layout", stages.length > 5 ? "vertical" : "horizontal");
  } else {
    setDomProperty($("opbar"), "hidden", true);
  }
  updateOperationCountdown();
}

function renderOp(op, { reconcilePending = false, fromRefresh = false, fromStream = false } = {}) {
  // Errors and partial frames are not evidence that an operation has finished.
  if (!validOperationState(op)) return false;
  const snapshot = op.snapshot;
  if (snapshot) {
    if (retiredOpsEpochs.has(snapshot.epoch)) return false;
    if (opsSnapshot?.epoch === snapshot.epoch && snapshot.sequence <= opsSnapshot.sequence) return false;
    if (fromStream && opsSnapshot && snapshot.epoch !== opsSnapshot.epoch) {
      // A buffered old connection cannot establish that the server restarted.
      if (!opsEpochRefresh) {
        opsGeneration++;
        opsEpochRefresh = refreshOps().finally(() => { opsEpochRefresh = null; });
      }
      return false;
    }
    if (opsSnapshot && opsSnapshot.epoch !== snapshot.epoch) retiredOpsEpochs.add(opsSnapshot.epoch);
    opsSnapshot = snapshot;
  } else if (opsSnapshot) return false;
  const active = op.active;
  if (active?.id && active.id === S.op?.active?.id && S.op.active.cancelRequested) {
    op = { ...op, active: { ...active, cancelRequested: true, cancellable: false } };
  }
  if (!fromRefresh) opsGeneration++;
  if (S.operationUncertain && reconcilePending) {
    clearPendingOperation();
  }
  for (const h of [...(op?.history || [])].reverse()) {
    if (!h || typeof h !== "object") continue;
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
  updateButtons();
  return true;
}

function updateOperationCountdown() {
  const deadline = Number(S.op?.active?.countdownEndsAt);
  const available = Number.isFinite(deadline) && deadline > 0;
  setDomProperty($("opTimer"), "hidden", !available);
  setDomProperty($("opCountdown"), "hidden", !available);
  if (available) {
    const left = Math.max(0, Math.ceil(deadline - Date.now() / 1000));
    setDomProperty($("opCountdown"), "textContent", `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`);
  }
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
function loadFailureMarkup(kind, message) {
  return `<p class="list-error">${esc(message || I18n.t("Не удалось загрузить данные. Повторите попытку."))}</p><button type="button" class="btn small" data-load-retry="${kind}">${esc(I18n.t("Повторить"))}</button>`;
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
  window.PlayersMap?.update(data);
  const body = $("playersBody");
  if (!data.ok) {
    setListMessage(body, "error", loadFailureMarkup("players", data.error));
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
  const names = window.PlayersMap?.filterNames([...new Set(data.names)]) || [...new Set(data.names)];
  if (!names.length) {
    setListMessage(body, "empty", I18n.msg`<p class="list-empty">Игроки не найдены.</p>`);
    return;
  }
  syncRows(body, names, (name) => name, (n) => {
    const template = document.createElement("template");
    template.innerHTML = I18n.msg`
    <div class="player-row">
      <span class="dot"></span>
      <span class="p-name" title="${esc(n)}">${esc(n)}</span>
      <button type="button" class="p-locate btn small ghost" data-locate="${esc(n)}" disabled>На карте</button>
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
  window.PlayersMap?.syncRoster();
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
        await runRconCommand(cmd);
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
    const halfWidth = tip.getBoundingClientRect().width / 2;
    tip.style.left = Math.max(halfWidth, Math.min(rect.width - halfWidth, ev.clientX - rect.left)) + "px";
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
  setDomProperty($("ramSub").dataset, "state", st.ok ? "ok" : "error");
  if (S.overview && S.overview.mode === "remote") {
    $("cpuVal").textContent = "—"; $("ramVal").textContent = "—";
    $("ramSub").textContent = I18n.t("метрики — только с хоста сервера");
    $("netIn").textContent = "—"; $("netOut").textContent = "—"; $("pids").textContent = "—";
    $("cpuBar").style.width = "0%"; $("ramBar").style.width = "0%";
    return;
  }
  if (!st.ok) {
    $("cpuVal").textContent = "—"; $("ramVal").textContent = "—";
    setListMessage($("ramSub"), "error", loadFailureMarkup("stats", st.error));
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

/* ───────────────────────── бэкапы ───────────────────────── */

const backupsPager = { page: 0, size: 10 };
let backupsLoaded = false;

function backupStorageLabel(storage) {
  return storage === "s3" ? "S3" : I18n.t("Локально");
}

function backupVerificationText(verified) {
  if (!verified) return "";
  const notes = [I18n.msg`Проверен ${fmtTime(verified.verifiedAt)} · файлов: ${verified.files}`];
  if (!verified.hasServerIni) notes.push(I18n.t("Нет конфигурации сервера (Server/*.ini)"));
  if (!verified.hasMapData) notes.push(I18n.t("Нет данных мира"));
  return notes.join(" · ");
}

function renderBackups(data) {
  const body = $("backupsBody");
  setDomProperty($("backupsStorageError"), "textContent", data.ok ? data.storageError || "" : "");
  setDomProperty($("backupsStorageError"), "hidden", !data.ok || !data.storageError);
  if (!data.ok) {
    S.backupsItems = [];
    setDomProperty($("backupsPager"), "hidden", true);
    setListMessage(body, "error", loadFailureMarkup("backups", data.error));
    return;
  }
  renderBkSchedule(data);
  backupsLoaded = true;
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
      <div class="b-identity"><span class="b-name mono" title="${esc(b.name)} — создан ${esc(b.mtime)}">${esc(b.name)}</span><span class="b-storage">${esc(backupStorageLabel(b.storage))}</span></div>
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
    setDomProperty(row.querySelector(".b-storage"), "textContent", backupStorageLabel(b.storage));
    const verified = b.verification;
    const verifyButton = row.querySelector('[data-b="verify"]');
    setDomAttribute(verifyButton, "data-verified", verified ? "true" : "false");
    setDomProperty(verifyButton.querySelector("span"), "textContent", I18n.t(verified ? "Проверен" : "Проверить"));
    setDomProperty(verifyButton, "title", verified ? I18n.msg`Проверен ${fmtTime(verified.verifiedAt)} · файлов: ${verified.files}` : I18n.t("Проверить архив"));
    setDomAttribute(verifyButton, "aria-label", verified ? `${I18n.t("Проверен")}: ${b.name}` : I18n.msg`Проверить ${b.name}`);
    const result = row.querySelector(".backup-result");
    setDomProperty(result, "hidden", !verified);
    setDomProperty(result, "textContent", backupVerificationText(verified));
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
      ${j.status === "error" && j.error ? `<p class="j-error">${esc(j.error)}</p>` : ""}
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
  const archive = S.backupsItems.find(item => item.name === name);
  const running = S.overview?.containerInfo?.running;
  const stopNotice = running === false
    ? I18n.t("Сервер сейчас остановлен. После замены данных пульт запустит его.")
    : I18n.t("Если сервер работает, игроки получат предупреждение за 60 секунд, затем пульт остановит сервер. После замены данных сервер будет запущен.");
  modal.open({
    title: I18n.t("Восстановление из бэкапа"),
    danger: true,
    okLabel: I18n.t("Восстановить"),
    bodyHTML: I18n.msg`
      <p class="backup-selected mono">${esc(name)}</p>
      ${archive ? I18n.msg`<dl class="backup-details"><dt>Хранилище</dt><dd>${esc(backupStorageLabel(archive.storage))}</dd><dt>Размер</dt><dd>${esc(archive.size != null ? fmtBytes(archive.size) : archive.sizeText || "—")}</dd><dt>Создан</dt><dd>${esc(fmtTime(archive.mtime))}</dd></dl>` : ""}
      ${archive?.verification ? `<p class="backup-restore-verification hint">${esc(backupVerificationText(archive.verification))}</p>` : ""}
      <p class="backup-restore-copy">Текущий мир и конфиги будут <b>полностью заменены</b> содержимым этого архива.</p>
      <p class="backup-restore-copy">Перед остановкой и заменой данных пульт заново проверит архив и подготовит файлы восстановления.</p>
      <p class="backup-restore-copy">${esc(stopNotice)}</p>
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
  const running = S.overview?.containerInfo?.running;
  const liveHint = I18n.t("Если сервер работает, пульт запросит сохранение через RCON. Бэкап без остановки не гарантирует согласованность файлов.");
  const stopHint = I18n.t("Если сервер работает, игроки получат предупреждение за 60 секунд. Пульт остановит сервер на время создания архива и запустит его снова. Остановленный сервер останется выключенным.");
  modal.open({
    title: I18n.t("Создать бэкап"),
    okLabel: I18n.t("Создать"),
    bodyHTML: I18n.msg`
      <p>Архив собирается из каталога данных сервера (мир, конфиги, whitelist). Логи в бэкап не входят.</p>
      ${running === false ? I18n.html("<p>Сервер сейчас остановлен. Пульт создаст бэкап и оставит сервер выключенным.</p>") : I18n.html('<label class="check"><input type="checkbox" id="bkStop" aria-describedby="bkModeHint" /> Остановить сервер на время бэкапа (надёжнее для целостности)</label>')}
      ${typeof running !== "boolean" ? I18n.html("<p>Состояние сервера неизвестно. Пульт проверит его при запуске бэкапа.</p>") : ""}
      ${running !== false ? `<p id="bkModeHint">${esc(liveHint)}</p>` : ""}
    `,
    onConfirm: async () => {
      return action("backup", { stopServer: $("bkStop")?.checked || false });
    },
  });
  $("bkStop")?.addEventListener("change", () => {
    $("bkModeHint").textContent = $("bkStop").checked ? stopHint : liveHint;
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

function eventLevel(ev) {
  // Mirror logging_utils.event_level; tolerate invalid levels in older journal records.
  let level = String(ev.level ?? "").trim().toUpperCase();
  if (level === "WARN") level = "WARNING";
  return ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].includes(level)
    ? level : ({ error: "ERROR", "rcon-error": "ERROR", warn: "WARNING" }[ev.type] || "INFO");
}

function eventSeverity(ev) {
  const level = eventLevel(ev);
  const label = { WARNING: I18n.t("Предупреждение"), ERROR: I18n.t("Ошибка"), CRITICAL: I18n.t("Критическая ошибка") }[level];
  return label ? `<span class="event-severity">${esc(label)}</span>` : "";
}

function eventDate(ts) {
  if (ts == null || ts === "") return null;
  const date = new Date(ts);
  return Number.isFinite(date.getTime()) ? date : null;
}

function eventTime(ts, grouped = false) {
  const date = eventDate(ts);
  if (!date) return `<span class="e-time mono">${esc(I18n.t("Время неизвестно"))}</span>`;
  const full = eventFullFmt.format(date);
  return `<time class="e-time mono" datetime="${esc(date.toISOString())}" title="${esc(full)}">${esc(grouped ? timeFmt.format(date) : relTime(ts) || fmtTime(ts))}</time>`;
}

function selectEventsFilter(filter) {
  eventsFilter = filter;
  eventsPager.page = 0;
  document.querySelectorAll("#eventFilters .chip").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.ef === filter)));
  if (eventsData) renderEvents(eventsData);
}

function renderEvents(data) {
  eventsData = data;
  const body = $("eventsBody");
  if (!data.ok) {
    setDomProperty($("eventsPager"), "hidden", true);
    setListMessage(body, "error", loadFailureMarkup("events", data.error));
    setListMessage($("recentBody"), "error", loadFailureMarkup("events", data.error));
    return;
  }
  const items = data.items || [];
  renderRecent(items);
  const visible = eventsFilter === "all"
    ? items
    : items.filter((ev) => (EVENT_GROUPS[eventsFilter] || []).includes(ev.type) || (eventsFilter === "problems" && ["WARNING", "ERROR", "CRITICAL"].includes(eventLevel(ev))));
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
      : I18n.msg`<div class="events-empty"><p class="list-empty">Нет событий в этой категории.</p><button class="btn small" type="button" data-events-reset>Сбросить фильтр</button></div>`);
    return;
  }
  setDomProperty(body.dataset, "state", "ok");
  const today = new Date();
  const yest = new Date(today);
  yest.setDate(yest.getDate() - 1);
  const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  let html = "";
  let lastDay = "";
  for (const ev of pageItems) {
    const d = eventDate(ev.ts);
    const key = d ? d.toDateString() : "unknown";
    if (key !== lastDay) {
      lastDay = key;
      const label = !d ? I18n.t("Время неизвестно") : sameDay(d, today) ? I18n.t("Сегодня") : sameDay(d, yest) ? I18n.t("Вчера") : (d.getFullYear() !== today.getFullYear() ? eventYearDayFmt : eventDayFmt).format(d);
      html += `<div class="event-day">${esc(label)}</div>`;
    }
    html += `<div class="event-row" data-kind="${esc(ev.type)}" data-level="${eventLevel(ev)}">
      <div class="event-meta">${eventTime(ev.ts, true)}</div>
      <div class="event-content">${eventSeverity(ev)} <span class="e-text"><b>${esc(EVENT_LABELS[ev.type] || ev.type)}.</b> ${esc(ev.text)}</span></div>
    </div>`;
  }
  setStaticMarkup(body, html);
}

$("eventFilters").addEventListener("click", (e) => {
  const btn = e.target.closest(".chip[data-ef]");
  if (!btn) return;
  selectEventsFilter(btn.dataset.ef);
});

$("eventsBody").addEventListener("click", event => {
  if (!event.target.closest("[data-events-reset]")) return;
  selectEventsFilter("all");
  document.querySelector('#eventFilters [data-ef="all"]').focus({ preventScroll: true });
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
    <div class="event-row" data-kind="${esc(ev.type)}" data-level="${eventLevel(ev)}">
      <div class="event-meta">${eventTime(ev.ts)}</div>
      <div class="event-content">${eventSeverity(ev)} <span class="e-text"><b>${esc(EVENT_LABELS[ev.type] || ev.type)}.</b> ${esc(ev.text)}</span></div>
    </div>`).join(""));
}

/* ───────────────────────── консоль ───────────────────────── */

function consoleAppend(text, cls = "") {
  const out = $("consoleOut");
  const follow = out.scrollHeight - out.scrollTop - out.clientHeight < 30;
  const line = document.createElement("span");
  if (cls) line.className = cls;
  line.textContent = text;
  out.appendChild(line);
  out.appendChild(document.createTextNode("\n"));
  while (out.childNodes.length > 800) out.removeChild(out.firstChild);
  if (follow) out.scrollTop = out.scrollHeight;
  return line;
}

function rconAvailabilityReason({ includePending = true } = {}) {
  if (operationBusy() || S.demo || includePending && S.rconPending) return operationAvailabilityReason();
  if (!S.overview || document.body.classList.contains("is-booting")) return I18n.t("Пульт подключается к серверу…");
  if (S.overview.rconConfigured === false) return I18n.t("RCON не настроен. Проверьте пароль и порт RCON в настройках подключения.");
  if (S.overview.mode !== "remote" && !S.overview.containerInfo?.running) return I18n.t("Сервер остановлен — RCON недоступен");
  return "";
}

function createConsoleEntry(command, state, text) {
  const out = $("consoleOut");
  const follow = out.scrollHeight - out.scrollTop - out.clientHeight < 30;
  if (consoleBootLine?.isConnected) {
    const separator = consoleBootLine.nextSibling;
    if (separator?.nodeType === Node.TEXT_NODE && separator.textContent === "\n") separator.remove();
    consoleBootLine.remove();
    consoleBootLine = null;
  }
  const entry = document.createElement("div");
  entry.className = "console-entry";
  entry.dataset.state = state;
  const heading = document.createElement("span");
  heading.className = "console-command";
  heading.textContent = `> ${command}`;
  const response = document.createElement("span");
  response.className = "console-response";
  response.textContent = text;
  entry.append(heading, response);
  out.appendChild(entry);
  while (out.childNodes.length > 400) out.removeChild(out.firstChild);
  if (follow) out.scrollTop = out.scrollHeight;
  return { entry, response };
}

async function runRconCommand(command) {
  const unavailable = rconAvailabilityReason();
  command = command.trim();
  const refusal = unavailable || (!command || command.length > 500 ? I18n.t("Пустая или слишком длинная команда") : "");
  if (refusal) {
    createConsoleEntry(command, "error", refusal);
    setDomProperty($("consoleStatus"), "textContent", refusal);
    throw new Error(refusal);
  }
  S.rconPending = true;
  const out = $("consoleOut");
  const { entry, response } = createConsoleEntry(command, "pending", I18n.t("Ожидается ответ RCON…"));
  setDomProperty($("consoleStatus"), "textContent", I18n.t("Ожидается ответ RCON…"));
  updateButtons();
  let followResponse = false;
  try {
    const result = await api("/api/rcon", { method: "POST", body: { command }, timeout: 30000 });
    if (result.ok !== true || result.httpStatus >= 400 || typeof result.output !== "string") {
      const error = new Error(result.error || I18n.t("Пульт вернул некорректный ответ на команду."));
      error.outcomeUnknown = !(result.httpStatus >= 400 && result.httpStatus < 500);
      throw error;
    }
    followResponse = out.scrollHeight - out.scrollTop - out.clientHeight < 30;
    response.textContent = result.output || I18n.t("(без ответа)");
    entry.dataset.state = "success";
    setDomProperty($("consoleStatus"), "textContent", I18n.t("Ответ RCON получен."));
    return result;
  } catch (error) {
    followResponse = out.scrollHeight - out.scrollTop - out.clientHeight < 30;
    if (error.outcomeUnknown) {
      error.message = I18n.msg`${error.message} Команда могла выполниться. Проверьте состояние сервера и логи перед повтором.`;
    }
    entry.dataset.state = error.outcomeUnknown ? "unknown" : "error";
    response.textContent = error.message || String(error);
    setDomProperty($("consoleStatus"), "textContent", response.textContent);
    throw error;
  } finally {
    S.rconPending = false;
    updateButtons();
    if (followResponse) out.scrollTop = out.scrollHeight;
  }
}

$("consoleForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (operationBusy() || $("consoleInput").disabled) return;
  const input = $("consoleInput");
  const cmd = input.value.trim();
  if (!cmd) return;
  if (rconAvailabilityReason()) return;
  if (consoleHistory.items[consoleHistory.items.length - 1] !== cmd) consoleHistory.items.push(cmd);
  if (consoleHistory.items.length > 100) consoleHistory.items.shift();
  consoleHistory.idx = consoleHistory.items.length;
  consoleHistory.draft = input.value;
  const draft = input.value;
  try {
    await runRconCommand(cmd);
    if (input.value === draft) input.value = "";
    consoleHistory.draft = input.value;
  } catch (err) {
    setDomProperty($("consoleStatus"), "textContent", err.message || String(err));
  }
});

const consoleHistory = { items: [], idx: 0, draft: "" };

$("consoleInput").addEventListener("keydown", (e) => {
  if (e.key === "ArrowUp" && consoleHistory.items.length) {
    e.preventDefault();
    if (consoleHistory.idx === consoleHistory.items.length) consoleHistory.draft = $("consoleInput").value;
    consoleHistory.idx = consoleHistory.idx === consoleHistory.items.length
      ? consoleHistory.items.length - 1
      : Math.max(0, consoleHistory.idx - 1);
    $("consoleInput").value = consoleHistory.items[consoleHistory.idx] || "";
  } else if (e.key === "ArrowDown" && consoleHistory.idx < consoleHistory.items.length) {
    e.preventDefault();
    consoleHistory.idx = Math.min(consoleHistory.items.length, consoleHistory.idx + 1);
    $("consoleInput").value = consoleHistory.idx === consoleHistory.items.length
      ? consoleHistory.draft : consoleHistory.items[consoleHistory.idx];
  }
});

document.querySelectorAll("#quickCmds .chip").forEach((chip) => {
  chip.addEventListener("click", async () => {
    if (chip.dataset.cmd) {
      try { await runRconCommand(chip.dataset.cmd); }
      catch (error) { setDomProperty($("consoleStatus"), "textContent", error.message || String(error)); }
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
          await runRconCommand(`servermsg "${safe}"`);
        },
      });
    }
  });
});

/* ───────────────────────── логи ───────────────────────── */

const LOG_TIMESTAMP = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s/;
const LOG_PREFIX = /^(?:\[(?:\d[\d: .-]*)\]\s*)?(?:\[([^\]]*\/(?:ERROR|FATAL|SEVERE|WARN|WARNING|INFO|DEBUG|TRACE))\]|\[(ERROR|FATAL|SEVERE|WARN|WARNING|INFO|DEBUG|TRACE|LOG)\]|(ERROR|FATAL|SEVERE|WARN|WARNING|INFO|DEBUG|TRACE|LOG)\b)/i;
const JAVA_EXCEPTION = /^(?:Exception in thread "[^"]+"\s+)?(?:[\w$]+\.)*[\w$]*(?:Exception|Error)(?::|\s*$)/;
const STACK_CONTINUATION = /^(?:at\s+\S+\(|Caused by:\s|Suppressed:\s|\.\.\.\s+\d+\s+(?:more|common frames omitted)\b)/;

function classifyLog(line) {
  const body = line.replace(LOG_TIMESTAMP, "").trimStart();
  const prefix = body.match(LOG_PREFIX);
  const level = (prefix?.[1]?.split("/").pop() || prefix?.[2] || prefix?.[3] || "").toUpperCase();
  if (["ERROR", "FATAL", "SEVERE"].includes(level) || (!prefix && JAVA_EXCEPTION.test(body))) return "error";
  if (["WARN", "WARNING"].includes(level)) return "warn";
  return "";
}

function parseLogs(text) {
  let previousLevel = "";
  return (text || "").split("\n").filter(raw => raw.trim()).map((raw) => {
    const m = raw.match(LOG_TIMESTAMP);
    const body = raw.replace(LOG_TIMESTAMP, "");
    let view = raw;
    if (m) {
      const dt = new Date(m[1]);
      view = (isNaN(dt) ? m[1] : `${dayFmt.format(dt)} ${timeFullFmt.format(dt)}`) + " " + body;
    }
    const level = classifyLog(raw) || (STACK_CONTINUATION.test(body.trimStart()) ? previousLevel : "");
    previousLevel = level;
    return { raw, view, level, timestamp: m?.[1] || "" };
  });
}

function renderLogs(data) {
  if (S.overview && S.overview.mode === "remote") {
    S.logsText = null;
    S.logsLines = [];
    setDomProperty($("logsResult"), "hidden", true);
    setDomProperty($("logsError"), "hidden", true);
    setDomProperty($("logsLimit"), "hidden", true);
    setDomProperty($("logsScope"), "hidden", true);
    setDomProperty($("logsFreshness"), "textContent", "");
    renderedMarkup.delete($("logsOut"));
    setDomProperty($("logsOut"), "textContent", I18n.t("Логи контейнера доступны только при запуске пульта на хосте сервера."));
    return;
  }
  if (!data.ok) {
    setDomProperty($("logsError"), "textContent", (data.error || I18n.t("Логи недоступны")) + (S.logsUpdatedAt ? I18n.msg` · последние данные: ${fmtTime(S.logsUpdatedAt)}` : ""));
    setDomProperty($("logsError"), "hidden", false);
    if (!S.logsUpdatedAt) {
      renderedMarkup.delete($("logsOut"));
      setDomProperty($("logsOut"), "textContent", I18n.t("Нет данных логов"));
    }
    return;
  }
  setDomProperty($("logsError"), "hidden", true);
  setDomProperty($("logsLimit"), "hidden", !data.truncated);
  S.logsUpdatedAt = Date.now();
  setDomProperty($("logsFreshness"), "textContent", I18n.msg`Обновлено: ${timeFullFmt.format(S.logsUpdatedAt)}`);
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
  setDomProperty($("logsLatest"), "disabled", !source.length || S.overview?.mode === "remote");
  const filtered = !!f || level !== "all" || !!S.logsSince || !!S.logsUntil;
  const lines = source.filter((l) =>
    (level === "all" || l.level === level) && (!f || l.view.toLowerCase().includes(f)) &&
    (!S.logsSince || Date.parse(l.raw.slice(0, l.raw.indexOf(" "))) >= S.logsSince) &&
    (!S.logsUntil || Date.parse(l.raw.slice(0, l.raw.indexOf(" "))) <= S.logsUntil));
  setDomProperty($("logsScope"), "hidden", !S.logsSince);
  setDomProperty($("logsPeriod"), "textContent", S.logsSince ? I18n.msg`Логи операции${S.logsProfile ? ` · ${S.logsProfile}` : ""} · ${fmtTime(S.logsSince)}${S.logsUntil ? ` — ${fmtTime(S.logsUntil)}` : I18n.t(" · продолжается")}` : "");
  setDomProperty($("logsResult"), "hidden", false);
  setDomProperty($("logsCount"), "textContent", I18n.msg`Строк: ${lines.length} из ${source.length}`);
  setDomProperty($("logsResetFilters"), "hidden", !filtered);
  // Only identical source records can share a row: identifiers and timestamps matter.
  const merged = [];
  for (const l of lines) {
    const key = l.raw;
    const last = merged[merged.length - 1];
    if (last && last.key === key) last.n += 1;
    else merged.push({ view: l.view, level: l.level, timestamp: l.timestamp, key, n: 1 });
  }
  const markup = merged.length ? merged.map((l) => {
    const cls = l.level === "error" ? ' class="l-err"' : l.level === "warn" ? ' class="l-warn"' : "";
    const dup = l.n > 1 ? `<span class="l-dup">× ${l.n}</span>` : "";
    const timestamp = l.timestamp ? ` title="${esc(l.timestamp)}"` : "";
    return `<span${cls}${timestamp}>${esc(l.view)}${dup}</span>`;
  }).join("\n") : esc(source.length ? I18n.t("Нет строк по выбранным фильтрам.") : I18n.t("Нет данных логов"));
  setStaticMarkup(pre, markup);
  if (atBottom && S.logsAuto) pre.scrollTop = pre.scrollHeight;
  else pre.scrollTop = scrollTop;
}

$("logsFilter").addEventListener("input", renderLogsFiltered);
function followLatestLogs() {
  S.logsAuto = true;
  $("logsAuto").checked = true;
  $("logsOut").scrollTop = $("logsOut").scrollHeight;
}
$("logsAuto").addEventListener("change", () => {
  S.logsAuto = $("logsAuto").checked;
  if (S.logsAuto) followLatestLogs();
});
$("logsOut").addEventListener("scroll", () => {
  const pre = $("logsOut");
  if (S.logsAuto && pre.scrollTop + pre.clientHeight < pre.scrollHeight - 30) {
    S.logsAuto = false;
    $("logsAuto").checked = false;
  }
});
$("logsLatest").addEventListener("click", followLatestLogs);
$("logsRefresh").addEventListener("click", () => refreshLogs({ manual: true }));
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
for (const [id, operation, cancelAction, message] of [
  ["btnCancelMods", "mods-restart", "cancel-mods-update", "Запрошена отмена автообновления модов"],
  ["btnCancelUpdate", "apply-update", "cancel-update", "Запрошена отмена обновления сервера"],
]) {
  $(id).addEventListener("click", async () => {
    const active = S.op?.active;
    const identity = operationIdentity(active);
    if (!identity || pendingCancellations.has(identity)) return;
    pendingCancellations.add(identity);
    updateButtons();
    try {
      const res = await api("/api/action", { method: "POST", body: { op: cancelAction, ...(active.id ? { operationId: active.id } : {}) } });
      if (res.error || res.ok !== true || res.cancelRequested !== true) throw new Error(res.error || I18n.t("Запрос не принят"));
      if (S.op?.active?.op === operation && operationIdentity(S.op.active) === identity) {
        S.op.active.cancelRequested = true;
        S.op.active.cancellable = false;
      }
      toast(I18n.t(message), "ok");
    } catch (e) {
      toast(e.message || String(e), "error");
    } finally {
      pendingCancellations.delete(identity);
      updateButtons();
    }
  });
}
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
  try { await runRconCommand("save"); }
  catch (error) { toast(error.message || String(error), "error"); }
});
$("btnCheckUpd").addEventListener("click", async () => {
  await action("check-update");
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
  await action("check-dashboard-update");
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
  await action("check-mods-update");
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

/* ───────────────────────── настройки автообновления ───────────────────────── */

const SETTING_GROUPS = {
  playerNotifications: { ids: ["playerLanguage"], card: "playerNotifications", title: I18n.t("Предупреждения игрокам") },
  autoUpdate: { ids: ["autoSwitch", "autoInterval", "autoWarn", "buBackup"], card: "sec-updates", title: I18n.t("Автообновление образа") },
  watchdog: { ids: ["wdSwitch", "wdThreshold", "wdGracePeriod", "wdRestart"], card: "sec-watchdog", title: "Watchdog RCON" },
  modsUpdate: { ids: ["modsAutoSwitch", "modsAutoInterval", "modsAutoAction", "modsAutoWarn"], card: "sec-mods-update", title: I18n.t("Автообновление модов") },
  telegram: { ids: ["tgSwitch", "tgToken", "tgChat", "tgMtproxySwitch", "tgMtproxyHost", "tgMtproxyPort", "tgMtproxySecret", "tgApiId", "tgApiHash", "tgLanguage", "tgOps", "tgBackup", "tgUpdate", "tgProblems"], card: "sec-notify", title: "Telegram" },
  autoBackup: { ids: ["bkAutoSwitch", "bkAutoTime", "bkAutoKeep", "bkAutoStop"], card: "sec-bkauto", title: I18n.t("Расписание бэкапов") },
};
const settingStates = new Map();
document.addEventListener("pz:before-update", (event) => {
  const settings = [...settingStates.values()];
  const reasons = [];
  if (S.op?.active || S.actionPending || pendingMutations || settings.some(state => state.pending)) reasons.push("operation");
  if (settings.some(state => state.dirty) || $('consoleInput').value.trim()) reasons.push("unsaved");
  if (!$('modalRoot').hidden || $('commandDialog').open) reasons.push("dialog");
  if (reasons.length) {
    event.preventDefault();
    event.detail?.reasons?.push(...reasons);
  }
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

function secretDescription(hasValue) {
  return hasValue ? I18n.t("Значение задано · скрыто") : I18n.t("Не задано");
}

function secretPlaceholder(hasValue) {
  return hasValue ? "••••••••" : I18n.t("Введите новое значение");
}

function setSecretPresence(input, hasValue) {
  input.placeholder = secretPlaceholder(hasValue);
  $(input.dataset.secretDescription).textContent = secretDescription(hasValue);
}

function renderTelegramSecrets(telegram) {
  for (const [id, hasValue] of [["tgToken", !!telegram.botTokenMasked], ["tgMtproxySecret", !!telegram.hasMtproxySecret], ["tgApiHash", !!telegram.hasApiHash]]) {
    setSecretPresence($(id), hasValue);
  }
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
        if (group === "telegram") {
          for (const [id, key] of [["tgToken", "botToken"], ["tgMtproxySecret", "mtproxySecret"], ["tgApiHash", "apiHash"]]) {
            if ($(id).value.trim() === body.telegram[key]) $(id).value = "";
          }
        }
        state.dirty = false;
        if (res.settings) {
          const settings = acceptSettings(res.settings);
          if (group === "telegram") renderTelegramSecrets(settings.telegram || {});
        }
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
      document.dispatchEvent(new Event("pz:update-guard-changed"));
    }
  };
  const result = settingsQueue.then(run);
  settingsQueue = result.then(() => undefined);
  return result;
}

function pushSettings(eventOrGroup) {
  const group = typeof eventOrGroup === "string" ? eventOrGroup : settingGroup(eventOrGroup?.target?.id) || "telegram";
  if (group === "watchdog" && !$("wdGracePeriod").reportValidity()) return Promise.resolve(false);
  if (group === "telegram" && ["tgMtproxyPort", "tgApiId"].some(id => !$(id).reportValidity())) return Promise.resolve(false);
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
      mtproxyEnabled: $("tgMtproxySwitch").checked,
      mtproxyHost: $("tgMtproxyHost").value.trim(),
      mtproxyPort: $("tgMtproxyPort").value === "" ? "" : Number($("tgMtproxyPort").value),
      mtproxySecret: $("tgMtproxySecret").value.trim(),
      apiId: $("tgApiId").value === "" ? "" : Number($("tgApiId").value),
      apiHash: $("tgApiHash").value.trim(),
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
["tgMtproxyHost", "tgMtproxyPort", "tgMtproxySecret", "tgApiId", "tgApiHash"].forEach((id) => $(id).addEventListener("change", pushSettings));
function updateTelegramProxyFields() {
  const enabled = $("tgMtproxySwitch").checked;
  $("tgMtproxyFields").hidden = !enabled;
  $("tgMtproxyChatsHint").hidden = !enabled;
  if (enabled) $("tgChatsBody").hidden = true;
}
$("tgMtproxySwitch").addEventListener("change", (event) => {
  updateTelegramProxyFields();
  updateButtons();
  pushSettings(event);
});
async function flushTelegramSettings() {
  const state = settingState("telegram");
  do {
    if (!await pushSettings("telegram")) return false;
  } while (state.pending || state.dirty);
  return true;
}
["tgOps", "tgBackup", "tgUpdate", "tgProblems"].forEach((id) => $(id).addEventListener("change", pushSettings));
/* «Найти чаты бота»: getUpdates показывает, где бот реально состоит,
   и подставляет настоящий chat id вместо копипасты с ошибками */
$("btnTgChats").addEventListener("click", async () => {
  if ($("tgMtproxySwitch").checked) return;
  const btn = $("btnTgChats");
  const body = $("tgChatsBody");
  try {
    S.telegramChatsPending = true;
    btn.disabled = true;
    if (!await flushTelegramSettings()) return; // токен мог быть введён только что
    if ($("tgMtproxySwitch").checked) return;
    const res = await api("/api/telegram-chats");
    if ($("tgMtproxySwitch").checked) return;
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
    if (!await flushTelegramSettings()) return;
    const res = await api("/api/notify-test", { method: "POST", body: {}, timeout: 20000 });
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

async function retryConnection(options = {}) {
  const initial = options.initial === true;
  if (S.demo || document.hidden && !initial || connectionAttempt) return;
  clearConnectionRetry();
  const attempt = new AbortController();
  connectionAttempt = attempt;
  renderConnectionWarning();
  try {
    // A fresh authenticated snapshot proves recovery; health alone is not enough.
    const overview = await api("/api/overview", { timeout: initial ? 9000 : 5000, signal: attempt.signal });
    if (connectionAttempt !== attempt || document.hidden && !initial) return;
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
    if (connectionAttempt !== attempt || document.hidden && !initial) return;
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

function applyPlayers(data) { if (data.ok || !S.players?.ok) renderPlayers(data); else window.PlayersMap?.outage(); renderKpis(); }
function applyStats(data) { if (data.ok || !S.stats?.ok) renderStats(data); renderKpis(); }
const applyOps = renderOp;
function applyBackups(data) { if (data.ok || !backupsLoaded) renderBackups(data); }
function applyEvents(data) { if (data.ok || !eventsData?.ok) renderEvents(data); }

function applyLogs(data) {
  renderLogs(data);
}

async function refreshOverview() {
  try { applyOverview(await api("/api/overview")); }
  catch (e) { markConnFail(); }
}

async function refreshPlayers() {
  try { applyPlayers(await api("/api/players")); } catch { applyPlayers({ ok: false }); }
}

async function refreshStats() {
  try { applyStats(await api("/api/stats")); } catch { applyStats({ ok: false }); }
}

let logsRefreshSequence = 0;
let logsManualRefreshSequence = 0;
function updateLogsRefreshButton() {
  const unavailable = S.overview?.mode === "remote" || S.demo || document.body.classList.contains("is-booting");
  setDomProperty($("logsRefresh"), "disabled", unavailable || !!S.logsManualPending);
  setDomAttribute($("logsRefresh"), "aria-busy", String(!!S.logsManualPending));
}

async function refreshLogs({ manual = false } = {}) {
  const sequence = ++logsRefreshSequence;
  const manualToken = manual ? ++logsManualRefreshSequence : 0;
  const since = S.logsSince, until = S.logsUntil;
  S.logsRefreshing = true;
  if (manualToken) {
    S.logsManualPending = manualToken;
    updateLogsRefreshButton();
  }
  const params = new URLSearchParams();
  if (since) params.set("since", new Date(since).toISOString());
  if (until) params.set("until", new Date(until).toISOString());
  if (since) params.set("tail", "10000");
  try {
    const data = await api("/api/logs" + (params.size ? `?${params}` : ""));
    if (sequence === logsRefreshSequence && since === S.logsSince && until === S.logsUntil) applyLogs(data);
  } catch (e) {
    if (sequence === logsRefreshSequence && since === S.logsSince && until === S.logsUntil) applyLogs({ ok: false, error: I18n.t("Не удалось получить логи. Повторное подключение идёт автоматически.") });
  } finally {
    if (sequence === logsRefreshSequence) {
      S.logsRefreshing = false;
    }
    if (manualToken && S.logsManualPending === manualToken) {
      S.logsManualPending = 0;
      updateLogsRefreshButton();
    }
  }
}

async function refreshBackups() {
  try { applyBackups(await api("/api/backups")); } catch { applyBackups({ ok: false }); }
}

async function refreshEvents() {
  try { applyEvents(await api("/api/events?limit=200")); } catch { applyEvents({ ok: false }); }
}
document.addEventListener("click", async event => {
  const button = event.target.closest("[data-load-retry]");
  const load = { players: refreshPlayers, stats: refreshStats, backups: refreshBackups, events: refreshEvents }[button?.dataset.loadRetry];
  if (!load || button.disabled) return;
  button.disabled = true;
  try { await load(); } finally { if (button.isConnected) button.disabled = false; }
});

async function refreshOps() {
  const generation = opsGeneration;
  const request = ++opsRefreshSequence;
  const resolving = S.operationUncertain && S.pendingRequest;
  try {
    const response = resolving
      ? await api("/api/action", { method: "POST", body: { op: "resolve-request", ...resolving } })
      : await api("/api/ops");
    if (resolving && (response.error || response.ok !== true || typeof response.requestPending !== "boolean" || S.pendingRequest !== resolving)) return false;
    const op = resolving ? response.operationState : response;
    const settled = !!resolving && !response.requestPending && validOperationState(op) && !!op.snapshot;
    const supersededEpoch = request < opsAppliedRefresh && op?.snapshot?.epoch !== opsSnapshot?.epoch;
    if (generation !== opsGeneration || supersededEpoch || (!op?.snapshot && request < opsAppliedRefresh)) {
      if (settled) { clearPendingOperation(); updateButtons(); }
      return settled;
    }
    const applied = renderOp(op, { reconcilePending: !resolving || !response.requestPending, fromRefresh: true });
    // Admission proof remains valid even when newer live progress won the race.
    if (!applied && settled) { clearPendingOperation(); updateButtons(); }
    if (applied) opsAppliedRefresh = Math.max(opsAppliedRefresh, request);
    return applied || settled;
  } catch (e) { return false; }
}

function refreshAll() {
  refreshOverview(); refreshPlayers(); refreshStats(); refreshBackups(); refreshEvents(); refreshPlayersHistory(); refreshStatsHistory();
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
window.PlayersMap?.configure({
  request: () => api("/api/players/map/geometry", { timeout: 30000 }),
  demo: () => S.demo,
  filterChanged: () => { if (S.players) renderPlayers(S.players); },
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
mobileLayout.addEventListener("change", () => {
  $("moreMenu").hidden = true;
  $("navMore").setAttribute("aria-expanded", "false");
});
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
  const main = document.querySelector(".main"), flow = $("configFlow"), feedback = $("configFeedback");
  const startup = $("configStartup");
  if (editing) {
    const view = $("view-" + location.hash.replace(/^#\/?/, ""));
    view.before(startup);
    const card = view.querySelector(".editor-card");
    card.querySelector(".editor-tabs").before(flow);
  } else main.querySelector(".view").before(startup, flow);
  flow.before(feedback);
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
const modSettingsLink = document.createElement("a");
modSettingsLink.id = "modSettingsLink";
modSettingsLink.className = "btn";
modSettingsLink.href = "#/settings";
modSettingsLink.textContent = I18n.t("Настройки модов");
modSettingsLink.addEventListener("click", async event => {
  event.preventDefault();
  if (modSettingsLink.getAttribute("aria-disabled") === "true") return;
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
// Reuse the sidebar icons so secondary mobile routes share its visual language.
for (const link of $("moreMenu").querySelectorAll("a[data-route]")) {
  link.prepend(primaryNav.querySelector(`[data-route="${link.dataset.route}"] svg`).cloneNode(true));
}

let activeView = null;
let consoleBootLine = null;
let startupRouteLoad = null;

function currentRoute() {
  const h = location.hash.replace(/^#\/?/, "");
  return VIEWS[h] ? h : "overview";
}

function applyRoute() {
  const r = currentRoute();
  if (r === activeView) return;
  activeView = r;
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== "view-" + r; });
  window.PlayersMap?.route(r === "players");
  document.querySelectorAll(".nav a, .more-menu a").forEach((a) => {
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
  window.ConfigEditor?.route(r);
  startupRouteLoad?.(r);
  pollVisibleLogs();
  if (r === "console" && S.logsAuto) $("logsOut").scrollTop = $("logsOut").scrollHeight;
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
  const profileReason = () => window.ConfigEditor?.initialPending ? I18n.t("Профиль ещё загружается") : !window.ConfigEditor?.file ? I18n.t("Выберите доступный профиль") : "";
  const confirmedAction = id => async () => {
    await navigate("overview");
    const button = $(id);
    if (button.disabled) { toast(I18n.t("Действие сейчас недоступно. Проверьте состояние сервера."), "error"); button.closest("section").focus(); return; }
    button.focus(); button.click();
  };
  const entries = () => [
    ...Object.entries(VIEWS).map(([route, label]) => ({ label: I18n.t("Открыть: ") + label, hint: I18n.t("Раздел пульта"), run: () => navigate(route) })),
    { label: I18n.t("Найти настройку сервера"), hint: I18n.t("По названию или техническому ключу"), reason: profileReason(), run: settings("server") },
    { label: I18n.t("Найти настройку мира"), hint: I18n.t("Параметры Sandbox"), reason: profileReason(), run: settings("world") },
    { label: I18n.t("Найти мод в порядке загрузки"), hint: I18n.t("ModID или название; без изменения порядка"), reason: profileReason() || (!window.ConfigEditor?.modsReady ? I18n.t("Состав модов ещё не загружен") : ""), run: async () => { await navigate("mods"); await window.ConfigEditor.focusModOrder(); } },
    { label: I18n.t("Найти строку в логах"), hint: I18n.t("Перейти к фильтру логов"), reason: $("logsFilter").disabled ? I18n.t("Логи контейнера недоступны в demo и remote") : "", run: async () => { await navigate("console"); $("logsFilter").focus(); } },
    { label: I18n.t("Посмотреть текущую операцию"), hint: I18n.t("Фаза, время и переход к логам"), reason: !S.op?.active ? I18n.t("Сейчас нет активной операции") : "", run: () => { $("opbar").tabIndex = -1; $("opbar").focus(); } },
    { label: I18n.t("Скопировать имя профиля"), hint: I18n.t("Имя файла конфигурации"), reason: profileReason(), run: () => copyText(window.ConfigEditor.file) },
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
  bind("ops", data => renderOp(data, { fromStream: true }));
  bind("stats-history", (d) => renderStatsHistory(d.points || []));
  bind("players-history", (d) => renderPlayersHistory(d.points || []));
  // браузер сам переподключается; откат на опрос — если поток так и не ожил
  // или умер уже после того, как работал
  es.onerror = () => {
    if (document.hidden || liveSource !== es) return;
    window.PlayersMap?.outage();
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
    updateOperationCountdown();
    retryConnection();
  }
});

async function revealDashboard() {
  const loader = $("startupLoader");
  document.body.classList.add("is-opening");
  // Resolve the inert layout once: fonts and the exit must not depend on rAF.
  document.body.getBoundingClientRect();
  // The revealed inert layout requests its own fonts; unavailable fonts use fallback.
  let fontTimer;
  await Promise.race([
    document.fonts?.ready?.catch(() => {}),
    new Promise(resolve => { fontTimer = setTimeout(resolve, 300); }),
  ]);
  clearTimeout(fontTimer);
  loader.querySelector(".startup-content").setAttribute("aria-busy", "false");
  loader.setAttribute("aria-busy", "false");
  if (!matchMedia("(prefers-reduced-motion: reduce)").matches) {
    await new Promise(resolve => {
      const finish = event => {
        if (event && (event.target !== loader || event.propertyName !== "opacity")) return;
        clearTimeout(timer);
        loader.removeEventListener("transitionend", finish);
        resolve();
      };
      const timer = setTimeout(finish, 230);
      loader.addEventListener("transitionend", finish);
      loader.dataset.state = "leaving";
    });
  }
  loader.hidden = true;
  $("dashboardShell").inert = false;
  document.body.classList.remove("is-booting", "is-opening");
  if (activeView === "console" && S.logsAuto) $("logsOut").scrollTop = $("logsOut").scrollHeight;
  updateButtons();
  $("view-" + activeView)?.focus({ preventScroll: true });
}

async function boot() {
  let deadlineTimer;
  const deadline = new Promise(resolve => { deadlineTimer = setTimeout(resolve, 15000); });
  setInterval(() => {
    if (document.hidden) return;
    updateConnectionWarning();
  }, 5000);
  setInterval(() => { if (!document.hidden) updateOperationCountdown(); }, 1000);
  consoleBootLine = consoleAppend(I18n.t("Пульт подключается к серверу…"), "c-dim");
  applyRoute();
  let liveReady = false;
  if (S.demo) enterDemo();
  const connection = (async () => {
    if (S.demo) return refreshOverview();
    try {
      const h = await api("/api/health", { timeout: 3500 });
      if (!h.ok) throw new Error("no health");
      liveReady = true;
    } catch {
      // The snapshot is still useful when a separate health probe is unavailable.
    }
  })();
  // The initial snapshot cannot depend on the timing or completeness of SSE.
  // Load the visible route and shared state together; later updates stay live.
  const routeLoads = {
    overview: [refreshPlayers, refreshStats, refreshBackups, refreshEvents, refreshPlayersHistory, refreshStatsHistory],
    players: [refreshPlayers, refreshPlayersHistory],
    backups: [refreshBackups],
    events: [refreshEvents],
    console: [refreshLogs],
  };
  const routeRequests = new Map();
  const loadRoute = route => {
    for (const load of routeLoads[route] || []) {
      if (!routeRequests.has(load)) routeRequests.set(load, load());
    }
    return Promise.allSettled((routeLoads[route] || []).map(load => routeRequests.get(load)));
  };
  startupRouteLoad = loadRoute;
  const shared = Promise.allSettled([connection, S.demo ? null : retryConnection({ initial: true }), refreshOps(), window.ConfigEditor?.ready]);
  loadRoute(activeView);
  const initial = (async () => {
    await shared;
    let route;
    do {
      route = activeView;
      await loadRoute(route);
    } while (route !== activeView);
  })();
  await Promise.race([initial, deadline]);
  clearTimeout(deadlineTimer);
  // Leave the original editor requests running; their transport budget is unchanged.
  startupRouteLoad = null;
  await revealDashboard();
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
