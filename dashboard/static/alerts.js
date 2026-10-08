"use strict";

// Update and connection notices share markup, placement and mobile clearance.
window.FloatingAlerts = (() => {
  const alerts = [];
  function layout() {
    let offset = 0;
    for (const { element } of alerts) {
      element.style.setProperty("--floating-alert-offset", `${offset}px`);
      if (!element.hidden) offset += element.offsetHeight + 12;
    }
    document.documentElement.style.setProperty("--pwa-update-clearance", `${offset}px`);
  }
  const resize = new ResizeObserver(layout);
  const visibility = new MutationObserver(layout);
  function create({ id, className = "", actionId, noteId, icon = "" }) {
    const element = document.createElement("aside");
    element.className = `floating-alert ${className}`.trim();
    if (id) element.id = id;
    element.hidden = true;
    element.innerHTML = '<p class="pwa-update-message" role="status" aria-live="polite"></p><button class="pwa-update-action" type="button"><span></span></button><p class="pwa-update-note" role="status" aria-live="polite" hidden></p>';
    const action = element.querySelector("button");
    action.id = actionId;
    action.insertAdjacentHTML("beforeend", icon);
    const note = element.querySelector(".pwa-update-note");
    if (noteId) note.id = noteId;
    const alert = { element, action, label: action.querySelector("span"), message: element.querySelector(".pwa-update-message"), note };
    alerts.push(alert);
    // Dashboard startup protects dynamically created controls as well.
    (document.getElementById("dashboardShell") || document.body).append(element);
    resize.observe(element);
    visibility.observe(element, { attributes: true, attributeFilter: ["hidden"] });
    return alert;
  }
  return { create };
})();
