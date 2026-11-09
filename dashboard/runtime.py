"""Compose built-in capabilities with explicitly supplied, trusted plugins."""

import actions
import payloads
from extensions import ExtensionRegistry


def build_registry(plugins=()):
    return ExtensionRegistry(
        actions=actions.builtin_actions(), channels=payloads.builtin_channels(), plugins=plugins
    )
