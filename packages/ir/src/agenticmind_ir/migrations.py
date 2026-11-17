"""Explicit, chained IR schema migrations.

Each migration upgrades a raw document from one ``ir_version`` to the next. Migrations operate
on plain dicts (before model validation) and must preserve all user-authored configuration.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import Any

from .models import IR_VERSION

Migration = Callable[[dict[str, Any]], dict[str, Any]]

_MIGRATIONS: dict[str, tuple[str, Migration]] = {}


class UnsupportedIRVersionError(ValueError):
    def __init__(self, version: object) -> None:
        super().__init__(f"Unsupported ir_version {version!r}; this build supports up to {IR_VERSION}")
        self.version = version


def register_migration(from_version: str, to_version: str) -> Callable[[Migration], Migration]:
    def decorator(fn: Migration) -> Migration:
        if from_version in _MIGRATIONS:
            raise ValueError(f"Migration from {from_version} already registered")
        _MIGRATIONS[from_version] = (to_version, fn)
        return fn

    return decorator


def unregister_migration(from_version: str) -> None:
    """Remove a migration (used by tests)."""
    _MIGRATIONS.pop(from_version, None)


def migrate(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``raw`` upgraded to the current IR version."""
    doc = copy.deepcopy(dict(raw))
    version = doc.get("ir_version")
    seen: set[str] = set()
    while version != IR_VERSION:
        if not isinstance(version, str) or version not in _MIGRATIONS or version in seen:
            raise UnsupportedIRVersionError(version)
        seen.add(version)
        to_version, fn = _MIGRATIONS[version]
        doc = fn(doc)
        doc["ir_version"] = to_version
        version = to_version
    return doc
