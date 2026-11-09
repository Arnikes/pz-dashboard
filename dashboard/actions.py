"""Validate and dispatch dashboard actions without depending on an HTTP handler."""

from functools import partial

import ops
import dashboardupdate
from extensions import ActionContext, ActionSpec, ExtensionRegistry, extension_failure


class ActionError(Exception):
    """An action rejection with the status exposed by the existing API."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def builtin_actions():
    """Bind workers at dispatch time so operation implementations remain replaceable."""
    return (
        ActionSpec("start", lambda data, context: ops._do_start),
        ActionSpec("stop", lambda data, context: partial(ops._do_stop, context.warn_seconds)),
        ActionSpec("restart", lambda data, context: partial(ops._do_restart, context.warn_seconds)),
        ActionSpec("check-update", lambda data, context: ops._do_check_update),
        ActionSpec("check-dashboard-update", lambda data, context: ops._do_check_dashboard_update),
        ActionSpec(
            "apply-update",
            lambda data, context: partial(
                ops._do_apply_update, context.warn_seconds, "Обновление сервера"
            ),
            lambda data, context: ops.defer_next_check(
                context.settings["autoUpdate"]["intervalHours"]
            ),
        ),
        ActionSpec(
            "apply-dashboard-update",
            lambda data, context: partial(dashboardupdate.apply, ops._set_phase),
        ),
        ActionSpec("check-mods-update", lambda data, context: ops._do_check_mods_update),
        ActionSpec(
            "apply-mods-update",
            lambda data, context: partial(ops._do_apply_mods_update, context.warn_seconds),
        ),
        ActionSpec(
            "backup",
            lambda data, context: partial(
                ops.run_backup_job, "manual", data.get("stopServer", False)
            ),
        ),
        ActionSpec(
            "verify-backup",
            lambda data, context: partial(ops.verify_backup, data.get("name") or ""),
        ),
        ActionSpec(
            "restore", lambda data, context: partial(ops._do_restore, data.get("name") or "")
        ),
    )


_BUILTINS = ExtensionRegistry(actions=builtin_actions())


def _expected_error(error):
    """Publish authored rejections only after validating their actual wire text."""
    message = str(error)
    message.encode("utf-8")
    if isinstance(error, ActionError):
        if type(error.status) is not int or not 400 <= error.status <= 599:
            raise ValueError("An action rejection needs an HTTP error status")
        return ActionError(error.status, message)
    if isinstance(error, ops.OpsErrorReported):
        return ops.OpsErrorReported(message)
    return ops.OpsError(message)


def _custom_callback(callback, owner, *args):
    try:
        return callback(*args)
    except (ActionError, ops.OpsError) as error:
        try:
            rejection = _expected_error(error)
        except Exception as invalid:  # noqa: BLE001 — malformed expected diagnostics are unsafe too
            raise ActionError(500, extension_failure(owner, invalid)) from None
        raise rejection from None
    except Exception as error:  # noqa: BLE001 — a plugin must not abort its HTTP request
        raise ActionError(500, extension_failure(owner, error)) from None


def _custom_worker(callback, owner):
    def worker():
        try:
            result = callback()
            if result is None or (type(result) is str and result in ("cancelled", "aborted")):
                return result
            # The dashboard-update handoff belongs to the host, and arbitrary
            # result objects must not execute comparisons inside its lifecycle.
            raise TypeError("An extension worker must return None, cancelled or aborted")
        except (ActionError, ops.OpsError) as error:
            try:
                rejection = _expected_error(error)
            except Exception as invalid:  # noqa: BLE001 — keep invalid text out of history/events
                raise ops.OpsError(extension_failure(owner, invalid)) from None
            # A worker rejection uses the existing operation history/event policy.
            if isinstance(rejection, ActionError):
                rejection = ops.OpsError(str(rejection))
            raise rejection from None
        except Exception as error:  # noqa: BLE001 — keep raw exceptions out of events/history
            raise ops.OpsError(extension_failure(owner, error)) from None

    return worker


def dispatch(data, *, registry=None):
    registry = _BUILTINS if registry is None else registry
    action = data.get("op")
    if not isinstance(action, str):
        raise ActionError(400, "Неизвестная операция")
    if action == "resolve-request":
        try:
            return ops.resolve_operation_request(data.get("requestId"), data.get("requestEpoch"))
        except ops.OpsError as error:
            raise ActionError(409, str(error)) from error
    # Отмена относится к занятой операции и не запускает нового worker.
    if action in {"cancel-mods-update", "cancel-update"}:
        operation_id = data.get("operationId")
        if "operationId" in data and (
            not isinstance(operation_id, str) or not operation_id.strip()
        ):
            raise ActionError(400, "Неизвестная операция")
        cancel_options = {"operation_id": operation_id} if "operationId" in data else {}
        try:
            if action == "cancel-update":
                ops.cancel_image_update(**cancel_options)
            else:
                ops.cancel_mods_update(**cancel_options)
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

    spec = registry.actions.get(action)
    if spec is None:
        raise ActionError(400, "Неизвестная операция")
    if ops.op_busy():
        raise ActionError(409, "Уже выполняется другая операция")

    context = ActionContext(settings, warn)
    owner = registry.action_owners.get(action)
    try:
        if owner is None:
            worker = spec.prepare(data, context)
        else:

            def prepare():
                prepared = spec.prepare(data, context)
                if not callable(prepared):
                    raise TypeError("An action must prepare a callable worker")
                return prepared

            worker = _custom_worker(_custom_callback(prepare, owner), owner)
        with ops.mutation_guard():
            if spec.before_start is not None:
                if owner is None:
                    spec.before_start(data, context)
                else:
                    _custom_callback(spec.before_start, owner, data, context)
            operation_id = ops.start_op(action, worker)
    except ops.OpsError as error:
        raise ActionError(409, str(error)) from error
    result = {"ok": True, "started": action}
    if isinstance(operation_id, str):
        result.update(operationId=operation_id, operationState=ops.op_state())
    return result
