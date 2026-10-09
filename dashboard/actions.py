"""Validate and dispatch dashboard actions without depending on an HTTP handler."""

from functools import partial

import ops
import dashboardupdate


class ActionError(Exception):
    """An action rejection with the status exposed by the existing API."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def dispatch(data):
    action = data.get("op")
    if not isinstance(action, str):
        raise ActionError(400, "Неизвестная операция")
    # Отмена относится к занятой операции и не запускает нового worker.
    if action == "cancel-mods-update":
        try:
            ops.cancel_mods_update()
        except ops.OpsError as error:
            raise ActionError(409, str(error)) from error
        return {"ok": True, "cancelRequested": True}
    if action == "backup" and type(data.get("stopServer", False)) is not bool:
        raise ActionError(400, "stopServer должен быть true или false")
    settings = ops.get_settings()
    warn_default = settings["autoUpdate"]["warnSeconds"]
    try:
        warn = max(0, min(3600, int(data.get("warnSeconds", warn_default))))
    except (TypeError, ValueError, OverflowError):
        warn = warn_default

    # Bind arguments now; start_op executes the selected callable in a worker.
    workers = {
        "start": ops._do_start,
        "stop": partial(ops._do_stop, warn),
        "restart": partial(ops._do_restart, warn),
        "check-update": ops._do_check_update,
        "check-dashboard-update": ops._do_check_dashboard_update,
        "apply-update": partial(ops._do_apply_update, warn, "Обновление сервера"),
        "apply-dashboard-update": partial(dashboardupdate.apply, ops._set_phase),
        "check-mods-update": ops._do_check_mods_update,
        "apply-mods-update": partial(ops._do_apply_mods_update, warn),
        "backup": partial(ops.run_backup_job, "manual", bool(data.get("stopServer", False))),
        "verify-backup": partial(ops.verify_backup, data.get("name") or ""),
        "restore": partial(ops._do_restore, data.get("name") or ""),
    }
    if action not in workers:
        raise ActionError(400, "Неизвестная операция")
    if ops.op_busy():
        raise ActionError(409, "Уже выполняется другая операция")

    try:
        if action == "apply-update":
            # Persist the deferral: get_settings returns a copy.
            ops.defer_next_check(settings["autoUpdate"]["intervalHours"])
        ops.start_op(action, workers[action])
    except ops.OpsError as error:
        raise ActionError(409, str(error)) from error
    return {"ok": True, "started": action}
