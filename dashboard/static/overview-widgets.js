"use strict";

window.OverviewWidgets = (() => {
  const byId = id => document.getElementById(id);
  const grid = byId("overviewWidgets");
  const widgets = new Map([...grid.children].map(element => [
    element.dataset.overviewWidget, { element, label: element.querySelector("[data-widget-label]").textContent },
  ]));
  const defaults = [...widgets.keys()];
  const storageKey = "pz.overview.layout";
  const version = 1;
  const dialog = byId("overviewCustomizeDialog");
  const activeList = byId("overviewVisibleWidgets");
  const availableList = byId("overviewAvailableWidgets");
  const announcement = byId("overviewLayoutAnnouncement");
  const done = byId("btnDoneOverviewLayout");
  const icons = {
    earlier: "M12 19V5m-6 6 6-6 6 6",
    later: "M12 5v14m-6-6 6 6 6-6",
    remove: "M5 12h14",
  };
  let visible = readLayout();
  let statusRevealed = false;
  let opener = null;

  function readLayout() {
    try {
      const stored = JSON.parse(localStorage.getItem(storageKey));
      if (stored?.version === version && Array.isArray(stored.visible)) {
        const known = [...new Set(stored.visible.filter(id => widgets.has(id)))];
        if (known.length || !stored.visible.length) return known;
      }
    } catch { /* The browser may block storage or contain malformed JSON. */ }
    return defaults;
  }

  function renderLayout() {
    const shown = statusRevealed ? ["status", ...visible] : visible;
    const hidden = defaults.filter(id => !shown.includes(id));
    [...shown, ...hidden].forEach((id, index) => {
      const { element } = widgets.get(id);
      element.hidden = index >= shown.length;
      // Move the existing card so visual order and keyboard order agree.
      if (grid.children[index] !== element) grid.insertBefore(element, grid.children[index]);
    });
    grid.hidden = !shown.length;
    byId("overviewEmpty").hidden = !!shown.length;
  }

  function actionButton(action, id, label, disabled = false) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = action === "add" ? "btn" : "btn ghost overview-widget-icon";
    button.dataset.widgetAction = action;
    button.dataset.widgetId = id;
    button.disabled = disabled;
    button.setAttribute("aria-label", label);
    if (action === "add") button.textContent = I18n.t("Добавить");
    else {
      button.title = label;
      button.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${icons[action]}"/></svg>`;
    }
    return button;
  }

  function widgetRow(id, index) {
    const row = document.createElement("li");
    const name = document.createElement("span");
    const { label } = widgets.get(id);
    name.textContent = label;
    row.append(name);
    if (index === undefined) {
      row.append(actionButton("add", id, I18n.msg`Добавить виджет «${label}»`));
    } else {
      const actions = document.createElement("div");
      actions.className = "overview-widget-actions";
      actions.append(
        actionButton("earlier", id, I18n.msg`Переместить «${label}» выше`, index === 0),
        actionButton("later", id, I18n.msg`Переместить «${label}» ниже`, index === visible.length - 1),
        actionButton("remove", id, I18n.msg`Убрать виджет «${label}»`),
      );
      row.append(actions);
    }
    return row;
  }

  function renderManager() {
    activeList.replaceChildren(...visible.map(widgetRow));
    const available = defaults.filter(id => !visible.includes(id));
    availableList.replaceChildren(...available.map(id => widgetRow(id)));
    byId("overviewNoVisible").hidden = !!visible.length;
    byId("overviewAvailableSection").hidden = !available.length;
  }

  function setLayout(next, message) {
    visible = next;
    statusRevealed = false;
    const notice = byId("overviewStorageNotice");
    try {
      localStorage.setItem(storageKey, JSON.stringify({ version, visible }));
      notice.hidden = true;
    } catch {
      notice.hidden = false;
    }
    renderLayout();
    renderManager();
    announcement.textContent = notice.hidden ? message : `${message}. ${notice.textContent}`;
  }

  function enabledAction(action, id) {
    if (!id) return null;
    return dialog.querySelector(`[data-widget-action="${action}"][data-widget-id="${id}"]:not(:disabled)`);
  }

  dialog.addEventListener("click", event => {
    const button = event.target.closest("[data-widget-action]");
    if (!button || button.disabled) return;
    const { widgetAction: action, widgetId: id } = button.dataset;
    const { label } = widgets.get(id);
    if (action === "add") {
      setLayout([...visible, id], I18n.msg`Виджет «${label}» добавлен`);
      (availableList.querySelector("button") || enabledAction("remove", id) || done).focus();
      return;
    }
    const index = visible.indexOf(id);
    const next = [...visible];
    if (action === "remove") {
      next.splice(index, 1);
      setLayout(next, I18n.msg`Виджет «${label}» убран`);
      (enabledAction("remove", next[index] || next[index - 1]) || enabledAction("add", id) || done).focus();
      return;
    }
    const destination = index + (action === "earlier" ? -1 : 1);
    [next[index], next[destination]] = [next[destination], next[index]];
    setLayout(next, I18n.msg`«${label}» · место ${destination + 1} из ${next.length}`);
    (enabledAction(action, id) || enabledAction(action === "earlier" ? "later" : "earlier", id)).focus();
  });

  function close(restoreFocus = true) {
    if (!dialog.open) return;
    dialog.close();
    if (restoreFocus) {
      const target = opener?.closest("[hidden]") ? byId("btnCustomizeOverview") : opener;
      target?.focus({ preventScroll: true });
    }
    opener = null;
  }

  grid.closest(".view").addEventListener("click", event => {
    const button = event.target.closest("#btnCustomizeOverview, #btnCustomizeOverviewEmpty");
    if (!button || dialog.open || !byId("modalRoot").hidden || byId("commandDialog").open) return;
    opener = button;
    announcement.textContent = "";
    renderManager();
    dialog.showModal();
  });
  byId("btnResetOverviewLayout").addEventListener("click", () => {
    setLayout(defaults, I18n.t("Восстановлена исходная раскладка"));
  });
  done.addEventListener("click", () => close());
  dialog.addEventListener("cancel", event => { event.preventDefault(); close(); });

  renderLayout();
  return Object.freeze({
    leave() {
      close(false);
      if (!statusRevealed) return;
      statusRevealed = false;
      renderLayout();
    },
    revealStatus() {
      if (statusRevealed || visible.includes("status")) return;
      statusRevealed = true;
      renderLayout();
    },
  });
})();
