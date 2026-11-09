"""Explicit extension contracts and immutable, validated runtime registrations."""

import json
import math
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType

import logging_utils

EXTENSION_ERROR = "Ошибка модуля расширения"
RESERVED_ACTIONS = frozenset(
    {"resolve-request", "cancel-update", "cancel-mods-update", "apply-config", "prepare-workshop"}
)
RESERVED_CHANNELS = frozenset({"auth-expired", "message", "error", "open", "retry"})
_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z", re.ASCII)


@dataclass(frozen=True)
class ActionContext:
    """Detached settings and the host-normalized warning duration for one request."""

    settings: Mapping
    warn_seconds: int

    def __post_init__(self):
        object.__setattr__(self, "settings", MappingProxyType(deepcopy(dict(self.settings))))


@dataclass(frozen=True)
class ActionSpec:
    """Bind a zero-argument worker; the host owns admission and its lifecycle."""

    name: str
    prepare: Callable
    before_start: Callable | None = None


@dataclass(frozen=True)
class ChannelSpec:
    """One collector shared by direct polling and interval-based SSE snapshots."""

    name: str
    collect: Callable
    interval: float = 5.0
    route: str | None = None
    telemetry: bool = False


@dataclass(frozen=True)
class Plugin:
    """Trusted callbacks registered explicitly, without discovery or dynamic imports."""

    name: str
    actions: tuple[ActionSpec, ...] = ()
    channels: tuple[ChannelSpec, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "actions", tuple(self.actions))
        object.__setattr__(self, "channels", tuple(self.channels))


def _valid_name(value):
    return isinstance(value, str) and len(value) <= 64 and _NAME.fullmatch(value) is not None


def extension_failure(owner, error):
    """Log callback identity and exception category, never raw diagnostics or values."""
    logging_utils.get_logger("extensions").error(
        "Extension callback failed (plugin=%s, exception=%s)", owner, type(error).__name__
    )
    return EXTENSION_ERROR


def _plain_payload(value):
    """Detach custom results and bound presentation depth before HTTP/cache encoding."""
    if not isinstance(value, dict):
        raise TypeError("A channel collector must return a JSON object")
    value = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    pending = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > 64:
            raise ValueError("A channel payload is too deeply nested")
        children = current.values() if isinstance(current, dict) else current
        pending.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    return value


@dataclass(frozen=True, init=False)
class ExtensionRegistry:
    """Compile registrations atomically; every exposed collection is read-only.

    Host registrations retain their existing routes and error policies. Plugin
    routes are restricted to their own namespace and cannot replace host routes.
    """

    actions: Mapping[str, ActionSpec]
    channels: Mapping[str, ChannelSpec]
    poll_routes: Mapping[str, str]
    stream_plan: tuple[tuple[str, float], ...]
    plugins: tuple[Plugin, ...]
    action_owners: Mapping[str, str]
    channel_owners: Mapping[str, str]

    def __init__(self, *, actions=(), channels=(), plugins=()):
        compiled_actions, compiled_channels, routes = {}, {}, {}
        action_owners, channel_owners = {}, {}
        plugins = tuple(plugins)
        owners = set()

        def add_action(spec, owner=None):
            if not isinstance(spec, ActionSpec) or not _valid_name(spec.name):
                raise ValueError("Invalid action registration")
            if spec.name in compiled_actions or spec.name in RESERVED_ACTIONS:
                raise ValueError(f"Duplicate or reserved action: {spec.name}")
            if not callable(spec.prepare) or (
                spec.before_start is not None and not callable(spec.before_start)
            ):
                raise ValueError(f"Invalid action callbacks: {spec.name}")
            compiled_actions[spec.name] = spec
            if owner is not None:
                action_owners[spec.name] = owner

        def add_channel(spec, owner=None):
            if not isinstance(spec, ChannelSpec) or not _valid_name(spec.name):
                raise ValueError("Invalid channel registration")
            if spec.name in compiled_channels or spec.name in RESERVED_CHANNELS:
                raise ValueError(f"Duplicate or reserved channel: {spec.name}")
            if not callable(spec.collect) or type(spec.telemetry) is not bool:
                raise ValueError(f"Invalid channel callback: {spec.name}")
            try:
                valid_interval = (
                    not isinstance(spec.interval, bool)
                    and isinstance(spec.interval, (int, float))
                    and math.isfinite(spec.interval)
                    and spec.interval > 0
                )
            except OverflowError:
                valid_interval = False
            if not valid_interval:
                raise ValueError(f"Invalid channel interval: {spec.name}")
            if spec.route is not None:
                if not isinstance(spec.route, str) or spec.route in routes:
                    raise ValueError("Invalid or duplicate polling route")
                if owner is not None and not re.fullmatch(
                    rf"/api/plugins/{re.escape(owner)}/[a-z][a-z0-9-]*(?:/[a-z][a-z0-9-]*)*",
                    spec.route,
                    re.ASCII,
                ):
                    raise ValueError(f"Plugin polling route must use /api/plugins/{owner}/")
                if owner is None and not spec.route.startswith("/api/"):
                    raise ValueError("Host polling route must use /api/")
                routes[spec.route] = spec.name
            compiled_channels[spec.name] = spec
            if owner is not None:
                channel_owners[spec.name] = owner

        for spec in tuple(actions):
            add_action(spec)
        for spec in tuple(channels):
            add_channel(spec)
        for plugin in plugins:
            if not isinstance(plugin, Plugin) or not _valid_name(plugin.name):
                raise ValueError("Invalid plugin registration")
            if plugin.name in owners or plugin.name in {"core", "builtin"}:
                raise ValueError(f"Duplicate or reserved plugin: {plugin.name}")
            owners.add(plugin.name)
            for spec in plugin.actions:
                add_action(spec, plugin.name)
            for spec in plugin.channels:
                add_channel(spec, plugin.name)

        for name, value in (
            ("actions", MappingProxyType(compiled_actions)),
            ("channels", MappingProxyType(compiled_channels)),
            ("poll_routes", MappingProxyType(routes)),
            (
                "stream_plan",
                tuple((spec.name, spec.interval) for spec in compiled_channels.values()),
            ),
            ("plugins", plugins),
            ("action_owners", MappingProxyType(action_owners)),
            ("channel_owners", MappingProxyType(channel_owners)),
        ):
            object.__setattr__(self, name, value)

    def collect(self, name, *, telemetry=None):
        spec = self.channels.get(name)
        if spec is None:
            return {"ok": False, "error": "нет такого потока"}

        def collect():
            if telemetry is not None and spec.telemetry:
                return spec.collect(state_provider=telemetry.container_state)
            return spec.collect()

        owner = self.channel_owners.get(name)
        if owner is None:
            return collect()
        try:
            return _plain_payload(collect())
        except Exception as error:  # noqa: BLE001 — contain custom collection/encoding failures
            return {"ok": False, "error": extension_failure(owner, error)}
