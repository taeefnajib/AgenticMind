"""AgenticMind plugins (ADR-0021): third-party node packages, installed into their own directory and run in
isolated worker processes with approved permissions. The platform process never imports plugin code."""

from __future__ import annotations

from .package import (
    ISOLATED_PERMISSIONS,
    PluginError,
    WheelInfo,
    describe,
    inspect_wheel,
    install,
    requested_permissions,
    unsupported_permissions,
)
from .remote import PluginConfig, RemoteNodeDefinition, make_definition

__all__ = [
    "ISOLATED_PERMISSIONS",
    "PluginConfig",
    "PluginError",
    "RemoteNodeDefinition",
    "WheelInfo",
    "describe",
    "inspect_wheel",
    "install",
    "make_definition",
    "requested_permissions",
    "unsupported_permissions",
]
