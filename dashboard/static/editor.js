/* Profile-scoped editors. Form edits persist drafts; only operations write PZ files. */
"use strict";
window.ConfigEditor = (() => {
  let file = "", draft = null, mods = null, configTab = "server", modTab = "composition";
  let chain = Promise.resolve(), loading = false, sourceDirty = false, fieldDirty = false;
  let previousOp = false, lastRefresh = 0, dragId = null, pendingPatches = 0;
  let resolvingWorkshop = false;
  const pendingFields = new Map();
  let deferredFields = false;
  let fieldSaveFailed = false;
  let unsaved = [];
  let operationMarkup = "";
  let historyRequest = 0;
  const groupStates = new Map();
  let fieldQuery = "";
  const SECRET = "__PZ_SECRET_UNCHANGED__";
  function gameDescription(text) {
    return String(text || "").replace(/<br\s*\/?\s*>|<LINE>/gi, "\n")
      .replace(/\/[0-9A-F]{6}|<(?:RGB:[^>]*|SIZE:[^>]*|CENTRE|LEFT|RIGHT|H[12])>/g, "");
  }
  const statuses = { draft: "Есть черновик", saved: "Сохранено, требуется запуск", applying: "Применение", applied: "Применено", error: "Ошибка", unconfirmed: "Применение не подтверждено", "select-mods": "Пакеты загружены; выберите ModID" };
  const call = async (path, body) => {
    const result = await api(path, body === undefined ? { timeout: 180000 } : { method: "POST", body, timeout: 180000 });
    if (!result.ok) { const failure = new Error(result.error || "Нет ответа редактора"); failure.remote = true; throw failure; }
    return result;
  };
  function error(message) {
    $("configError").textContent = message;
    $("configError").dataset.source = "operation";
    $("configError").hidden = false;
    toast(message, "error");
  }
  function clearError() { $("configError").hidden = true; delete $("configError").dataset.source; }
  function updateBar() {
    if (!draft) { $("draftBar").hidden = true; return; }
    const busy = !!S.op?.active;
    const locked = busy || loading || S.demo;
    const running = S.overview?.containerInfo?.running;
    $("configProfile").disabled = busy || loading;
    $("view-mods").inert = loading || !mods;
    for (const kind of ["ini", "sandbox"]) $(kind + "Source").readOnly = busy || loading;
    const editorView = ["settings", "mods"].includes(activeView);
    const needsAction = draft.changed || sourceDirty || fieldDirty || pendingFields.size || unsaved.length || draft.conflict || !$("configError").hidden || ["saved", "applying", "error", "select-mods"].includes(draft.status);
    $("draftBar").hidden = !editorView || !needsAction;
    $("draftRetry").hidden = !unsaved.length && !(fieldSaveFailed && pendingFields.size);
    $("draftLabel").textContent = (pendingFields.size ? "Есть несохранённые поля" : statuses[draft.status] || "Конфигурация") + (draft.changed ? ` · изменённых строк: ${draft.changedLines || 1}` : "");
    $("configStatus").textContent = statuses[draft.status] || "Конфигурация";
    $("configStatus").dataset.state = draft.conflict || draft.status === "error" ? "bad" : draft.changed || draft.status !== "applied" ? "warn" : "ok";
    $("configApply").disabled = busy || !draft.canApply || !draft.canWrite || S.demo || loading || !needsAction;
    $("configSave").disabled = busy || !draft.canWrite || running !== false || S.demo || loading;
    $("configDiscard").disabled = busy || S.demo || loading;
    $("configRebase").hidden = !draft.conflict;
    $("configRebase").disabled = busy || S.demo || loading || fieldDirty;
    $("configApply").title = draft.canApply ? "" : "Выбран неактивный или неподтверждённый профиль";
    $("configSave").title = running === true ? "Сначала остановите сервер" : running === false ? "" : "Состояние сервера ещё не получено";
    $("configVerifyHelp").hidden = !draft.canApply || !draft.state?.savedRevision || !S.overview?.containerInfo?.running;
    $("configVerify").disabled = busy || S.demo || loading;
    if (busy) $("draftSaved").textContent = `${S.op.active.phase || "Операция"} · ${S.op.active.message || ""}`;
    $("configFields").querySelectorAll("[data-key]").forEach(el => { el.disabled = busy || loading || S.demo || !!el.dataset.owner; });
    const incompatible = new Set((mods?.workshop || []).flatMap(w => w.available || []).filter(r => r.compatible === false).map(r => r.modId));
    $("modPackages").querySelectorAll("[data-modid]").forEach(el => { el.disabled = locked || incompatible.has(el.dataset.modid) && !el.checked; });
    document.querySelectorAll("#modPackages button, #modMapEditor input, #modMapEditor button, #modProblems button, #legacyMods button, #installNotice button, #configFields [data-remove-option], #configHistory button, #configOperationResult [data-operation-restore], #configOperationResult [data-verify-running]").forEach(el => { el.disabled = locked; });
    for (const id of ["modImport", "modExport", "modRescan", "sourceSave", "draftRetry"]) $(id).disabled = locked;
    $("modImport").closest("label").setAttribute("aria-disabled", String(locked));
    $("workshopInput").disabled = locked || resolvingWorkshop;
    $("workshopAdd").querySelector("button").disabled = locked || resolvingWorkshop;
    $("modOrderList").querySelectorAll("button").forEach(el => { el.disabled = busy || S.demo || loading || el.dataset.edge === "true"; });
    const profileState = $("configActive");
    profileState.textContent = draft.canApply ? "Активен на сервере" : draft.activeFile ? "Другой профиль" : "Не подтверждён";
    profileState.dataset.state = draft.canApply ? "active" : draft.activeFile ? "other" : "unknown";
    profileState.title = draft.activeFile ? `Сервер использует ${draft.activeFile}` : "Не удалось определить профиль запуска сервера";
    $("configVersion").textContent = draft.version ? `PZ ${draft.version}` : "B42 · версия неизвестна";
    if (draft.conflict && ($("configError").hidden || $("configError").dataset.source === "conflict")) {
      $("configError").textContent = "Рабочие файлы изменились. Нажмите «Посмотреть изменения», затем «Обновить основу черновика». Пересекающиеся правки нужно разрешить вручную.";
      $("configError").dataset.source = "conflict";
      $("configError").hidden = false;
    }
    $("configSaveHint").textContent = running === undefined
      ? "Правки сохраняются в черновике. Ожидаем состояние сервера перед записью файлов."
      : running
      ? "Сервер работает. Правки полей сохраняются в черновике. Для записи файлов используйте «Применить с рестартом…» или сначала остановите сервер."
      : "Правки полей сохраняются в черновике. «Сохранить» запишет файлы при остановленном сервере; запуск выполняется отдельно.";
    if (draft.dataDiagnostic) {
      $("configError").textContent = draft.dataDiagnostic;
      $("configError").hidden = false;
    }
    $("configVersionDiagnostic").hidden = !draft.versionDiagnostic;
    $("configVersionDiagnostic").textContent = draft.versionDiagnostic || "";
    const state = draft.state || {};
    if (S.logsProfile === file && S.logsSince && Date.parse(state.operationStartedAt || "") === S.logsSince) {
      const until = Date.parse(state.operationCompletedAt || "") || 0;
      if (until && until !== S.logsUntil) { S.logsUntil = until; renderLogsFiltered(); refreshLogs(); }
    }
    const result = $("configOperationResult");
    result.hidden = !editorView || !state.operationStartedAt;
    if (!result.hidden) {
      const status = busy ? "applying" : draft.status === "unconfirmed" ? "unconfirmed" : state.status;
      result.dataset.state = status === "error" ? "bad" : status === "applied" ? "ok" : "warn";
      const markup = `<strong>${esc(state.verifiedAt && ["applied", "select-mods"].includes(status) ? "Проверено после запуска" : statuses[status] || "Результат операции")}</strong><p class="hint">Профиль ${esc(file)} · ${esc(fmtTime(state.verifiedAt || state.operationCompletedAt || state.operationStartedAt))}</p>${state.error ? `<p class="editor-error">${esc(state.error)}</p>` : ""}${(state.verificationProblems || []).filter(p => p.severity !== "error").map(p => `<p class="hint">${esc(p.message)}</p>`).join("")}${state.lastFailure ? `<details><summary>Предыдущая ошибка операции</summary><p class="hint">${esc(state.lastFailure.error)}</p></details>` : ""}${state.worldBackup ? `<p class="hint">Бэкап мира: ${esc(state.worldBackup)}</p>` : state.backupBeforeApply === false ? '<p class="hint">Бэкап мира отключён для этой операции. Исходные конфиги сохраняются в истории.</p>' : ""}<div class="editor-toolbar"><button type="button" class="btn small" data-operation-logs>Логи операции</button>${status === "error" && state.historyId ? '<button type="button" class="btn small" data-operation-restore>Восстановить прежнюю конфигурацию…</button>' : ""}${status !== "applying" && draft.canApply && state.savedRevision ? '<button type="button" class="btn small" data-verify-running>Проверить запущенный сервер</button>' : ""}</div>`;
      if (markup !== operationMarkup) { result.innerHTML = markup; operationMarkup = markup; }
      result.querySelectorAll("[data-operation-restore], [data-verify-running]").forEach(el => { el.disabled = locked; });
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
      if (file !== data.file) {
        historyRequest++;
        $("configHistory").replaceChildren();
        mods = null; renderMods();
      }
      file = data.file;
      draft = data;
      $("configProfile").value = file;
      sourceDirty = fieldDirty = false;
      clearError();
      renderFields();
      renderSources();
      await loadMods();
    } finally { loading = false; updateBar(); }
    if (configTab === "history") await history();
  }
  async function init() {
    try {
      const data = await call("/api/server-configs");
      const profiles = data.profiles || [];
      $("configProfile").innerHTML = `<option value="">Выберите профиль…</option>` + profiles.map(p => `<option value="${esc(p.file)}">${esc(p.file)}</option>`).join("");
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
      if (S.op?.active || loading || S.demo) throw new Error("Дождитесь завершения операции; редактор временно недоступен");
      const update = typeof body === "function" ? body() : body;
      const updates = [...unsaved, update];
      unsaved = [];
      try {
        while (updates.length) {
          draft = await call("/api/config-draft", { file, draftRevision: draft.draftRevision, ...updates[0] });
          for (const [id, field] of pendingFields) {
            const sent = updates[0][field.dataset.kind]?.[field.dataset.key], value = formValue(field);
            const savedValue = Array.isArray(value) ? Array.isArray(sent) && sent.length === value.length && value.every((v, i) => v === sent[i]) : sent === value;
            if (field.checkValidity() && savedValue) pendingFields.delete(id);
          }
          updates.shift();
        }
      } catch (failure) { if (!failure.remote) unsaved = updates; fieldSaveFailed = !!pendingFields.size; throw failure; }
      if (!pendingFields.size) fieldSaveFailed = false;
      clearError();
      $("draftSaved").textContent = "Черновик сохранён";
      if (redraw) { renderFields(); renderSources(); await loadMods(); }
      else if (deferredFields && !pendingFields.size) renderFields();
      return draft;
    });
    chain = task.catch(e => { error(e.message); }).finally(() => { pendingPatches--; fieldDirty = pendingPatches > 0 || unsaved.length > 0; updateBar(); });
    return task;
  }
  async function loadMods(refresh = false) {
    if (!file) return;
    // Draft metadata is read separately so live SSE cannot overwrite the editing state.
    const profile = file, revision = draft?.draftRevision;
    const result = await call(`/api/mods?file=${encodeURIComponent(profile)}&draft=1${refresh ? "&refresh=1" : ""}`);
    if (file !== profile || draft?.draftRevision !== revision) return;
    if (result.file !== profile || result.draftRevision !== revision) { error("Черновик изменён другой вкладкой. Обновите страницу для загрузки актуального состава модов."); return; }
    mods = result;
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
    const attrs = `id="config-field-${i}" data-key="${esc(rec.key)}" data-owner="${esc(rec.owner || "")}" data-kind="${configTab === "server" ? "ini" : "sandbox"}" data-type="${esc(rec.type)}" aria-describedby="config-hint-${i}${rec.applicationScope ? ` config-scope-${i}` : ""}" ${rec.owner || S.demo ? "disabled" : ""}`;
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
  function formValue(field) {
    if (field.dataset.type === "map-list") return field.value.split(";").map(v => v.trim()).filter(Boolean);
    return field.type === "checkbox" ? field.checked : ["integer", "double", "enum"].includes(field.dataset.type) ? Number(field.value) : field.dataset.lineSeparator ? field.value.replace(/\r?\n/g, field.dataset.lineSeparator) : field.dataset.listDelimiter ? field.value.split(/\r?\n/).map(v => v.trim()).filter(Boolean).join(field.dataset.listDelimiter) : field.value;
  }
  async function flushFields() {
    await chain;
    if (!pendingFields.size) return;
    const update = {};
    for (const field of pendingFields.values()) {
      if (!field.checkValidity()) { field.reportValidity(); throw new Error("Проверьте значение и допустимый диапазон поля: " + field.dataset.key); }
      (update[field.dataset.kind] ||= {})[field.dataset.key] = formValue(field);
    }
    await patch(update, !!update.mods);
  }
  function renderFields() {
    if (pendingFields.size) { deferredFields = true; return; }
    deferredFields = false;
    if (!fieldQuery) $("configFields").querySelectorAll("details[data-group-key]").forEach(el => groupStates.set(el.dataset.groupKey, el.open));
    $("configFields").hidden = ["sources", "history"].includes(configTab);
    $("configSources").hidden = configTab !== "sources";
    $("configHistory").hidden = configTab !== "history";
    $("configSearch").hidden = ["sources", "history"].includes(configTab);
    $("configWorldNotice").hidden = !["world", "custom"].includes(configTab);
    if (!draft || ["sources", "history"].includes(configTab)) return;
    if (configTab !== "server" && draft.sandboxDiagnostic) {
      $("configFields").innerHTML = `<p class="editor-error">${esc(draft.sandboxDiagnostic)}. Форма отключена. В исходнике все литералы скрыты маркерами __PZ_RAW_LITERAL_; оставьте маркеры для сохранения исходных значений. Lua проверяется без выполнения.</p>`;
      return;
    }
    const query = $("configSearch").value.toLowerCase();
    fieldQuery = query;
    const groups = new Map();
    fields().filter(o => `${o.key} ${o.label}`.toLowerCase().includes(query)).forEach((rec, i) => {
      if (!groups.has(rec.group)) groups.set(rec.group, []);
      groups.get(rec.group).push(`<div class="config-field"><label for="config-field-${i}"><strong>${esc(rec.label)}</strong>${rec.label !== rec.key ? `<code>${esc(rec.key)}</code>` : ""}</label><div class="config-control">${fieldControl(rec, i)}</div>${rec.applicationScope ? `<p id="config-scope-${i}" class="config-scope" data-scope="${esc(rec.applicationScope.kind)}"><strong>${esc(rec.applicationScope.label)}</strong><span>${esc(rec.applicationScope.hint)}</span></p>` : ""}<p class="hint" id="config-hint-${i}">${esc(rec.owner ? `Источник: ${rec.owner}. Измените параметр в окружении контейнера.` : gameDescription(rec.hint) || "Применяется после запуска; влияние на существующий мир зависит от параметра")}${rec.absent ? " · Значение по умолчанию ещё не записано" : ""}</p><p class="field-error" data-error-key="${esc(rec.key)}" role="alert" hidden></p>${configTab !== "server" && rec.preserved ? `<button type="button" class="btn small" data-remove-option="${esc(rec.key)}">Удалить параметр…</button>` : ""}</div>`);
    });
    const order = ["Доступ и игроки", "PvP", "Чат", "Сохранение мира", "Безопасные дома", "Сеть", "Дополнительные параметры"];
    const ordered = [...groups].sort(([a], [b]) => configTab === "server" ? order.indexOf(a) - order.indexOf(b) : 0);
    const html = ordered.map(([name, entries]) => {
      const key = `${configTab}:${name}`;
      const open = query || (groupStates.get(key) ?? name !== "Дополнительные параметры");
      return `<details class="config-group" data-group-key="${esc(key)}" ${open ? "open" : ""}><summary>${esc(name)} <span class="group-count">${entries.length}</span></summary><div class="config-grid">${entries.join("")}</div></details>`;
    }).join("");
    $("configFields").innerHTML = (configTab === "server" ? '<p class="config-mod-link"><a href="#/mods">Mods, WorkshopItems и карты → редактор модов</a></p>' : "") + (html || '<p class="hint">Нет настроек, соответствующих поиску.</p>');
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
    if (!mods) {
      $("modSummary").textContent = "Состав выбранного профиля ещё не загружен";
      for (const id of ["modPackages", "modOrderList", "modMapEditor", "modProblems", "legacyMods"]) $(id).replaceChildren();
      $("installNotice").hidden = true;
      return;
    }
    $("modSummary").textContent = `${mods.workshop.length} пакетов · ${mods.mods.length} выбранных ModID`;
    const openIds = new Set([...$("modPackages").querySelectorAll("details[open]")].map(d => d.dataset.item));
    let packets = mods.workshop.filter(packetVisible);
    if ($("modSortNew").value === "title") packets = [...packets].sort((a, b) => a.title.localeCompare(b.title, "ru"));
    $("modPackages").innerHTML = packets.map(w => `<details class="workshop-package" data-item="${esc(w.workshopId)}" ${openIds.has(w.workshopId) ? "open" : ""}><summary><strong>${esc(w.title)}</strong><code>${esc(w.workshopId)}</code><span class="pill" data-state="${w.status === "pending" ? "warn" : "ok"}">${w.status === "pending" ? "Ожидает загрузки" : `${w.selected.length} / ${w.mods.length} ModID`}</span></summary><div class="package-content"><div class="editor-toolbar"><a href="${esc(w.url)}" target="_blank" rel="noopener">Steam Workshop ↗</a><button type="button" class="btn small" data-remove-item="${esc(w.workshopId)}">Удалить пакет из конфигурации…</button></div>${(w.available || []).map(r => r.modId ? `<div class="mod-option"><label><input type="checkbox" data-modid="${esc(r.modId)}" ${mods.mods.includes(r.modId) ? "checked" : ""} ${S.demo || r.compatible === false && !mods.mods.includes(r.modId) ? "disabled" : ""} /><strong>${esc(r.name)}</strong><code>${esc(r.modId)}</code></label><p class="hint">Версия ${esc(r.branch)} · папка ${esc(r.folder)}${r.compatible === false ? " · Нет подходящего каталога B42" : ""}${r.versionMin ? ` · От версии ${esc(r.versionMin)}` : ""}${r.versionMax ? ` · До версии ${esc(r.versionMax)}` : ""}${r.require?.length ? ` · Требует: ${esc(r.require.join(", "))}` : ""}</p>${r.options?.length ? '<a href="#/settings" data-open-custom>Настройки мода →</a>' : ""}</div>` : `<p class="editor-error">${esc(r.error)}</p>`).join("") || '<p class="hint">Сначала загрузите пакет через сервер, затем выберите ModID. Название Steam не определяет идентификаторы.</p>'}</div></details>`).join("") || '<p class="hint">Пакеты не найдены. Добавьте Steam-ссылку или измените фильтр.</p>';
    renderOrder();
    if (!pendingFields.has("mods:maps")) {
      $("modMapEditor").innerHTML = `<h3>Карты · Map=</h3><p class="hint">Порядок карт сохраняется. Добавление карты не изменяет уже исследованные области мира.</p><ol>${mods.maps.map(m => `<li><code>${esc(m)}</code></li>`).join("")}</ol><form id="mapEdit" class="editor-toolbar"><input id="mapList" data-key="maps" data-kind="mods" data-type="map-list" value="${esc(mods.maps.join(";"))}" aria-label="Порядок карт через точку с запятой" /><button class="btn" type="submit">В черновик</button></form><p class="hint">Найденные карты: ${esc([...new Set(mods.workshop.flatMap(w => w.available || []).flatMap(r => r.maps || []))].join(", ") || "нет")}</p>`;
      $("mapEdit").addEventListener("submit", e => { e.preventDefault(); patch({ mods: { maps: formValue($("mapList")) } }).catch(() => {}); });
    }
    $("modProblems").innerHTML = (mods.problems || []).map(p => `<div class="problem-row"><strong>${p.severity === "error" ? "Ошибка" : "Непроверено"}</strong><p>${esc(p.message)}</p>${p.code === "dependency" ? `<button type="button" class="btn small" data-add-dependency="${esc(p.dependency)}">Добавить зависимость ${esc(p.dependency)}</button>` : ""}</div>`).join("") || '<p class="hint">По доступным метаданным проблем не найдено. Это не проверка конфликтов Lua-кода.</p>';
    installNotice();
    $("legacyMods").hidden = !(draft?.legacyDisabled || []).length;
    $("legacyMods").innerHTML = '<strong>Старый реестр выключенных модов</strong><p>Принадлежность профилю не определена. Перенос выполняется только вашим явным выбором; старые записи сохраняются.</p>' + (draft?.legacyDisabled || []).map(r => `<button type="button" class="btn small" data-legacy-id="${esc(r.workshopId)}">Восстановить ${esc(r.title || r.workshopId)} в ${esc(file)}</button>`).join("");
    updateModTab();
    updateBar();
  }
  function installNotice() {
    const pending = mods.workshop.some(w => w.status === "pending");
    const installation = draft?.state?.installation;
    $("installNotice").hidden = !pending && !installation;
    const stage = installation?.stage;
    const title = stage === "select-mods" ? "Пакеты загружены. Выберите ModID" : stage === "error" ? "Загрузка требует проверки" : stage === "downloading" ? "Загрузка пакетов" : "Добавление модов проходит в две стадии";
    const explanation = stage === "select-mods" ? "Первый этап завершён. Выберите нужные ModID и примените черновик с рестартом. Выключенные моды не включаются автоматически." : stage === "error" ? "Прошлая операция не была подтверждена. Если вы уже перезапустили сервер, проверьте его без нового рестарта. Если пакеты не загрузились, повторите загрузку." : "1. Рестарт скачает новые Workshop items и сохранит прежние Mods. 2. После выбора ModID следующий рестарт активирует их вместе с остальными правками.";
    $("installNotice").innerHTML = `<strong>${title}</strong><p>${explanation}</p>${pending ? '<button type="button" class="btn" id="prepareWorkshop">Загрузить пакеты через сервер…</button>' : ""}${installation ? '<button type="button" class="btn small" id="cancelInstallation">Отменить добавление…</button><button type="button" class="btn small" id="retryInstallation">Повторить загрузку с рестартом…</button><button type="button" class="btn small" data-verify-running>Проверить запущенный сервер</button>' : ""}`;
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
    await flushFields();
    if (unsaved.length) throw new Error("Сначала повторите сохранение черновика после восстановления связи");
    await flushSources();
    const result = await call("/api/config-validate", { file, prepare, draftRevision: draft.draftRevision });
    document.querySelectorAll("[data-error-key]").forEach(el => { el.hidden = true; });
    for (const issue of result.errors || []) {
      document.querySelectorAll("[data-error-key]").forEach(el => { if (el.dataset.errorKey === issue.key) { el.textContent = issue.message; el.hidden = false; } });
    }
    return result;
  }
  function diffHtml(result) {
    return `${(result.errors || []).map(e => `<p class="editor-error">${esc(e.message)}</p>`).join("")}${(result.warnings || []).some(e => e.existing) ? '<p class="hint">Состав модов не меняется. Его существующие проблемы не блокируют сохранение других настроек.</p>' : ""}${(result.warnings || []).map(e => `<p class="hint">${esc(e.message)}</p>`).join("")}${Object.entries(result.conflictDiff || {}).map(([kind, diff]) => `<h4>Изменения на диске · ${esc(kind)}</h4><pre class="config-diff">${esc(diff || "Нет изменений")}</pre>`).join("")}${Object.entries(result.diff || {}).map(([kind, diff]) => `<h4>Ваш черновик · ${esc(kind)}</h4><pre class="config-diff">${esc(diff || "Нет изменений")}</pre>`).join("")}`;
  }
  function showConflict(result, compact = false) {
    const canRebase = result.rebaseAvailable;
    const reviewed = result.draftRevision;
    const explanation = canRebase
      ? '<p>«Обновить основу черновика» устранит конфликт с диском и сохранит ваши непересекающиеся правки. Это действие обновляет только черновик. Файлы сервера и его состояние останутся без изменений. Затем отдельно сохраните или примените настройки.</p>'
      : `<p class="editor-error">${esc(result.rebaseError || "Загрузите профиль заново для проверки конфликта.")}</p>`;
    const preview = canRebase ? Object.entries(result.rebaseDiff || {}).map(([kind, diff]) => `<h4>Правки после объединения · ${esc(kind)}</h4><pre class="config-diff">${esc(diff || "Нет пользовательских правок")}</pre>`).join("") : "";
    modal.open({ title: "Обновление основы черновика", okLabel: canRebase ? "Обновить основу черновика" : "Закрыть", bodyHTML: `${explanation}${compact ? "" : diffHtml(result)}${preview}`, onConfirm: canRebase ? async () => {
      if (draft.draftRevision !== reviewed || sourceDirty || fieldDirty || pendingFields.size || unsaved.length) throw new Error("Черновик изменился после просмотра. Проверьте объединение заново.");
      draft = await call("/api/config-draft", { file, draftRevision: reviewed, currentRevision: result.currentRevision, rebase: true });
      clearError(); renderFields(); renderSources(); await loadMods(); updateBar();
      toast("Основа черновика обновлена. Теперь можно проверить и сохранить настройки.", "ok");
    } : undefined });
  }
  async function confirmApply(prepare = false, restart = true) {
    if (!draft || S.demo) return;
    try {
      const result = await validate(prepare);
      if (!result.valid) {
        if (Object.keys(result.conflictDiff || {}).length) { showConflict(result); return; }
        error(result.errors.map(e => e.message).join("; ")); return;
      }
      const revisionAtReview = result.draftRevision;
      const backupDefault = result.modChanges || prepare;
      const options = `<div class="apply-options">${restart ? '<label class="field">Предупредить игроков<select id="editorWarn"><option value="300">За 5 минут</option><option value="600">За 10 минут</option><option value="60">За 1 минуту</option><option value="0">Без предупреждения</option></select></label>' : ""}<label class="check"><input type="checkbox" id="editorBackup" ${backupDefault ? "checked" : ""} aria-describedby="editorBackupHint" />Создать бэкап мира перед записью</label><p class="hint" id="editorBackupHint">${backupDefault ? "При изменении модов бэкап включён по умолчанию. Снимите флажок, чтобы применить изменения без него." : "Можно записать настройки без бэкапа мира или включить его этим флажком."} История конфигурации сохраняется в любом случае.</p></div>`;
      modal.open({ title: prepare ? "Загрузить Workshop items?" : restart ? "Применить конфигурацию?" : "Сохранить файлы?", bodyHTML: `<p>${prepare ? "Первый рестарт загрузит пакеты. Прежние ModID и остальные настройки останутся без изменений." : restart ? "Сервер сохранит мир, остановится, применит конфигурацию и запустится." : "Файлы будут записаны при остановленном сервере."}</p>${options}${diffHtml(result)}`, onConfirm: async () => {
        if (draft.draftRevision !== revisionAtReview || sourceDirty || fieldDirty || pendingFields.size) throw new Error("Черновик изменился после просмотра. Проверьте изменения заново");
        await call("/api/action", { op: prepare ? "prepare-workshop" : "apply-config", file, draftRevision: revisionAtReview, restart, backupBeforeApply: $("editorBackup").checked, warnSeconds: restart ? Number($("editorWarn").value) : 0 });
        toast("Операция запущена. Прогресс отображается в панели сервера.", "ok");
        draft.status = "applying";
        updateBar();
      } });
    } catch (e) { error(e.message); }
  }
  async function history() {
    if (!file) return;
    const profile = file, request = ++historyRequest;
    $("configHistory").innerHTML = '<p class="hint">Загружается история выбранного профиля…</p>';
    try {
      const data = await call(`/api/config-history?file=${encodeURIComponent(profile)}`);
      if (request !== historyRequest || file !== profile || configTab !== "history") return;
      if ($("configError").dataset.source === "history") clearError();
      $("configHistory").innerHTML = (data.items || []).map(r => `<div class="history-row"><strong>${esc(fmtTime(r.at))}</strong><span>${esc(r.reason)}</span><button type="button" class="btn small" data-profile="${esc(profile)}" data-restore-config="${esc(r.id)}">Посмотреть / восстановить…</button></div>`).join("") || '<p class="hint">История появится после первого сохранения конфигурации.</p>';
    } catch (failure) {
      if (request !== historyRequest || file !== profile || configTab !== "history") return;
      $("configHistory").textContent = "Не удалось загрузить историю: " + failure.message;
      error(failure.message);
      $("configError").dataset.source = "history";
    }
    updateBar();
  }
  $("configProfile").addEventListener("change", async () => {
    const next = $("configProfile").value;
    if (!next) { $("configProfile").value = file; return; }
    try {
      await chain;
      await flushFields();
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
      await chain; await flushFields(); if (unsaved.length) throw new Error("Нет связи: сначала повторите сохранение черновика или отмените изменения"); await flushSources(); configTab = button.dataset.tab;
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
  $("configSearch").addEventListener("input", () => { chain.then(renderFields); });
  function trackField(e) {
    const field = e.target.closest("[data-key]"); if (!field) return;
    pendingFields.set(`${field.dataset.kind}:${field.dataset.key}`, field);
    $("draftSaved").textContent = "Поле ещё не сохранено в черновик";
    updateBar();
  }
  $("configFields").addEventListener("input", trackField);
  $("modMapEditor").addEventListener("input", trackField);
  $("configFields").addEventListener("change", e => {
    const field = e.target.closest("[data-key]"); if (!field) return;
    pendingFields.set(`${field.dataset.kind}:${field.dataset.key}`, field);
    if (!field.checkValidity()) { field.reportValidity(); return; }
    const value = formValue(field);
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
  $("sourceSave").addEventListener("click", () => flushFields().then(flushSources).catch(e => error(e.message)));
  function selectWorkshopCandidates(result, profile) {
    const current = new Set(mods.workshop.map(w => w.workshopId));
    const candidates = [...new Map(result.items.map(w => [w.workshopId, w])).values()];
    modal.open({ title: result.source?.title || "Пакеты Steam-коллекции", okLabel: "Добавить выбранные", bodyHTML: `<p>Выберите пакеты для черновика <strong>${esc(profile)}</strong>. ModID выберете после загрузки.</p><div class="collection-tools"><label for="collectionQuery">Поиск пакета</label><input type="search" id="collectionQuery" placeholder="Название или Workshop ID…" /><div class="editor-toolbar"><button type="button" class="btn small" id="collectionSelect">Выбрать найденные</button><button type="button" class="btn small" id="collectionClear">Снять найденные</button></div><p id="collectionCount" class="hint" role="status"></p></div><div id="collectionCandidates">${candidates.map(w => `<label class="collection-candidate" data-candidate-query="${esc(`${w.title} ${w.workshopId}`.toLowerCase())}"><input type="checkbox" value="${esc(w.workshopId)}" ${current.has(w.workshopId) ? "disabled" : ""} /><span><strong>${esc(w.title || w.workshopId)}</strong><code>${esc(w.workshopId)}</code>${current.has(w.workshopId) ? '<span class="hint">Уже в конфигурации</span>' : ""}</span></label>`).join("") || '<p class="hint">В коллекции нет пакетов для добавления.</p>'}</div><p id="collectionEmpty" class="hint" hidden>Нет пакетов, соответствующих поиску.</p>`, onConfirm: async () => {
      if (file !== profile) throw new Error("Профиль изменился. Проверьте коллекцию заново.");
      const selected = [...$("collectionCandidates").querySelectorAll("input:checked:not(:disabled)")].map(input => input.value);
      if (!selected.length) throw new Error("Выберите хотя бы один пакет");
      await patch(() => ({ mods: { items: [...new Set([...mods.workshop.map(w => w.workshopId), ...selected])] } }));
      $("workshopInput").value = "";
    } });
    const rows = [...$("collectionCandidates").querySelectorAll(".collection-candidate")];
    function updateCandidates() {
      const query = $("collectionQuery").value.trim().toLowerCase();
      rows.forEach(row => { row.hidden = !row.dataset.candidateQuery.includes(query); });
      const selected = rows.filter(row => row.querySelector("input").checked).length;
      const found = rows.filter(row => !row.hidden).length;
      $("collectionCount").textContent = `Выбрано пакетов: ${selected} · найдено ${found} из ${candidates.length}${current.size && candidates.some(w => current.has(w.workshopId)) ? ` · уже в конфигурации ${candidates.filter(w => current.has(w.workshopId)).length}` : ""}`;
      $("collectionEmpty").hidden = found > 0;
      $("modalOk").disabled = !selected;
    }
    $("collectionQuery").addEventListener("input", updateCandidates);
    $("collectionCandidates").addEventListener("change", updateCandidates);
    for (const [id, checked] of [["collectionSelect", true], ["collectionClear", false]]) $(id).addEventListener("click", () => {
      rows.filter(row => !row.hidden).forEach(row => { const input = row.querySelector("input"); if (!input.disabled) input.checked = checked; }); updateCandidates();
    });
    updateCandidates(); $("collectionQuery").focus();
  }
  $("workshopAdd").addEventListener("submit", async e => {
    e.preventDefault(); if (!draft || !mods || resolvingWorkshop || S.op?.active || loading || S.demo) return;
    const profile = file;
    resolvingWorkshop = true; updateBar();
    try {
      const result = await call("/api/workshop-resolve", { input: $("workshopInput").value });
      if (file !== profile) throw new Error("Профиль изменился во время проверки Steam. Добавьте пакет в нужном профиле заново.");
      if (S.op?.active || loading) throw new Error("Дождитесь завершения операции и проверьте Steam-пакет заново");
      if (result.source?.kind === "collection" || result.items.length > 1) { selectWorkshopCandidates(result, profile); return; }
      await patch(() => ({ mods: { items: [...new Set([...mods.workshop.map(w => w.workshopId), ...result.items.map(w => w.workshopId)])] } }));
      $("workshopInput").value = "";
    } catch (err) { error(err.message); } finally { resolvingWorkshop = false; updateBar(); }
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
  function renderOrder() {
    if (!mods) return;
    const names = new Map(mods.workshop.flatMap(w => w.available || []).map(r => [r.modId, r.name]));
    const query = $("orderQuery").value.trim().toLowerCase();
    const entries = mods.mods.map((mid, index) => ({ mid, index, name: names.get(mid) || "" }))
      .filter(r => `${r.mid} ${r.name}`.toLowerCase().includes(query));
    $("orderCount").textContent = query ? `Найдено ${entries.length} из ${mods.mods.length} · номера позиций сохранены` : `Всего ${mods.mods.length} · позиция 1 загружается первой`;
    $("modOrderList").innerHTML = entries.map(({ mid, index, name }) => `<div class="order-row" data-order-id="${esc(mid)}">
      <button type="button" class="btn order-grip" draggable="true" aria-label="Перетащить ${esc(mid)}" title="Потяните для изменения порядка; с клавиатуры используйте «Переместить…»"><svg width="18" height="24" viewBox="0 0 18 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="5" r="2"/><circle cx="13" cy="5" r="2"/><circle cx="5" cy="12" r="2"/><circle cx="13" cy="12" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="13" cy="19" r="2"/></svg></button>
      <span class="order-position" aria-label="Позиция ${index + 1}">${index + 1}</span>
      <div class="order-identity"><code>${esc(mid)}</code>${name && name !== mid ? `<span class="hint">${esc(name)}</span>` : ""}</div>
      <div class="order-actions"><button type="button" class="btn small order-step" data-move-id="${esc(mid)}" data-direction="-1" data-edge="${index === 0}" aria-label="Поднять ${esc(mid)}" title="На одну позицию выше">↑</button><button type="button" class="btn small order-step" data-move-id="${esc(mid)}" data-direction="1" data-edge="${index === mods.mods.length - 1}" aria-label="Опустить ${esc(mid)}" title="На одну позицию ниже">↓</button><button type="button" class="btn small" data-position-id="${esc(mid)}" aria-label="Переместить ${esc(mid)}">Переместить…</button></div>
    </div>`).join("") || '<p class="hint">Нет модов, соответствующих поиску.</p>';
    updateBar();
  }
  function focusOrder(mid, direction) {
    const row = [...$("modOrderList").querySelectorAll("[data-order-id]")].find(el => el.dataset.orderId === mid);
    const step = direction && row?.querySelector(`[data-direction="${direction}"]`);
    const button = step && !step.disabled ? step : row?.querySelector("[data-position-id]");
    if (button) { button.focus({ preventScroll: true }); row.scrollIntoView({ block: "nearest" }); }
  }
  async function move(mid, target, direction) {
    if (S.demo || S.op?.active || loading) throw new Error("Редактор сейчас недоступен");
    let previous = 0, position = 0;
    await patch(() => {
      const index = mods.mods.indexOf(mid);
      if (index < 0) throw new Error("Мод больше не выбран в этом профиле");
      const list = mods.mods.filter(m => m !== mid);
      const destination = typeof target === "function" ? target(index, mods.mods) : target;
      if (!Number.isInteger(destination) || destination < 0 || destination > list.length) throw new Error(`Укажите целую позицию от 1 до ${mods.mods.length}`);
      previous = index + 1; position = destination + 1;
      list.splice(destination, 0, mid);
      return { mods: { selected: list } };
    });
    $("orderAnnouncement").textContent = `${mid}: позиция ${previous} → ${position}. Черновик сохранён.`;
    focusOrder(mid, direction);
  }
  function showPosition(mid) {
    const profile = file, total = mods.mods.length, index = mods.mods.indexOf(mid);
    if (index < 0) return;
    modal.open({ title: "Переместить мод", okLabel: "Переместить", bodyHTML: `<div class="order-move-dialog"><code>${esc(mid)}</code><p class="hint">Сейчас на позиции ${index + 1} из ${total}. Укажите итоговую позицию в полном списке. Изменится только черновик порядка модов.</p><div class="order-quick"><button type="button" class="btn" data-position-shortcut="1" ${index === 0 ? "disabled" : ""}>В начало</button><button type="button" class="btn" data-position-shortcut="${total}" ${index === total - 1 ? "disabled" : ""}>В конец</button></div><label for="orderDestination">Новая позиция</label><input id="orderDestination" type="number" inputmode="numeric" min="1" max="${total}" step="1" value="${index + 1}" required aria-describedby="orderPositionHint orderPositionError" /><p id="orderPositionHint" class="hint">От 1 до ${total}. Остальные моды сохранят взаимный порядок.</p><p id="orderPositionError" class="field-error" hidden></p></div>`, onConfirm: async () => {
      const input = $("orderDestination"), position = Number(input.value);
      if (!input.value.trim() || !Number.isInteger(position) || position < 1 || position > total) {
        $("orderPositionError").textContent = `Укажите целую позицию от 1 до ${total}`;
        $("orderPositionError").hidden = false; input.setAttribute("aria-invalid", "true"); input.focus();
        throw new Error($("orderPositionError").textContent);
      }
      if (file !== profile) throw new Error("Профиль изменился во время редактирования");
      await move(mid, position - 1);
    } });
    const input = $("orderDestination");
    input.addEventListener("input", () => { $("orderPositionError").hidden = true; input.removeAttribute("aria-invalid"); });
    input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); $("modalOk").click(); } });
    $("modalBody").querySelectorAll("[data-position-shortcut]").forEach(button => button.addEventListener("click", () => { input.value = button.dataset.positionShortcut; $("modalOk").click(); }));
    input.focus(); input.select();
  }
  $("modOrder").addEventListener("click", e => {
    const position = e.target.closest("[data-position-id]");
    if (position) { showPosition(position.dataset.positionId); return; }
    const button = e.target.closest("[data-move-id]");
    if (button) move(button.dataset.moveId, index => Math.max(0, Math.min(index + Number(button.dataset.direction), mods.mods.length - 1)), button.dataset.direction).catch(() => {});
  });
  function clearDropTarget() { $("modOrderList").querySelectorAll("[data-drop]").forEach(row => delete row.dataset.drop); }
  function endDrag() { dragId = null; clearDropTarget(); $("modOrderList").querySelectorAll(".dragging").forEach(row => row.classList.remove("dragging")); }
  $("modOrder").addEventListener("dragstart", e => {
    const grip = e.target.closest(".order-grip"), row = grip?.closest("[data-order-id]");
    if (!row || grip.disabled || S.demo || S.op?.active) { e.preventDefault(); return; }
    dragId = row.dataset.orderId; row.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", dragId);
  });
  $("modOrder").addEventListener("dragover", e => {
    const row = e.target.closest("[data-order-id]"); clearDropTarget();
    if (!dragId || !row || row.dataset.orderId === dragId) return;
    e.preventDefault(); e.dataTransfer.dropEffect = "move";
    const box = row.getBoundingClientRect(); row.dataset.drop = e.clientY < box.top + box.height / 2 ? "before" : "after";
  });
  $("modOrder").addEventListener("dragleave", e => { if (!$("modOrderList").contains(e.relatedTarget)) clearDropTarget(); });
  $("modOrder").addEventListener("drop", e => {
    const row = e.target.closest("[data-order-id]"), mid = dragId, after = row?.dataset.drop === "after";
    if (mid && row && row.dataset.drop) {
      e.preventDefault(); const targetId = row.dataset.orderId;
      move(mid, (index, list) => { const targetIndex = list.indexOf(targetId); if (targetIndex < 0) throw new Error("Позиция назначения больше недоступна"); const slot = targetIndex + (after ? 1 : 0); return slot - (index < slot ? 1 : 0); }).catch(() => {});
    }
    endDrag();
  });
  $("modOrder").addEventListener("dragend", endDrag);
  $("orderQuery").addEventListener("input", renderOrder);
  for (const id of ["modQuery", "modFilter", "modSortNew"]) $(id).addEventListener(id === "modQuery" ? "input" : "change", renderMods);
  $("modRescan").addEventListener("click", () => loadMods(true).catch(e => error(e.message)));
  $("modExport").addEventListener("click", async () => {
    try {
      await chain; await flushFields(); await flushSources();
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
      await flushFields();
      if (unsaved.length) throw new Error("Нет связи: сначала повторите сохранение черновика или отмените изменения");
      await flushSources();
      draft = await call("/api/modpack", { file, draftRevision: draft.draftRevision, pack });
      renderFields(); renderSources(); await loadMods(); updateBar();
    } catch (err) { error(err.message); } finally { e.target.value = ""; }
  });
  $("configDiff").addEventListener("click", async () => {
    try {
      const result = await validate();
      if (Object.keys(result.conflictDiff || {}).length) { showConflict(result); return; }
      modal.open({ title: "Изменения конфигурации", okLabel: "Закрыть", bodyHTML: diffHtml(result) });
    } catch (e) { error(e.message); }
  });
  $("configRebase").addEventListener("click", async () => {
    try {
      const result = await validate();
      if (!result.rebaseAvailable) throw new Error(result.rebaseError || "Нет конфликта для объединения. Загрузите профиль заново.");
      showConflict(result, true);
    } catch (e) { error(e.message); }
  });
  $("draftRetry").addEventListener("click", () => flushFields().then(() => patch({})).catch(e => error(e.message)));
  $("configDiscard").addEventListener("click", () => modal.open({ title: "Отменить черновик?", bodyHTML: "Будет загружена текущая конфигурация с диска. Уже записанные настройки и скачанные пакеты сохранятся.", onConfirm: async () => { unsaved = []; await patch({ discard: true }); pendingFields.clear(); sourceDirty = false; renderFields(); renderSources(); renderMods(); } }));
  $("configSave").addEventListener("click", () => confirmApply(false, false));
  $("configApply").addEventListener("click", () => confirmApply(false, true));
  async function verifyRunning() {
    if (S.demo || S.op?.active || loading) return;
    try {
      await chain; await flushFields(); await flushSources();
      if (unsaved.length) throw new Error("Сначала сохраните черновик после восстановления связи");
      loading = true; updateBar();
      const current = await call(`/api/config-draft?file=${encodeURIComponent(file)}`);
      if (current.draftRevision !== draft.draftRevision) throw new Error("Черновик изменён другой вкладкой. Загрузите профиль заново");
      draft = await call("/api/config-verify", { file, draftRevision: draft.draftRevision, currentRevision: current.currentRevision });
      clearError(); renderFields(); renderSources(); await loadMods(); updateBar();
      toast(draft.state?.installation ? "Пакеты загружены. Выберите ModID для второго этапа." : "Записанная конфигурация и готовность сервера подтверждены.", "ok");
    } catch (failure) { error(failure.message); }
    finally { loading = false; updateBar(); }
  }
  $("configVerify").addEventListener("click", verifyRunning);
  $("installNotice").addEventListener("click", e => { if (e.target.closest("[data-verify-running]")) verifyRunning(); });
  function operationLogs() {
    S.logsProfile = file;
    S.logsSince = Date.parse(draft?.state?.operationStartedAt || "") || 0;
    S.logsUntil = Date.parse(draft?.state?.operationCompletedAt || "") || 0;
    renderLogsFiltered(); location.hash = "#/console";
    refreshLogs();
  }
  $("editorLogs").addEventListener("click", operationLogs);
  $("draftMore").addEventListener("click", () => {
    const open = $("draftMore").getAttribute("aria-expanded") !== "true";
    $("draftMore").setAttribute("aria-expanded", String(open));
    $("draftExtra").dataset.open = String(open);
  });
  document.addEventListener("click", e => { if (!e.target.closest("#draftExtra, #draftMore")) { $("draftExtra").dataset.open = "false"; $("draftMore").setAttribute("aria-expanded", "false"); } });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && $("draftExtra").dataset.open === "true") { $("draftExtra").dataset.open = "false"; $("draftMore").setAttribute("aria-expanded", "false"); $("draftMore").focus(); } });
  function restoreHistory(id) {
    const profile = file;
    modal.open({ title: "Восстановить версию в черновик?", bodyHTML: `Профиль <strong>${esc(profile)}</strong>. Текущий черновик будет заменён. После загрузки просмотрите различия и отдельно примените изменения.`, onConfirm: async () => {
      await chain;
      if (file !== profile || loading || S.op?.active) throw new Error("Профиль или состояние сервера изменились. Откройте восстановление заново.");
      draft = await call("/api/config-history", { file: profile, historyId: id, draftRevision: draft.draftRevision });
      pendingFields.clear();
      unsaved = []; sourceDirty = fieldDirty = false;
      renderFields(); renderSources(); await loadMods(); updateBar();
      const result = await validate();
      setTimeout(() => modal.open({ title: "Изменения при восстановлении", bodyHTML: diffHtml(result), onConfirm: async () => {} }), 0);
    } });
  }
  $("configOperationResult").addEventListener("click", e => {
    if (e.target.closest("[data-verify-running]")) verifyRunning();
    if (e.target.closest("[data-operation-logs]")) operationLogs();
    if (e.target.closest("[data-operation-restore]") && draft?.state?.historyId) restoreHistory(draft.state.historyId);
  });
  $("configHistory").addEventListener("click", e => {
    const button = e.target.closest("[data-restore-config]"); if (!button) return;
    if (button.dataset.profile !== file || loading || S.op?.active) return;
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
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("moreMenu").hidden) { $("moreMenu").hidden = true; $("navMore").setAttribute("aria-expanded", "false"); $("navMore").focus(); } });
  document.addEventListener("click", e => { if (!e.target.closest("#moreMenu, #navMore")) { $("moreMenu").hidden = true; $("navMore").setAttribute("aria-expanded", "false"); } });
  window.addEventListener("beforeunload", e => { if (sourceDirty || fieldDirty || pendingFields.size) { e.preventDefault(); e.returnValue = ""; } });
  window.addEventListener("hashchange", updateBar);
  setInterval(async () => {
    if (document.hidden) return;
    updateBar();
    const active = !!S.op?.active;
    if ((previousOp || draft?.status === "applying") && !active && file && !loading && !sourceDirty && !fieldDirty && !pendingFields.size) {
      try { await loadProfile(file); } catch (e) { error(e.message); }
    }
    previousOp = active;
  }, 1500);
  init();
  return {
    get file() { return file; },
    operationChanged() { if (S.op?.active) endDrag(); updateBar(); },
    route(view) { updateBar(); if (view === "mods") updateModTab(); },
    background() {
      // Live registry payloads contain committed state, not this editor's draft.
      if (!file || sourceDirty || fieldDirty || pendingFields.size || loading || Date.now() - lastRefresh < 15000) return;
      lastRefresh = Date.now();
      const profile = file, requestedRevision = draft?.draftRevision;
      call(`/api/config-draft?file=${encodeURIComponent(profile)}`).then(data => {
        if (data.file !== file || profile !== file || sourceDirty || fieldDirty || pendingFields.size || loading || draft?.draftRevision !== requestedRevision) return;
        if (draft && data.draftRevision !== draft.draftRevision) { draft.conflict = true; $("draftSaved").textContent = "Черновик изменён другой вкладкой. Перевыберите профиль для загрузки."; }
        else if (draft) { draft.conflict = data.conflict; draft.status = data.status; draft.state = data.state; }
        updateBar();
      }).catch(() => { /* retain last visible data on disconnect */ });
    },
  };
})();
