"use strict";

// Submission previews contain only steps implied by the known request options.
// The admitted worker supplies the authoritative route, including conditional steps.
globalThis.OperationPlan = (() => {
  const labels = {
    prepare: "Подготовка…", warn: "Предупреждение игроков", stop: "Остановка",
    start: "Запуск", ready: "Загрузка сервера", "image-check": "Проверка актуального образа",
    "dashboard-check": "Проверка обновлений пульта", "mods-check": "Проверка модов",
    "mods-wait": "Ожидание", "image-download": "Скачивание нового образа",
    backup: "Страховочный бэкап", recreate: "Пересоздание контейнера",
    "world-save": "Сохранение мира", "write-wait": "Ожидание записи",
    archive: "Создание архива", "archive-verify": "Проверка и распаковка в песочницу",
    "s3-upload": "Выгрузка бэкапа в S3", "s3-verify": "Проверка загруженного объекта S3",
    "local-cleanup": "Удаление локальной копии бэкапа",
    "restore-verify": "Проверка архива перед восстановлением",
    "restore-prepare": "Подготовка файлов восстановления", "restore-files": "Восстановление файлов",
    "config-write": "Запись конфигурации", "workshop-download": "Загрузка пакетов",
    "config-verify": "Проверка конфигурации", "dashboard-recreate": "Пересоздание контейнера пульта",
    "dashboard-ready": "Проверка готовности пульта",
  };

  function preview(op, options = {}) {
    const ids = ["prepare"];
    const add = (...steps) => ids.push(...steps);
    const warn = () => { if (Number(options.warnSeconds) > 0 && options.running !== false) add("warn"); };
    const restart = () => { warn(); if (options.running !== false) add("stop"); add("start", "ready"); };
    switch (op) {
      case "start": add("start", "ready"); break;
      case "stop": warn(); add("stop"); break;
      case "restart": case "mods-restart": case "apply-mods-update": restart(); break;
      case "check-update": add("image-check"); break;
      case "check-dashboard-update": add("dashboard-check"); break;
      case "check-mods-update": add("mods-check", "mods-wait"); break;
      case "apply-update":
        add("image-check", "image-download");
        if (options.backupBeforeUpdate === true) add("backup");
        warn();
        if (options.running === true) add("stop");
        add("recreate", "ready");
        break;
      case "apply-dashboard-update": add("image-download", "dashboard-recreate", "dashboard-ready"); break;
      case "backup":
        if (options.running === true) add(...(options.stopServer ? ["warn", "stop"] : ["world-save", "write-wait"]));
        add("archive");
        if (options.running === true && options.stopServer) add("start", "ready");
        add("archive-verify");
        if (options.s3 === true) add("s3-upload", "s3-verify", "local-cleanup");
        break;
      case "verify-backup": add("archive-verify"); break;
      case "restore":
        add("restore-verify", "restore-prepare");
        if (options.running === true) add("warn", "stop");
        add("restore-files", "start", "ready");
        break;
      case "apply-config": case "prepare-workshop": {
        const launch = options.restart === true || op === "prepare-workshop";
        if (options.running === true && launch) { warn(); add("stop"); }
        if (options.backupBeforeApply === true) add("backup");
        add("config-write");
        if (launch) add(op === "prepare-workshop" ? "workshop-download" : "start", "ready", "config-verify");
        break;
      }
      default: break;
    }
    return ids.map((id, index) => ({ id, label: id === "backup" && ["apply-config", "prepare-workshop"].includes(op) ? "Бэкап" : labels[id], status: index ? "pending" : "current" }));
  }

  function valid(plan) {
    return Array.isArray(plan) && plan.length > 0
      && plan.every(step => step && typeof step.id === "string" && step.id
        && typeof step.label === "string" && step.label.trim()
        && ["pending", "current", "completed", "skipped"].includes(step.status))
      && new Set(plan.map(step => step.id)).size === plan.length
      && plan.filter(step => step.status === "current").length === 1;
  }

  return { preview, valid };
})();
