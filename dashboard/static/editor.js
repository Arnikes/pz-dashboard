/* Profile-scoped editors. Form edits persist drafts; only operations write PZ files. */
"use strict";
window.ConfigEditor = (() => {
  let file = "", draft = null, mods = null, configTab = "server", modTab = "composition";
  let chain = Promise.resolve(), loading = false, sourceDirty = false, fieldDirty = false;
  let previousOp = false, lastRefresh = 0, dragId = null, pendingPatches = 0;
  let unsaved = [];
  let operationMarkup = "";
  const SECRET = "__PZ_SECRET_UNCHANGED__";
  const statuses = { draft: "Есть черновик", saved: "Сохранено, требуется запуск", applying: "Применение", applied: "Применено", error: "Ошибка", unconfirmed: "Применение не подтверждено", "select-mods": "Пакеты загружены; выберите ModID" };
  const call = async (path, body) => {
    const result = await api(path, body === undefined ? { timeout: 180000 } : { method: "POST", body, timeout: 180000 });
    if (!result.ok) { const failure = new Error(result.error || "Нет ответа редактора"); failure.remote = true; throw failure; }
    return result;
  };
  function error(message) {
    $("configError").textContent = message;
    $("configError").hidden = false;
    toast(message, "error");
  }
  function clearError() { $("configError").hidden = true; }
  function updateBar() {
    if (!draft) { $("draftBar").hidden = true; return; }
    const busy = !!S.op?.active;
    const editorView = ["settings", "mods"].includes(activeView);
    $("draftBar").hidden = !editorView;
    $("draftRetry").hidden = !unsaved.length;
    $("draftLabel").textContent = (statuses[draft.status] || "Конфигурация") + (draft.changed ? ` · изменённых строк: ${draft.changedLines || 1}` : "");
    $("configStatus").textContent = statuses[draft.status] || "Конфигурация";
    $("configStatus").dataset.state = draft.conflict || draft.status === "error" ? "bad" : draft.changed ? "warn" : "ok";
    $("configApply").disabled = busy || !draft.canApply || !draft.canWrite || S.demo || loading || fieldDirty;
    $("configSave").disabled = busy || !draft.canWrite || !!S.overview?.containerInfo?.running || S.demo || loading || fieldDirty;
    $("configDiscard").disabled = busy || S.demo || loading;
    $("configApply").title = draft.canApply ? "" : "Выбран неактивный или неподтверждённый профиль";
    $("configSave").title = S.overview?.containerInfo?.running ? "Сначала остановите сервер" : "";
    if (busy) $("draftSaved").textContent = `${S.op.active.phase || "Операция"} · ${S.op.active.message || ""}`;
    $("configFields").querySelectorAll("[data-key]").forEach(el => { el.disabled = busy || S.demo || !!el.dataset.owner; });
    $("configActive").textContent = draft.canApply ? "Активный профиль" : "Редактирование другого / неподтверждённого профиля";
    $("configVersion").textContent = draft.version ? `Версия ${draft.version}` : "B42 · версия неизвестна";
    if (draft.conflict) {
      $("configError").textContent = "Рабочие файлы изменились. Посмотрите конфликт; отмена черновика загрузит свежие файлы.";
      $("configError").hidden = false;
    }
    if (draft.dataDiagnostic) {
      $("configError").textContent = draft.dataDiagnostic;
      $("configError").hidden = false;
    }
    $("configVersionDiagnostic").hidden = !draft.versionDiagnostic;
    $("configVersionDiagnostic").textContent = draft.versionDiagnostic || "";
    const state = draft.state || {};
    const result = $("configOperationResult");
    result.hidden = !editorView || !state.operationStartedAt;
    if (!result.hidden) {
      const status = busy ? "applying" : state.status;
      result.dataset.state = status === "error" ? "bad" : "ok";
      const markup = `<strong>${esc(statuses[status] || "Результат операции")}</strong><p class="hint">Профиль ${esc(file)} · ${esc(fmtTime(state.operationCompletedAt || state.operationStartedAt))}</p>${state.error ? `<p class="editor-error">${esc(state.error)}</p>` : ""}${(state.verificationProblems || []).filter(p => p.severity !== "error").map(p => `<p class="hint">${esc(p.message)}</p>`).join("")}${state.worldBackup ? `<p class="hint">Бэкап мира: ${esc(state.worldBackup)}</p>` : ""}<div class="editor-toolbar"><button type="button" class="btn small" data-operation-logs>Логи операции</button>${status === "error" && state.historyId ? '<button type="button" class="btn small" data-operation-restore>Восстановить прежнюю конфигурацию…</button>' : ""}</div>`;
      if (markup !== operationMarkup) { result.innerHTML = markup; operationMarkup = markup; }
    }
    attention();
  }
  function attention() {
    const messages = [];
    if (draft?.changed) messages.push("Есть неприменённый черновик конфигурации");
    if (draft?.status === "saved") messages.push("Настройки сохранены и ожидают запуска сервера");
    if (draft?.state?.installation) messages.push("Установка Workshop не завершена: загрузите пакеты и выберите ModID");
    if (draft?.state?.error) messages.push(draft.state.error);
    const backups = S.backupsItems || [];
    if (!backups.length && S.overview?.backupsCount === 0) messages.push("Нет резервной копии мира");
    if (backups.length && Math.max(...backups.map(b => Date.parse(b.mtime) || 0)) < Date.now() - 48 * 3600000) messages.push("Последнему бэкапу больше двух суток");
    $("editorAttention").hidden = activeView !== "overview" || !messages.length;
    $("editorAttention").innerHTML = `<strong>Требуют внимания</strong>${messages.map(m => `<p>${esc(m)}</p>`).join("")}<a href="#/settings">Открыть настройки</a> · <a href="#/mods">Открыть моды</a>`;
  }
  async function loadProfile(next) {
    loading = true;
    updateBar();
    try {
      const data = await call(`/api/config-draft?file=${encodeURIComponent(next)}`);
      file = data.file;
      draft = data;
      $("configProfile").value = file;
      sourceDirty = fieldDirty = false;
      clearError();
      renderFields();
      renderSources();
      await loadMods();
    } finally { loading = false; updateBar(); }
  }
  async function init() {
    try {
      const data = await call("/api/server-configs");
      const profiles = data.profiles || [];
      $("configProfile").innerHTML = `<option value="">Выберите профиль…</option>` + profiles.map(p => `<option value="${esc(p.file)}">${esc(p.file)}${p.active ? " · активный" : ""}</option>`).join("");
      let preferred = "";
      try { preferred = localStorage.getItem("pz-config-profile") || ""; } catch (_) { /* optional preference */ }
      const next = profiles.some(p => p.file === preferred) ? preferred : data.activeFile || (profiles.length === 1 ? profiles[0].file : "");
      if (next && profiles.some(p => p.file === next)) await loadProfile(next);
      if (!profiles.length) $("configFields").innerHTML = '<p class="hint">В каталоге Server нет доступных .ini. Проверьте монтирование данных сервера.</p>';
    } catch (e) {
      $("configProfile").innerHTML = '<option value="">Профили недоступны</option>';
      $("configFields").textContent = e.message;
    }
  }
  function patch(body, redraw = true) {
    if (sourceDirty && !(body && typeof body === "object" && (body.texts || body.discard === true))) return flushSources().then(() => patch(body, redraw));
    const target = file;
    pendingPatches++;
    fieldDirty = true;
    $("draftSaved").textContent = "Сохраняется черновик…";
    updateBar();
    const task = chain.then(async () => {
      if (!draft || target !== file) throw new Error("Профиль изменился во время редактирования");
      const update = typeof body === "function" ? body() : body;
      const updates = [...unsaved, update];
      unsaved = [];
      try {
        while (updates.length) {
          draft = await call("/api/config-draft", { file, draftRevision: draft.draftRevision, ...updates[0] });
          updates.shift();
        }
      } catch (failure) { if (!failure.remote) unsaved = updates; throw failure; }
      clearError();
      $("draftSaved").textContent = "Черновик сохранён";
      if (redraw) { renderFields(); renderSources(); await loadMods(); }
      return draft;
    });
    chain = task.catch(e => { error(e.message); }).finally(() => { pendingPatches--; fieldDirty = pendingPatches > 0 || unsaved.length > 0; updateBar(); });
    return task;
  }
  async function loadMods(refresh = false) {
    if (!file) return;
    // Draft metadata is read separately so live SSE cannot overwrite the editing state.
    mods = await call(`/api/mods?file=${encodeURIComponent(file)}&draft=1${refresh ? "&refresh=1" : ""}`);
    renderMods();
    if (["world", "custom"].includes(configTab)) renderFields();
  }
  function customFields() {
    if (!mods) return [];
    const selected = new Set(mods.mods);
    return mods.workshop.flatMap(w => w.available || []).filter(r => selected.has(r.modId)).flatMap(r => (r.options || []).map(o => ({ ...o, modId: r.modId, custom: true })));
  }
  function fields() {
    if (!draft) return [];
    if (configTab === "server") return draft.fields || [];
    const known = new Map(customFields().map(o => [o.key, o]));
    const allModOptions = new Set((mods?.workshop || []).flatMap(w => w.available || []).flatMap(r => r.options || []).map(o => o.key));
    const present = new Map((draft.sandboxFields || []).map(o => [o.key, o]));
    const combined = [...present.values()].map(o => known.has(o.key) ? { ...o, ...known.get(o.key), value: o.value } : allModOptions.has(o.key) || o.group === "Неизвестные / сохранённые параметры" ? { ...o, group: "Выключенные / отсутствующие моды", preserved: true } : o);
    for (const o of known.values()) {
      if (!present.has(o.key)) combined.push({ ...o, value: o.default ?? "", absent: true });
    }
    return combined.filter(o => configTab === "custom" ? o.custom || o.preserved : !o.custom && !o.preserved);
  }
  function fieldControl(rec, i) {
    const attrs = `id="config-field-${i}" data-key="${esc(rec.key)}" data-owner="${esc(rec.owner || "")}" data-kind="${configTab === "server" ? "ini" : "sandbox"}" data-type="${esc(rec.type)}" ${rec.owner || S.demo ? "disabled" : ""}`;
    if (rec.type === "boolean") return `<input type="checkbox" ${attrs} ${rec.value === true ? "checked" : ""} />`;
    if (rec.type === "multiline" || rec.type === "string" && typeof rec.value === "string" && (rec.value.includes("\n") || rec.value.length > 140)) {
      const value = rec.lineSeparator ? String(rec.value).split(rec.lineSeparator).join("\n") : rec.value;
      return `<textarea ${attrs} rows="4" ${rec.lineSeparator ? `data-line-separator="${esc(rec.lineSeparator)}"` : ""}>${esc(value)}</textarea>`;
    }
    if (rec.type === "list") return `<textarea ${attrs} rows="3" data-list-delimiter="${esc(rec.delimiter || ";")}" aria-description="По одной записи в строке">${esc(String(rec.value).split(rec.delimiter || ";").join("\n"))}</textarea>`;
    if (rec.type === "enum" && rec.choices?.length) {
      const unknown = rec.choices.every(c => Number(c.value) !== Number(rec.value));
      return `<select ${attrs}>${unknown ? `<option value="${esc(rec.value)}">${esc(rec.value)} · вне схемы</option>` : ""}${rec.choices.map(c => `<option value="${esc(c.value)}" ${Number(c.value) === Number(rec.value) ? "selected" : ""}>${esc(c.label)}</option>`).join("")}</select>`;
    }
    const numeric = ["integer", "double"].includes(rec.type);
    return `<input ${attrs} type="${rec.secret ? "password" : numeric ? "number" : "text"}" value="${rec.secret ? "" : esc(rec.value)}" ${rec.secret ? 'placeholder="Сохранённый пароль скрыт" autocomplete="new-password"' : ""} ${numeric ? `step="${rec.type === "integer" ? "1" : "any"}"` : ""} ${rec.min !== undefined ? `min="${esc(rec.min)}"` : ""} ${rec.max !== undefined ? `max="${esc(rec.max)}"` : ""} />`;
  }
  function renderFields() {
    $("configFields").hidden = ["sources", "history"].includes(configTab);
    $("configSources").hidden = configTab !== "sources";
    $("configHistory").hidden = configTab !== "history";
    $("configSearch").hidden = ["sources", "history"].includes(configTab);
    if (!draft || ["sources", "history"].includes(configTab)) return;
    if (configTab !== "server" && draft.sandboxDiagnostic) {
      $("configFields").innerHTML = `<p class="editor-error">${esc(draft.sandboxDiagnostic)}. Форма отключена. В исходнике все литералы скрыты маркерами __PZ_RAW_LITERAL_; оставьте маркеры для сохранения исходных значений. Lua проверяется без выполнения.</p>`;
      return;
    }
    const query = $("configSearch").value.toLowerCase();
    const groups = new Map();
    fields().filter(o => `${o.key} ${o.label}`.toLowerCase().includes(query)).forEach((rec, i) => {
      if (!groups.has(rec.group)) groups.set(rec.group, []);
      groups.get(rec.group).push(`<div class="config-field"><label for="config-field-${i}"><strong>${esc(rec.label)}</strong><code>${esc(rec.key)}</code></label>${fieldControl(rec, i)}<p class="hint">${esc(rec.owner ? `Источник: ${rec.owner}. Измените параметр в окружении контейнера.` : rec.hint || "Применяется после запуска; влияние на существующий мир зависит от параметра")}${rec.absent ? " · Значение по умолчанию ещё не записано" : ""}</p><p class="field-error" data-error-key="${esc(rec.key)}" role="alert" hidden></p>${configTab !== "server" && rec.preserved ? `<button type="button" class="btn small" data-remove-option="${esc(rec.key)}">Удалить параметр…</button>` : ""}</div>`);
    });
    $("configFields").innerHTML = (configTab === "server" ? '<p><a href="#/mods">Mods, WorkshopItems и карты → редактор модов</a></p>' : "") + [...groups].map(([name, entries]) => `<details class="config-group" open><summary>${esc(name)} <span class="hint">${entries.length}</span></summary><div class="config-grid">${entries.join("")}</div></details>`).join("") || '<p class="hint">Нет настроек. При необходимости добавьте параметр в исходник.</p>';
  }
  function highlight(kind) {
    const input = $(kind + "Source"), output = $(kind + "Highlight");
    output.innerHTML = input.value.split("\n").map((line, i) => `<span class="source-line"><span class="line-number">${i + 1}</span><span class="${/^\s*(#|;|--)/.test(line) ? "syntax-comment" : line.includes("=") ? "syntax-value" : ""}">${esc(line) || " "}</span></span>`).join("\n") + "\n";
    output.scrollTop = input.scrollTop;
    output.scrollLeft = input.scrollLeft;
  }
  function renderSources() {
    if (!draft || sourceDirty) return;
    $("iniSource").value = draft.texts.ini || "";
    $("sandboxSource").value = draft.texts.sandbox || "";
    $("sandboxSource").disabled = draft.texts.sandbox === null;
    for (const kind of ["ini", "sandbox"]) highlight(kind);
  }
  async function flushSources() {
    if (!sourceDirty || !draft) return;
    const texts = { ini: $("iniSource").value };
    if (!$("sandboxSource").disabled) texts.sandbox = $("sandboxSource").value;
    await patch({ texts }, false);
    sourceDirty = false;
    renderFields();
    renderSources();
    await loadMods();
  }
  function packetVisible(packet) {
    const query = $("modQuery").value.toLowerCase();
    const filter = $("modFilter").value;
    const hasIssue = mods.problems.some(p => p.workshopId === packet.workshopId || packet.mods.includes(p.modId));
    const updateIds = new Set((S.overview?.modsCheck?.items || []).map(i => String(i.workshopId)));
    return `${packet.title} ${packet.workshopId} ${packet.mods.join(" ")}`.toLowerCase().includes(query) &&
      (filter === "all" || filter === "selected" && packet.selected.length || filter === "disabled" && !packet.selected.length || filter === "pending" && packet.status === "pending" || filter === "problems" && hasIssue || filter === "updates" && updateIds.has(packet.workshopId));
  }
  function renderMods() {
    if (!mods) return;
    $("modSummary").textContent = `${mods.workshop.length} пакетов · ${mods.mods.length} выбранных ModID`;
    const openIds = new Set([...$("modPackages").querySelectorAll("details[open]")].map(d => d.dataset.item));
    let packets = mods.workshop.filter(packetVisible);
    if ($("modSortNew").value === "title") packets = [...packets].sort((a, b) => a.title.localeCompare(b.title, "ru"));
    $("modPackages").innerHTML = packets.map(w => `<details class="workshop-package" data-item="${esc(w.workshopId)}" ${openIds.has(w.workshopId) ? "open" : ""}><summary><strong>${esc(w.title)}</strong><code>${esc(w.workshopId)}</code><span class="pill" data-state="${w.status === "pending" ? "warn" : "ok"}">${w.status === "pending" ? "Ожидает загрузки" : `${w.selected.length} / ${w.mods.length} ModID`}</span></summary><div class="package-content"><div class="editor-toolbar"><a href="${esc(w.url)}" target="_blank" rel="noopener">Steam Workshop ↗</a><button type="button" class="btn small" data-remove-item="${esc(w.workshopId)}">Удалить пакет из конфигурации…</button></div>${(w.available || []).map(r => r.modId ? `<div class="mod-option"><label><input type="checkbox" data-modid="${esc(r.modId)}" ${mods.mods.includes(r.modId) ? "checked" : ""} ${S.demo || r.compatible === false && !mods.mods.includes(r.modId) ? "disabled" : ""} /><strong>${esc(r.name)}</strong><code>${esc(r.modId)}</code></label><p class="hint">Версия ${esc(r.branch)} · папка ${esc(r.folder)}${r.compatible === false ? " · Нет подходящего каталога B42" : ""}${r.versionMin ? ` · От версии ${esc(r.versionMin)}` : ""}${r.versionMax ? ` · До версии ${esc(r.versionMax)}` : ""}${r.require?.length ? ` · Требует: ${esc(r.require.join(", "))}` : ""}</p>${r.options?.length ? '<a href="#/settings" data-open-custom>Настройки мода →</a>' : ""}</div>` : `<p class="editor-error">${esc(r.error)}</p>`).join("") || '<p class="hint">Сначала загрузите пакет через сервер, затем выберите ModID. Название Steam не определяет идентификаторы.</p>'}</div></details>`).join("") || '<p class="hint">Пакеты не найдены. Добавьте Steam-ссылку или измените фильтр.</p>';
    $("modOrder").innerHTML = '<h3>Порядок выбранных ModID</h3><p class="hint">Порядок отображения пакетов на вкладке «Состав» не меняет конфигурацию.</p>' + mods.mods.map((mid, i) => `<div class="order-row" draggable="true" data-order-id="${esc(mid)}"><span class="hint">${i + 1}</span><code>${esc(mid)}</code><button type="button" class="btn small" data-move-id="${esc(mid)}" data-direction="-1" ${i === 0 ? "disabled" : ""} aria-label="Поднять ${esc(mid)}">↑</button><button type="button" class="btn small" data-move-id="${esc(mid)}" data-direction="1" ${i === mods.mods.length - 1 ? "disabled" : ""} aria-label="Опустить ${esc(mid)}">↓</button></div>`).join("") + `<h3>Карты · Map=</h3><p class="hint">Порядок карт сохраняется. Добавление карты не изменяет уже исследованные области мира.</p><ol>${mods.maps.map(m => `<li><code>${esc(m)}</code></li>`).join("")}</ol><form id="mapEdit" class="editor-toolbar"><input id="mapList" value="${esc(mods.maps.join(";"))}" aria-label="Порядок карт через точку с запятой" /><button class="btn" type="submit">В черновик</button></form><p class="hint">Найденные карты: ${esc([...new Set(mods.workshop.flatMap(w => w.available || []).flatMap(r => r.maps || []))].join(", ") || "нет")}</p>`;
    $("mapEdit").addEventListener("submit", e => { e.preventDefault(); patch({ mods: { maps: $("mapList").value.split(";").map(v => v.trim()).filter(Boolean) } }).catch(() => {}); });
    $("modProblems").innerHTML = (mods.problems || []).map(p => `<div class="problem-row"><strong>${p.severity === "error" ? "Ошибка" : "Непроверено"}</strong><p>${esc(p.message)}</p>${p.code === "dependency" ? `<button type="button" class="btn small" data-add-dependency="${esc(p.dependency)}">Добавить зависимость ${esc(p.dependency)}</button>` : ""}</div>`).join("") || '<p class="hint">По доступным метаданным проблем не найдено. Это не проверка конфликтов Lua-кода.</p>';
    installNotice();
    $("legacyMods").hidden = !(draft?.legacyDisabled || []).length;
    $("legacyMods").innerHTML = '<strong>Старый реестр выключенных модов</strong><p>Принадлежность профилю не определена. Перенос выполняется только вашим явным выбором; старые записи сохраняются.</p>' + (draft?.legacyDisabled || []).map(r => `<button type="button" class="btn small" data-legacy-id="${esc(r.workshopId)}">Восстановить ${esc(r.title || r.workshopId)} в ${esc(file)}</button>`).join("");
    updateModTab();
  }
  function installNotice() {
    const pending = mods.workshop.some(w => w.status === "pending");
    const installation = draft?.state?.installation;
    $("installNotice").hidden = !pending && !installation;
    $("installNotice").innerHTML = `<strong>${installation?.stage === "select-mods" ? "Пакеты загружены. Выберите ModID" : "Добавление модов проходит в две стадии"}</strong><p>1. Рестарт скачает новые Workshop items и сохранит прежние Mods. 2. После выбора ModID следующий рестарт активирует их вместе с остальными правками.</p>${pending ? '<button type="button" class="btn" id="prepareWorkshop">Загрузить пакеты через сервер…</button>' : ""}${installation ? '<button type="button" class="btn small" id="cancelInstallation">Отменить добавление…</button><button type="button" class="btn small" id="retryInstallation">Повторить проверку загрузки</button>' : ""}`;
    $("prepareWorkshop")?.addEventListener("click", () => confirmApply(true));
    $("retryInstallation")?.addEventListener("click", () => confirmApply(true));
    $("cancelInstallation")?.addEventListener("click", () => modal.open({ title: "Отменить добавление пакетов?", bodyHTML: "Новые пакеты будут удалены из черновика. После этого примените изменения с рестартом. Скачанный кеш сохранится.", onConfirm: async () => {
      const added = new Set(installation.addedItems);
      const mids = new Set(mods.workshop.filter(w => added.has(w.workshopId)).flatMap(w => w.mods));
      await patch({ mods: { items: mods.workshop.filter(w => !added.has(w.workshopId)).map(w => w.workshopId), selected: mods.mods.filter(mid => !mids.has(mid)) } });
    } }));
  }
  function updateModTab() {
    $("modComposition").hidden = modTab !== "composition";
    $("modOrder").hidden = modTab !== "order";
    $("modProblems").hidden = modTab !== "problems";
    $("sec-mods-update").hidden = modTab !== "updates";
  }
  async function validate(prepare = false) {
    await chain;
    await flushSources();
    const result = await call("/api/config-validate", { file, prepare, draftRevision: draft.draftRevision });
    document.querySelectorAll("[data-error-key]").forEach(el => { el.hidden = true; });
    for (const issue of result.errors || []) {
      document.querySelectorAll("[data-error-key]").forEach(el => { if (el.dataset.errorKey === issue.key) { el.textContent = issue.message; el.hidden = false; } });
    }
    return result;
  }
  function diffHtml(result) {
    return `${(result.errors || []).map(e => `<p class="editor-error">${esc(e.message)}</p>`).join("")}${(result.warnings || []).map(e => `<p class="hint">${esc(e.message)}</p>`).join("")}${Object.entries(result.conflictDiff || {}).map(([kind, diff]) => `<h4>Изменения на диске · ${esc(kind)}</h4><pre class="config-diff">${esc(diff || "Нет изменений")}</pre>`).join("")}${Object.entries(result.diff || {}).map(([kind, diff]) => `<h4>Ваш черновик · ${esc(kind)}</h4><pre class="config-diff">${esc(diff || "Нет изменений")}</pre>`).join("")}`;
  }
  async function confirmApply(prepare = false, restart = true) {
    if (!draft || S.demo) return;
    try {
      const result = await validate(prepare);
      if (!result.valid) { error(result.errors.map(e => e.message).join("; ")); return; }
      const revisionAtReview = result.draftRevision;
      modal.open({ title: prepare ? "Загрузить Workshop items?" : restart ? "Применить конфигурацию?" : "Сохранить файлы?", bodyHTML: `<p>${prepare ? "Первый рестарт загрузит пакеты. Прежние ModID и остальные настройки останутся без изменений." : restart ? "Сервер сохранит мир, остановится, применит конфигурацию и запустится." : "Файлы будут записаны при остановленном сервере."}${result.modChanges || prepare ? " Перед изменением модов обязателен бэкап мира." : ""}</p>${diffHtml(result)}${restart ? '<label class="field">Предупредить игроков<select id="editorWarn"><option value="300">За 5 минут</option><option value="600">За 10 минут</option><option value="60">За 1 минуту</option><option value="0">Без предупреждения</option></select></label>' : ""}`, onConfirm: async () => {
        if (draft.draftRevision !== revisionAtReview || sourceDirty || fieldDirty) throw new Error("Черновик изменился после просмотра. Проверьте изменения заново");
        await call("/api/action", { op: prepare ? "prepare-workshop" : "apply-config", file, draftRevision: revisionAtReview, restart, warnSeconds: restart ? Number($("editorWarn").value) : 0 });
        toast("Операция запущена. Прогресс отображается в панели сервера.", "ok");
        draft.status = "applying";
        updateBar();
      } });
    } catch (e) { error(e.message); }
  }
  async function history() {
    if (!file) return;
    const data = await call(`/api/config-history?file=${encodeURIComponent(file)}`);
    $("configHistory").innerHTML = (data.items || []).map(r => `<div class="history-row"><strong>${esc(fmtTime(r.at))}</strong><span>${esc(r.reason)}</span><button type="button" class="btn small" data-restore-config="${esc(r.id)}">Посмотреть / восстановить…</button></div>`).join("") || '<p class="hint">История появится после первого сохранения конфигурации.</p>';
  }
  $("configProfile").addEventListener("change", async () => {
    const next = $("configProfile").value;
    if (!next) { $("configProfile").value = file; return; }
    try {
      await chain;
      if (unsaved.length) throw new Error("Нет связи: сначала повторите сохранение черновика или отмените изменения");
      await flushSources();
      await loadProfile(next);
      try { localStorage.setItem("pz-config-profile", next); } catch (_) { /* optional preference */ }
    } catch (e) { $("configProfile").value = file; error(e.message); }
  });
  $("configTabs").addEventListener("click", async e => {
    const button = e.target.closest("[data-tab]");
    if (!button) return;
    try {
      await chain; if (unsaved.length) throw new Error("Нет связи: сначала повторите сохранение черновика или отмените изменения"); await flushSources(); configTab = button.dataset.tab;
      $("configTabs").querySelectorAll("[role=tab]").forEach(b => b.setAttribute("aria-selected", String(b === button)));
      renderFields(); renderSources(); if (configTab === "history") await history();
    } catch (err) { error(err.message); }
  });
  $("modTabs").addEventListener("click", e => {
    const button = e.target.closest("[data-tab]"); if (!button) return;
    modTab = button.dataset.tab;
    $("modTabs").querySelectorAll("[role=tab]").forEach(b => b.setAttribute("aria-selected", String(b === button)));
    updateModTab();
  });
  $("configSearch").addEventListener("input", renderFields);
  $("configFields").addEventListener("change", e => {
    const field = e.target.closest("[data-key]"); if (!field) return;
    if (!field.checkValidity()) { field.reportValidity(); return; }
    const value = field.type === "checkbox" ? field.checked : ["integer", "double", "enum"].includes(field.dataset.type) ? Number(field.value) : field.dataset.lineSeparator ? field.value.replace(/\r?\n/g, field.dataset.lineSeparator) : field.dataset.listDelimiter ? field.value.split(/\r?\n/).map(v => v.trim()).filter(Boolean).join(field.dataset.listDelimiter) : field.value;
    patch({ [field.dataset.kind]: { [field.dataset.key]: value } }, false).catch(() => {});
  });
  $("configFields").addEventListener("click", e => {
    const remove = e.target.closest("[data-remove-option]"); if (!remove) return;
    modal.open({ title: "Удалить сохранённый параметр?", bodyHTML: esc(remove.dataset.removeOption), onConfirm: () => patch({ removeSandbox: [remove.dataset.removeOption] }) });
  });
  for (const kind of ["ini", "sandbox"]) {
    $(kind + "Source").addEventListener("input", () => { sourceDirty = true; $("draftSaved").textContent = "Исходник ещё не сохранён в черновик"; highlight(kind); });
    $(kind + "Source").addEventListener("scroll", () => highlight(kind));
  }
  $("sourceSave").addEventListener("click", () => flushSources().catch(e => error(e.message)));
  $("workshopAdd").addEventListener("submit", async e => {
    e.preventDefault(); if (!draft || !mods) return;
    const button = e.target.querySelector("button"); button.disabled = true;
    try {
      const result = await call("/api/workshop-resolve", { input: $("workshopInput").value });
      await patch({ mods: { items: [...new Set([...mods.workshop.map(w => w.workshopId), ...result.items.map(w => w.workshopId)])] } });
      $("workshopInput").value = "";
    } catch (err) { error(err.message); } finally { button.disabled = false; }
  });
  $("modPackages").addEventListener("change", async e => {
    const input = e.target.closest("[data-modid]"); if (!input) return;
    const checked = input.checked, mid = input.dataset.modid;
    try { await patch(() => ({ mods: { selected: checked ? [...new Set([...mods.mods, mid])] : mods.mods.filter(m => m !== mid), preserveOrder: true } })); } catch (_) { input.checked = !checked; }
  });
  $("legacyMods").addEventListener("click", e => {
    const button = e.target.closest("[data-legacy-id]"); if (!button) return;
    const rec = draft.legacyDisabled.find(r => r.workshopId === button.dataset.legacyId);
    modal.open({ title: "Перенести запись в выбранный профиль?", bodyHTML: `Профиль ${esc(file)}. Состав будет проверен по скачанным Steam-пакетам перед применением.`, onConfirm: () => patch({ mods: { items: [...new Set([...mods.workshop.map(w => w.workshopId), rec.workshopId])], selected: [...new Set([...mods.mods, ...rec.modIds.map(mid => mid.replace(/^\\+/, ""))])] } }) });
  });
  $("modPackages").addEventListener("click", e => {
    const remove = e.target.closest("[data-remove-item]");
    if (e.target.closest("[data-open-custom]")) { configTab = "custom"; $("configTabs").querySelectorAll("[role=tab]").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === "custom"))); renderFields(); }
    if (!remove) return;
    const packet = mods.workshop.find(w => w.workshopId === remove.dataset.removeItem);
    modal.open({ title: "Удалить пакет из конфигурации?", bodyHTML: `Будут убраны Workshop item ${esc(packet.workshopId)} и его выбранные ModID. Скачанные файлы сохранятся.`, onConfirm: () => patch({ mods: { items: mods.workshop.filter(w => w !== packet).map(w => w.workshopId), selected: mods.mods.filter(mid => !packet.mods.includes(mid)) } }) });
  });
  $("modProblems").addEventListener("click", e => {
    const button = e.target.closest("[data-add-dependency]"); if (!button) return;
    const mid = button.dataset.addDependency;
    const providers = mods.workshop.flatMap(w => w.available || []).filter(r => r.modId === mid);
    if (providers.length !== 1) { error("Пакет зависимости не определён. Добавьте его Steam-ссылку; Workshop ID нельзя получить из ModID."); return; }
    patch({ mods: { selected: [mid, ...mods.mods.filter(m => m !== mid)] } }).catch(() => {});
  });
  function move(mid, target) {
    const list = mods.mods.filter(m => m !== mid);
    list.splice(Math.max(0, Math.min(target, list.length)), 0, mid);
    return patch({ mods: { selected: list } });
  }
  $("modOrder").addEventListener("click", e => {
    const button = e.target.closest("[data-move-id]");
    if (button) move(button.dataset.moveId, mods.mods.indexOf(button.dataset.moveId) + Number(button.dataset.direction)).catch(() => {});
  });
  $("modOrder").addEventListener("dragstart", e => { const row = e.target.closest("[data-order-id]"); if (row) { dragId = row.dataset.orderId; e.dataTransfer.setData("text/plain", dragId); } });
  $("modOrder").addEventListener("dragover", e => { if (dragId && e.target.closest("[data-order-id]")) e.preventDefault(); });
  $("modOrder").addEventListener("drop", e => { const row = e.target.closest("[data-order-id]"); if (dragId && row) { e.preventDefault(); move(dragId, mods.mods.indexOf(row.dataset.orderId)).catch(() => {}); } dragId = null; });
  $("modOrder").addEventListener("dragend", () => { dragId = null; });
  for (const id of ["modQuery", "modFilter", "modSortNew"]) $(id).addEventListener(id === "modQuery" ? "input" : "change", renderMods);
  $("modRescan").addEventListener("click", () => loadMods(true).catch(e => error(e.message)));
  $("modExport").addEventListener("click", async () => {
    try {
      await chain; await flushSources();
      const result = await call("/api/modpack", { file });
      const url = URL.createObjectURL(new Blob([JSON.stringify(result.pack, null, 2)], { type: "application/json" }));
      const link = document.createElement("a"); link.href = url; link.download = `${file.replace(/\.ini$/, "")}-mods.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) { error(e.message); }
  });
  $("modImport").addEventListener("change", async e => {
    try {
      const uploaded = e.target.files[0]; if (!uploaded) return;
      if (uploaded.size > 1_000_000) throw new Error("Набор больше 1 МБ");
      const pack = JSON.parse(await uploaded.text());
      await chain;
      if (unsaved.length) throw new Error("Нет связи: сначала повторите сохранение черновика или отмените изменения");
      await flushSources();
      draft = await call("/api/modpack", { file, draftRevision: draft.draftRevision, pack });
      renderFields(); renderSources(); await loadMods(); updateBar();
    } catch (err) { error(err.message); } finally { e.target.value = ""; }
  });
  $("configDiff").addEventListener("click", async () => {
    try { modal.open({ title: draft?.conflict ? "Конфликт / изменения конфигурации" : "Изменения конфигурации", bodyHTML: diffHtml(await validate()), onConfirm: async () => {} }); } catch (e) { error(e.message); }
  });
  $("draftRetry").addEventListener("click", () => patch({}).catch(() => {}));
  $("configDiscard").addEventListener("click", () => modal.open({ title: "Отменить черновик?", bodyHTML: "Будет загружена текущая конфигурация с диска. Уже записанные настройки и скачанные пакеты сохранятся.", onConfirm: async () => { unsaved = []; await patch({ discard: true }); sourceDirty = false; renderSources(); } }));
  $("configSave").addEventListener("click", () => confirmApply(false, false));
  $("configApply").addEventListener("click", () => confirmApply(false, true));
  function operationLogs() {
    S.logsSince = Date.parse(draft?.state?.operationStartedAt || "") || 0;
    $("logsFilter").value = ""; S.logsLevel = "all";
    renderLogsFiltered(); location.hash = "#/console";
  }
  $("editorLogs").addEventListener("click", operationLogs);
  function restoreHistory(id) {
    modal.open({ title: "Восстановить версию в черновик?", bodyHTML: "Текущий черновик будет заменён. После загрузки просмотрите различия и отдельно примените изменения.", onConfirm: async () => {
      await chain;
      draft = await call("/api/config-history", { file, historyId: id, draftRevision: draft.draftRevision });
      unsaved = []; sourceDirty = fieldDirty = false;
      renderFields(); renderSources(); await loadMods(); updateBar();
      const result = await validate();
      setTimeout(() => modal.open({ title: "Изменения при восстановлении", bodyHTML: diffHtml(result), onConfirm: async () => {} }), 0);
    } });
  }
  $("configOperationResult").addEventListener("click", e => {
    if (e.target.closest("[data-operation-logs]")) operationLogs();
    if (e.target.closest("[data-operation-restore]") && draft?.state?.historyId) restoreHistory(draft.state.historyId);
  });
  $("configHistory").addEventListener("click", e => {
    const button = e.target.closest("[data-restore-config]"); if (!button) return;
    restoreHistory(button.dataset.restoreConfig);
  });
  $("navMore").addEventListener("click", () => {
    const open = $("moreMenu").hidden; $("moreMenu").hidden = !open; $("navMore").setAttribute("aria-expanded", String(open));
  });
  $("moreMenu").addEventListener("click", () => { $("moreMenu").hidden = true; $("navMore").setAttribute("aria-expanded", "false"); });
  for (const id of ["configTabs", "modTabs"]) $(id).addEventListener("keydown", e => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    const tabs = [...$(id).querySelectorAll("[role=tab]")];
    const current = tabs.indexOf(document.activeElement);
    if (current < 0) return;
    e.preventDefault();
    const next = e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : (current + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next].focus(); tabs[next].click();
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape") { $("moreMenu").hidden = true; $("navMore").setAttribute("aria-expanded", "false"); } });
  window.addEventListener("beforeunload", e => { if (sourceDirty || fieldDirty) { e.preventDefault(); e.returnValue = ""; } });
  window.addEventListener("hashchange", updateBar);
  setInterval(async () => {
    updateBar();
    const active = !!S.op?.active;
    if ((previousOp || draft?.status === "applying") && !active && file && !loading && !sourceDirty && !fieldDirty) {
      try { await loadProfile(file); } catch (e) { error(e.message); }
    }
    previousOp = active;
  }, 1500);
  init();
  return {
    get file() { return file; },
    route(view) { updateBar(); if (view === "mods") updateModTab(); },
    background() {
      // Live registry payloads contain committed state, not this editor's draft.
      if (!file || sourceDirty || fieldDirty || loading || Date.now() - lastRefresh < 15000) return;
      lastRefresh = Date.now();
      call(`/api/config-draft?file=${encodeURIComponent(file)}`).then(data => {
        if (data.file !== file) return;
        if (draft && data.draftRevision !== draft.draftRevision) { draft.conflict = true; $("draftSaved").textContent = "Черновик изменён другой вкладкой. Перевыберите профиль для загрузки."; }
        else if (draft) { draft.conflict = data.conflict; draft.status = data.status; draft.state = data.state; }
        updateBar();
      }).catch(() => { /* retain last visible data on disconnect */ });
    },
  };
})();
