"""Application process logs, independent of event persistence and notifications."""

import logging
import sys
import unicodedata

import config

_LEVELS = {
    name: getattr(logging, name) for name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
}
_EVENT_LEVELS = {"error": "ERROR", "rcon-error": "ERROR", "warn": "WARNING"}


def level_name(value):
    """Accept standard level names and WARN, without accepting arbitrary integers."""
    name = str(value).strip().upper()
    name = "WARNING" if name == "WARN" else name
    if name not in _LEVELS:
        raise ValueError("Unknown application log level")
    return name


def event_level(kind, level=None):
    return level_name(level) if level is not None else _EVENT_LEVELS.get(kind, "INFO")


def sanitize(value):
    """Keep untrusted values on one terminal line, including Unicode controls."""
    return "".join(
        (f"\\x{ord(char):02x}" if ord(char) <= 255 else f"\\u{ord(char):04x}")
        if unicodedata.category(char).startswith("C") or char in "\u2028\u2029"
        else char
        for char in str(value)
    )


class _Formatter(logging.Formatter):
    def format(self, record):
        # Do not append exception objects or tracebacks: transport diagnostics can
        # contain credentials. Callers log safe context and exception categories.
        return (
            f"{self.formatTime(record, '%Y-%m-%dT%H:%M:%S')} "
            f"{record.levelname} {sanitize(record.name)} {sanitize(record.getMessage())}"
        )


class _StderrHandler(logging.StreamHandler):
    def emit(self, record):
        # Resolve stderr at emit time so redirection and embedded callers work.
        self.stream = sys.stderr
        super().emit(record)

    def handleError(self, record):
        # Best-effort diagnostics must not abort operations or notifications, and
        # the stdlib fallback would expose raw arguments/tracebacks on stderr.
        pass


def get_logger(name):
    return logging.getLogger(f"pz.{name}")


def configure(level=None):
    """Configure only our namespace; preserve host and library logging handlers."""
    invalid = False
    try:
        name = level_name(config.CFG.get("log_level", "INFO") if level is None else level)
    except ValueError:
        name, invalid = "INFO", True
    logger = logging.getLogger("pz")
    logger.setLevel(_LEVELS[name])
    logger.propagate = False
    handler = next((item for item in logger.handlers if isinstance(item, _StderrHandler)), None)
    if handler is None:
        handler = _StderrHandler()
        handler.setFormatter(_Formatter())
        logger.addHandler(handler)
    handler.setLevel(_LEVELS[name])
    if invalid:
        get_logger("logging").warning("Invalid PZ_LOG_LEVEL; using INFO")
    return logger
