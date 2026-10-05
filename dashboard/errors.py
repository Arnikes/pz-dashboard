"""Shared domain errors, independent of HTTP and server operations."""


class OpsError(Exception):
    pass


class OpsErrorReported(OpsError):
    """OpsError, о которой уже сообщено (событие и журнал) — воркер не дублирует."""

    pass


class EditorError(OpsError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status
