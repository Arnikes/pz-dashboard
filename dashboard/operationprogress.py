"""Small, explicit progress recipes for the dashboard's built-in workers."""

from functools import wraps


STEPS = {
    "prepare": ("Подготовка…", ()),
    "warn": ("Предупреждение игроков", ()),
    "stop": ("Остановка", ("Остановка сервера", "Остановка для бэкапа")),
    "start": ("Запуск", ("Запуск сервера",)),
    "ready": ("Загрузка сервера", ()),
    "image-check": ("Проверка актуального образа", ()),
    "dashboard-check": ("Проверка обновлений пульта", ()),
    "mods-check": ("Проверка модов", ()),
    "mods-wait": ("Ожидание", ()),
    "image-download": ("Скачивание нового образа", ()),
    "backup": (
        "Страховочный бэкап",
        (
            "Бэкап",
            "Сохранение мира",
            "Ожидание записи",
            "Создание архива",
            "Проверка и распаковка в песочницу",
            "Выгрузка бэкапа в S3",
            "Проверка загруженного объекта S3",
            "Удаление локальной копии бэкапа",
        ),
    ),
    "recreate": ("Пересоздание контейнера", ()),
    "world-save": ("Сохранение мира", ()),
    "write-wait": ("Ожидание записи", ()),
    "archive": ("Создание архива", ()),
    "archive-verify": ("Проверка и распаковка в песочницу", ()),
    "s3-upload": ("Выгрузка бэкапа в S3", ()),
    "s3-verify": ("Проверка загруженного объекта S3", ()),
    "local-cleanup": ("Удаление локальной копии бэкапа", ()),
    "restore-download": ("Скачивание бэкапа из S3", ()),
    "restore-verify": ("Проверка архива перед восстановлением", ()),
    "restore-prepare": ("Подготовка файлов восстановления", ()),
    "restore-files": ("Восстановление файлов", ()),
    "config-write": ("Запись конфигурации", ()),
    "workshop-download": ("Загрузка пакетов", ()),
    "config-verify": ("Проверка конфигурации", ()),
    "dashboard-recreate": ("Пересоздание контейнера пульта", ()),
    "dashboard-ready": ("Проверка готовности пульта", ()),
}


def build(operation, options=None):
    """Freeze the route before launch; unknown branches remain skippable steps."""
    options = options or {}
    running = options.get("running") is not False
    warning = ["warn"] if options.get("warnSeconds", 1) != 0 and running else []
    stop = warning + ["stop"] if running else []
    launch = ["start", "ready"]
    if operation == "start":
        steps = launch
    elif operation == "stop":
        steps = warning + ["stop"]
    elif operation in {"restart", "mods-restart", "apply-mods-update"}:
        steps = stop + launch
    elif operation in {"check-update", "check-dashboard-update", "check-mods-update"}:
        steps = {
            "check-update": ["image-check"],
            "check-dashboard-update": ["dashboard-check"],
            "check-mods-update": ["mods-check", "mods-wait"],
        }[operation]
    elif operation == "apply-update":
        steps = ["image-check", "image-download"]
        if options.get("backupBeforeUpdate", True):
            steps += ["backup"]
        steps += stop + ["recreate", "ready"]
    elif operation == "apply-dashboard-update":
        steps = ["image-download", "dashboard-recreate", "dashboard-ready"]
    elif operation == "backup":
        stopping = options.get("stopServer")
        steps = []
        if running and stopping is not False:
            steps += ["warn", "stop"]
        if running and stopping is not True:
            steps += ["world-save", "write-wait"]
        steps += ["archive"]
        if running and stopping is not False:
            steps += launch
        steps += ["archive-verify"]
        if options.get("s3"):
            steps += ["s3-upload", "s3-verify", "local-cleanup"]
    elif operation == "verify-backup":
        steps = (["restore-download"] if options.get("s3") else []) + ["archive-verify"]
    elif operation == "restore":
        steps = (["restore-download"] if options.get("s3") else []) + [
            "restore-verify",
            "restore-prepare",
        ]
        steps += (["warn", "stop"] if running else []) + ["restore-files"] + launch
    elif operation in {"apply-config", "prepare-workshop"}:
        restart = operation == "prepare-workshop" or options.get("restart", False)
        steps = stop if running and restart else []
        if options.get("backupBeforeApply", True):
            steps += ["backup"]
        steps += ["config-write"]
        if restart:
            steps += [
                "workshop-download" if operation == "prepare-workshop" else "start",
                "ready",
                "config-verify",
            ]
    else:
        return None
    return [
        {
            "id": key,
            "label": "Бэкап"
            if key == "backup" and operation in {"apply-config", "prepare-workshop"}
            else STEPS[key][0],
            "status": "current" if key == "prepare" else "pending",
        }
        for key in ["prepare", *steps]
    ]


def planned(worker, operation, options):
    """Attach a frozen recipe without changing the public start_op contract."""

    @wraps(worker)
    def run():
        return worker()

    run.operation_plan = build(operation, options)
    return run


def advance(plan, phase, *, interrupted=False):
    """Advance visits, skipping unvisited branches; nested Done is not terminal."""
    if not plan or phase in {"Подготовка…", "Готово"}:
        return
    current = next((i for i, step in enumerate(plan) if step["status"] == "current"), -1)
    cancellation = next((step for step in plan if step["id"] == "cancel"), None)
    if cancellation is not None:
        # Accepted cancellation may still need archive/download cleanup. Those
        # nested visits update details without reviving the abandoned route.
        if phase in {"Отмена", "Отменено"}:
            cancellation["label"] = phase
        return
    if phase in {"Отмена", "Отменено"}:
        if current >= 0:
            plan[current]["status"] = "skipped"
        for step in plan:
            if step["status"] == "pending":
                step["status"] = "skipped"
        plan.insert(current + 1, {"id": "cancel", "label": phase, "status": "current"})
        return

    def matches(step):
        label, aliases = STEPS.get(step["id"], (step["label"], ()))
        return phase in (step["label"], label, *aliases)

    if current >= 0 and matches(plan[current]) and not interrupted:
        return
    target = next(
        (
            i
            for i in range(current + 1, len(plan))
            if plan[i]["status"] == "pending" and matches(plan[i])
        ),
        None,
    )
    if current >= 0:
        plan[current]["status"] = "skipped" if interrupted else "completed"
    if target is None:
        # A retry/recovery is a new visit, inserted before the remaining route.
        target = current + 1
        plan.insert(target, {"id": f"visit-{len(plan)}", "label": phase, "status": "pending"})
    for step in plan[current + 1 : target]:
        if step["status"] == "pending":
            step["status"] = "skipped"
    plan[target]["status"] = "current"


def finish(plan):
    """Only the outer worker's success settles the remaining route."""
    for step in plan or []:
        if step["status"] == "current":
            step["status"] = "completed"
        elif step["status"] == "pending":
            step["status"] = "skipped"
