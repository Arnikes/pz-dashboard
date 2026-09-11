"""Validate and dispatch dashboard actions without depending on an HTTP handler."""

from functools import partial

import ops


class ActionError(Exception):
    """An action rejection with the status exposed by the existing API."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def dispatch(data):
    action = data.get("op")
    settings = ops.get_settings()
    warn_default = settings["autoUpdate"]["warnSeconds"]
    try:
        warn = max(0, min(3600, int(data.get("warnSeconds", warn_default))))
    except (TypeError, ValueError):
        warn = warn_default

    # Bind arguments now; start_op executes the selected callable in a worker.
    workers = {
        "start": ops._do_start,
        "stop": partial(ops._do_stop, warn),
        "restart": partial(ops._do_restart, warn),
        "apply-update": partial(ops._do_apply_update, warn, "Обновление сервера"),
        "check-mods-update": partial(ops.check_mods_update, source="manual"),
        "apply-mods-update": partial(ops._do_apply_mods_update, warn),
        "backup": partial(ops.run_backup_job, "manual", bool(data.get("stopServer", False))),
        "verify-backup": partial(ops.verify_backup, data.get("name") or ""),
        "restore": partial(ops._do_restore, data.get("name") or ""),
    }
    if action != "check-update" and action not in workers:
        raise ActionError(400, "Неизвестная операция")
    if ops.op_busy():
        raise ActionError(409, "Уже выполняется другая операция")

    try:
        if action == "check-update":
            return {"ok": True, "check": ops.check_update(force_event=True)}
        if action == "apply-update":
            # Persist the deferral: get_settings returns a copy.
            ops.defer_next_check(settings["autoUpdate"]["intervalHours"])
        ops.start_op(action, workers[action])
    except ops.OpsError as error:
        raise ActionError(409, str(error)) from error
    return {"ok": True, "started": action}
