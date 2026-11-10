/* Local game geometry and independently expiring player observations. */
"use strict";
window.PlayersMap = (() => {
  const el = id => document.getElementById(id);
  const viewport = el("playerMapViewport"), canvas = el("playerMapCanvas"), markers = el("playerMapMarkers");
  const ctx = canvas.getContext("2d");
  const text = (node, value) => { if (node.textContent !== value) node.textContent = value; };
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const t = source => I18n.t(source);
  let options = {}, frame = null, snapshot = null, receivedAt = 0, transportLost = false;
  let active = false, query = "", selected = null, geometry = null, camera = null;
  let requestSequence = 0, pending = false, geometryProfile = null, loadedAt = 0;
  let width = 0, height = 0, raf = null, drag = null, synthetic = false, paths = [];
  const nodes = new Map();
  const reasonText = {
    "profile-unavailable": "Активный профиль сервера не определён.",
    "integration-missing": "Для координат игроков подключите мод карты.",
    "snapshot-invalid": "Снимок координат повреждён. Ожидается следующий снимок.",
    "snapshot-before-start": "Ожидаются координаты после запуска сервера.",
    "snapshot-future": "Время снимка опережает время сервера. Проверьте часы сервера.",
    "map-files-missing": "Файлы карты не найдены. Подключите каталог игры /server-files.",
    "cell-size-required": "Размер ячейки карты не определён. Укажите PZ_MAP_CELL_SIZE: 256 или 300.",
    "unsupported-map-schema": "Этот формат карты пока не поддерживается.",
    "map-invalid": "Файл карты повреждён или недоступен.",
    "map-budget-exceeded": "Карта показана частично: достигнут предел размера.",
    "map-overlap-unverified": "Наложение мод-карт не проверено. Геометрия может перекрываться.",
  };
  function positions() {
    const online = new Set(frame?.ok ? frame.names || [] : []);
    const seen = new Set();
    return (snapshot?.positions || []).filter(p => {
      if (!p || typeof p.name !== "string" || !online.has(p.name) || seen.has(p.name) || ![p.x, p.y, p.z].every(finite)) return false;
      seen.add(p.name);
      return !query || p.name.toLocaleLowerCase().includes(query);
    });
  }
  function fresh() {
    return !transportLost && snapshot?.state === "fresh" && finite(snapshot.ageMs) &&
      snapshot.ageMs + Math.max(0, performance.now() - receivedAt) <= Math.min(15000, snapshot.maxAgeMs || 15000);
  }
  function pointBounds(points) {
    if (!points.length) return null;
    const xs = points.map(p => p.x), ys = points.map(p => p.y);
    return { minX: Math.min(...xs) - 100, maxX: Math.max(...xs) + 100, minY: Math.min(...ys) - 100, maxY: Math.max(...ys) + 100 };
  }
  function validBounds(b) { return b && [b.minX, b.minY, b.maxX, b.maxY].every(finite) && b.maxX > b.minX && b.maxY > b.minY; }
  function fit(bounds) {
    if (!validBounds(bounds) || !width || !height) return;
    camera = { x: (bounds.minX + bounds.maxX) / 2, y: (bounds.minY + bounds.maxY) / 2,
      scale: Math.min((width - 64) / (bounds.maxX - bounds.minX), (height - 64) / (bounds.maxY - bounds.minY)) };
    camera.scale = Math.max(.0001, Math.min(20, camera.scale));
    schedule();
  }
  function worldBounds() { return validBounds(geometry?.bounds) ? geometry.bounds : pointBounds(positions()); }
  function project(x, y) { return [width / 2 + (x - camera.x) * camera.scale, height / 2 + (y - camera.y) * camera.scale]; }
  function schedule() { if (active && !raf) raf = requestAnimationFrame(() => { raf = null; draw(); }); }
  function draw() {
    const rect = viewport.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    width = rect.width; height = rect.height;
    const ratio = Math.min(devicePixelRatio || 1, 2);
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) {
      canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
    }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (!camera) {
      const b = worldBounds();
      if (b) { fit(b); if (!camera) return; }
    }
    if (!camera) { nodes.forEach(n => n.hidden = true); return; }
    const colors = { water: "#263d41", forest: "#263024", road: "#897c60", building: "#625943", other: "#343026" };
    ctx.setTransform(ratio * camera.scale, 0, 0, ratio * camera.scale, ratio * (width / 2 - camera.x * camera.scale), ratio * (height / 2 - camera.y * camera.scale));
    for (const feature of paths) {
      ctx.fillStyle = colors[feature.kind] || colors.other;
      ctx.strokeStyle = colors[feature.kind] || colors.other;
      ctx.lineWidth = (feature.kind === "road" ? 2 : 1) / camera.scale;
      if (feature.polygon) ctx.fill(feature.path, "evenodd"); else ctx.stroke(feature.path);
    }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    const desired = new Set();
    for (const p of positions()) {
      desired.add(p.name);
      let node = nodes.get(p.name);
      if (!node) {
        node = document.createElement("button"); node.type = "button"; node.className = "player-map-marker";
        const dot = document.createElement("span"); dot.className = "player-map-pin"; dot.setAttribute("aria-hidden", "true");
        const label = document.createElement("span"); label.className = "player-map-label"; label.textContent = p.name;
        node.append(dot, label); node.addEventListener("click", () => choose(p.name));
        nodes.set(p.name, node); markers.append(node);
      }
      const [x, y] = project(p.x, p.y);
      node.hidden = x < 16 || y < 16 || x > width - 16 || y > height - 16;
      node.style.left = `${x}px`; node.style.top = `${y}px`;
      node.classList.toggle("is-selected", selected === p.name);
      node.classList.toggle("is-stale", !fresh());
      const label = `${t("На карте")}: ${p.name}, X ${Math.round(p.x)}, Y ${Math.round(p.y)}, Z ${p.z}${fresh() ? "" : `, ${t("Координаты устарели")}`}`;
      if (node.getAttribute("aria-label") !== label) node.setAttribute("aria-label", label);
      node.setAttribute("aria-pressed", String(selected === p.name));
    }
    for (const [name, node] of nodes) if (!desired.has(name)) { node.remove(); nodes.delete(name); }
    const tiles = 10 ** Math.floor(Math.log10(100 / camera.scale));
    text(el("playerMapScale"), `${tiles.toLocaleString(I18n.locale)} ${t("клеток")}`);
    el("playerMapScale").style.setProperty("--scale-width", `${Math.max(20, tiles * camera.scale)}px`);
  }
  function choose(name, locate = false) {
    selected = name;
    const p = positions().find(p => p.name === name);
    if (p && locate && camera) { camera.x = p.x; camera.y = p.y; camera.scale = Math.max(camera.scale, .5); }
    renderStatus(); syncRoster(); schedule();
  }
  function syncRoster() {
    const byName = new Map(positions().map(p => [p.name, p]));
    for (const row of el("playersBody").querySelectorAll("[data-row-key]")) {
      const p = byName.get(row.dataset.rowKey), button = row.querySelector("[data-locate]");
      if (!button) continue;
      if (button.disabled === !!p) button.disabled = !p;
      const value = p ? `X ${Math.round(p.x)} · Y ${Math.round(p.y)} · Z ${p.z}` : t("Позиция недоступна");
      const label = p ? `${t("На карте")} · ${value}` : value;
      text(button, label);
      const selectedNow = selected === row.dataset.rowKey;
      if (row.classList.contains("is-map-selected") !== selectedNow) row.classList.toggle("is-map-selected", selectedNow);
    }
  }
  function renderStatus() {
    const list = positions(), live = fresh(), total = frame?.ok ? frame.count || 0 : 0;
    const allLocated = (snapshot?.positions || []).filter(p => frame?.names?.includes(p.name)).length;
    text(el("playerMapCoverage"), frame?.ok ? I18n.t("На карте: {{0}} из {{1}}", [allLocated, total]) : "");
    const state = live ? "fresh" : snapshot?.positions?.length ? "stale" : snapshot?.state || "setup";
    viewport.dataset.state = state;
    let message = !frame?.ok || transportLost ? t("Связь потеряна. Координаты не подтверждены.") :
      state === "stale" ? t("Координаты устарели") : total === 0 ? t("Нет игроков онлайн.") :
      live ? t("Координаты обновляются каждые 5 секунд") : t(reasonText[snapshot?.reason] || "Для координат игроков подключите мод карты.");
    if (synthetic) message = t("Демо: карта и координаты вымышлены.");
    text(el("playerMapStatus"), message);
    text(el("playerMapTime"), finite(snapshot?.capturedAt) ? new Date(snapshot.capturedAt).toLocaleString(I18n.locale) : "");
    const hasGeometry = !!geometry?.features?.length;
    const geometryMessage = pending ? t("Загрузка карты…") : geometry?.reason === "map-overlap-unverified" ? t(reasonText[geometry.reason]) : !hasGeometry ? t(reasonText[geometry?.reason] || "Файлы карты не найдены. Подключите каталог игры /server-files.") :
      geometry.state === "partial" || geometry.truncated ? t("Карта показана частично.") : "";
    text(el("playerMapGeometryStatus"), geometryMessage); el("playerMapGeometryStatus").hidden = !geometryMessage;
    el("playerMapPlaceholder").hidden = hasGeometry || list.length > 0;
    text(el("playerMapPlaceholder"), pending ? t("Загрузка карты…") : t("Карта пока недоступна"));
    el("playerMapSetup").hidden = synthetic || (hasGeometry && (live || total === 0 && snapshot?.state === "empty"));
    const p = list.find(p => p.name === selected);
    el("playerMapSelection").hidden = !p;
    if (p) {
      text(el("playerMapSelectedName"), p.name);
      text(el("playerMapSelectedPosition"), `X ${Math.round(p.x)} · Y ${Math.round(p.y)} · Z ${p.z}${live ? "" : ` · ${t("Координаты устарели")}`}`);
    }
    for (const id of ["playerMapZoomIn", "playerMapZoomOut", "playerMapFit"]) el(id).disabled = !worldBounds();
    el("playerMapFitPlayers").disabled = !list.length;
    el("playerMapRefresh").disabled = pending;
  }
  async function load(force = false) {
    if (!active || pending || !options.request || !force && geometry && geometryProfile === snapshot?.profile && Date.now() - loadedAt < 60000) return;
    const sequence = ++requestSequence, profile = snapshot?.profile || null;
    geometryProfile = profile; pending = true; renderStatus();
    try {
      const data = options.demo?.() ? demoGeometry() : await options.request();
      if (sequence !== requestSequence || profile !== (snapshot?.profile || null) || !active) return;
      const firstGeometry = !geometry;
      geometry = data && typeof data === "object" ? data : { state: "error" };
      if (data.profile && profile && data.profile !== profile) geometry = { state: "setup", reason: "profile-unavailable" };
      loadedAt = Date.now();
      paths = (geometry.features || []).map(feature => {
        const g = feature.geometry || {}, path = new Path2D(), polygon = g.type === "Polygon";
        for (const ring of polygon ? g.coordinates || [] : g.type === "LineString" ? [g.coordinates || []] : []) {
          let first = true;
          for (const point of ring) {
            if (!Array.isArray(point) || !finite(point[0]) || !finite(point[1])) continue;
            if (first) { path.moveTo(point[0], point[1]); first = false; } else path.lineTo(point[0], point[1]);
          }
          if (polygon) path.closePath();
        }
        return { path, polygon, kind: feature.kind };
      });
      if (firstGeometry && validBounds(geometry.bounds)) camera = null;
      schedule();
    } catch { if (sequence === requestSequence) geometry = { state: "error", reason: "map-invalid" }; }
    finally { if (sequence === requestSequence) { pending = false; renderStatus(); schedule(); } }
  }
  function update(data) {
    const oldProfile = snapshot?.profile, oldSession = snapshot?.sessionId;
    const newFrame = data !== frame;
    frame = data;
    if (!data.ok) { transportLost = true; renderStatus(); schedule(); return; }
    synthetic = !!options.demo?.();
    const next = synthetic && !data.map ? demoSnapshot(data.names || []) : data.map;
    // Re-rendering a retained frame must never restart its observation lifetime.
    if (next !== snapshot) { snapshot = next || null; receivedAt = performance.now(); }
    if (newFrame) transportLost = false;
    if (oldProfile !== snapshot?.profile || oldSession && oldSession !== snapshot?.sessionId) {
      camera = null; geometry = null; paths = []; selected = null; requestSequence++; pending = false;
      nodes.forEach(node => node.remove()); nodes.clear();
    }
    if (selected && !(data.names || []).includes(selected)) selected = null;
    renderStatus(); schedule();
    if (active && (!geometry || Date.now() - loadedAt > 60000)) load();
  }
  function zoom(factor) { if (camera) { camera.scale = Math.max(.0001, Math.min(20, camera.scale * factor)); schedule(); } }
  function demoSnapshot(names) {
    return { state: "fresh", profile: "demo", capturedAt: Date.now(), ageMs: 0, maxAgeMs: 15000, sessionId: "demo",
      positions: names.map((name, i) => ({ name, x: 200 + i * 210, y: 250 + i % 2 * 260, z: 0 })) };
  }
  function demoGeometry() {
    return { ok: true, state: "ready", revision: "synthetic-demo", bounds: { minX: 0, minY: 0, maxX: 1000, maxY: 800 }, features: [
      { kind: "forest", geometry: { type: "Polygon", coordinates: [[[0, 0], [1000, 0], [1000, 800], [0, 800], [0, 0]]] } },
      { kind: "water", geometry: { type: "Polygon", coordinates: [[[0, 30], [240, 180], [420, 160], [700, 30], [1000, 180], [1000, 260], [700, 100], [450, 230], [220, 250], [0, 110], [0, 30]]] } },
      ...[220, 450, 680].map(x => ({ kind: "road", geometry: { type: "LineString", coordinates: [[x, 220], [x, 760]] } })),
      { kind: "road", geometry: { type: "LineString", coordinates: [[80, 400], [920, 400]] } },
      ...[260, 350, 490, 580, 720].map(x => ({ kind: "building", geometry: { type: "Polygon", coordinates: [[[x, 300], [x + 40, 300], [x + 40, 350], [x, 350], [x, 300]]] } })),
    ] };
  }
  el("playerQuery").addEventListener("input", event => { query = event.target.value.trim().toLocaleLowerCase(); options.filterChanged?.(); schedule(); });
  el("playersBody").addEventListener("click", event => { const button = event.target.closest("[data-locate]"); if (button && !button.disabled) choose(button.dataset.locate, true); });
  el("playerMapZoomIn").addEventListener("click", () => zoom(1.5));
  el("playerMapZoomOut").addEventListener("click", () => zoom(1 / 1.5));
  el("playerMapFit").addEventListener("click", () => fit(worldBounds()));
  el("playerMapFitPlayers").addEventListener("click", () => fit(pointBounds(positions())));
  el("playerMapRefresh").addEventListener("click", () => load(true));
  viewport.addEventListener("keydown", event => {
    if (event.target !== viewport || !camera) return;
    const moves = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (moves[event.key]) { event.preventDefault(); camera.x += moves[event.key][0] * 60 / camera.scale; camera.y += moves[event.key][1] * 60 / camera.scale; schedule(); }
    else if (["+", "=", "-"].includes(event.key)) { event.preventDefault(); zoom(event.key === "-" ? 1 / 1.5 : 1.5); }
    else if (event.key === "Home") { event.preventDefault(); fit(worldBounds()); }
  });
  viewport.addEventListener("pointerdown", event => { if (!camera || event.target.closest("button")) return; drag = { id: event.pointerId, x: event.clientX, y: event.clientY, cx: camera.x, cy: camera.y }; viewport.setPointerCapture(event.pointerId); });
  viewport.addEventListener("pointermove", event => { if (drag?.id !== event.pointerId || !camera) return; camera.x = drag.cx - (event.clientX - drag.x) / camera.scale; camera.y = drag.cy - (event.clientY - drag.y) / camera.scale; schedule(); });
  for (const type of ["pointerup", "pointercancel"]) viewport.addEventListener(type, () => { drag = null; });
  viewport.addEventListener("wheel", event => { if (!camera || !event.ctrlKey) return; event.preventDefault(); zoom(event.deltaY > 0 ? 1 / 1.15 : 1.15); }, { passive: false });
  new ResizeObserver(schedule).observe(viewport);
  const expire = () => { if (active) { renderStatus(); schedule(); } };
  setInterval(expire, 1000);
  document.addEventListener("visibilitychange", expire);
  window.addEventListener("offline", () => { transportLost = true; expire(); });
  return {
    configure(value) { options = value; }, update, syncRoster,
    filterNames(names) { return names.filter(name => !query || name.toLocaleLowerCase().includes(query)); },
    outage() { transportLost = true; renderStatus(); schedule(); },
    route(value) { active = value; if (value) { renderStatus(); schedule(); load(); } else { requestSequence++; pending = false; } },
  };
})();
