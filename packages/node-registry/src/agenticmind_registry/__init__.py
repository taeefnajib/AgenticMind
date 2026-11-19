"""Node registry.

Node packages are discovered through the ``agenticmind.nodes`` entry-point group. Each entry
point refers to a ``register(registrar)`` function. Trust is assigned by the *registry* (based on
the distribution that provides the entry point), never self-declared by the package.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from importlib.metadata import entry_points
from typing import Any

from agenticmind_node_sdk import NodeDefinition
from agenticmind_node_sdk.trust import CORE_DISTRIBUTIONS, may_load_in_process

ENTRY_POINT_GROUP = "agenticmind.nodes"


class TrustLevel(str, Enum):
    CORE = "core"
    VERIFIED = "verified"
    COMMUNITY = "community"
    PRIVATE = "private"


@dataclass(frozen=True)
class RegistryEntry:
    definition: NodeDefinition[Any]
    trust: TrustLevel
    source: str
    #: Runs in an isolated worker process (installed plugins, ADR-0021) instead of in-process.
    isolated: bool = False
    #: For plugins: whether an operator approved the package's permissions. Unapproved nodes are listed
    #: (so graphs referencing them load) but refused by validation.
    permitted: bool = True

    @property
    def manifest(self):  # noqa: ANN201 - convenience
        return self.definition.manifest


class RegistryError(ValueError):
    pass


_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-(.+))?$")


def semver_key(version: str) -> tuple[int, int, int, int, str]:
    m = _SEMVER.match(version)
    if not m:
        return (0, 0, 0, 0, version)
    major, minor, patch, pre = m.groups()
    # releases sort after pre-releases of the same version
    return (int(major), int(minor), int(patch), 0 if pre else 1, pre or "")


Registrar = Callable[[NodeDefinition[Any]], None]


class NodeRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, dict[str, RegistryEntry]] = {}
        #: Non-core distributions found in the environment but not imported (install them as plugins).
        self.skipped: list[str] = []

    def register(
        self,
        definition: NodeDefinition[Any],
        *,
        trust: TrustLevel = TrustLevel.PRIVATE,
        source: str = "local",
        isolated: bool = False,
        permitted: bool = True,
    ) -> None:
        t, v = definition.key
        versions = self._entries.setdefault(t, {})
        if v in versions:
            raise RegistryError(f"Node {t}@{v} is already registered (from {versions[v].source})")
        versions[v] = RegistryEntry(
            definition=definition, trust=trust, source=source, isolated=isolated, permitted=permitted
        )

    def unregister_source(self, source: str) -> int:
        """Remove every node registered from ``source`` (e.g. a plugin being disabled or upgraded)."""
        removed = 0
        for t in list(self._entries):
            for v in [v for v, e in self._entries[t].items() if e.source == source]:
                del self._entries[t][v]
                removed += 1
            if not self._entries[t]:
                del self._entries[t]
        return removed

    def register_all(self, definitions: Iterable[NodeDefinition[Any]], **kwargs: Any) -> None:
        for d in definitions:
            self.register(d, **kwargs)

    def get(self, node_type: str, version: str) -> RegistryEntry | None:
        return self._entries.get(node_type, {}).get(version)

    def has_type(self, node_type: str) -> bool:
        return node_type in self._entries

    def versions(self, node_type: str) -> list[str]:
        return sorted(self._entries.get(node_type, {}), key=semver_key)

    def latest(self, node_type: str) -> RegistryEntry | None:
        vs = self.versions(node_type)
        return self._entries[node_type][vs[-1]] if vs else None

    def entries(self) -> list[RegistryEntry]:
        return [self._entries[t][v] for t in sorted(self._entries) for v in sorted(self._entries[t], key=semver_key)]

    def load_entry_points(self, group: str = ENTRY_POINT_GROUP) -> None:
        """Load node packages installed in this environment.

        Only core distributions are imported, unless the operator opted in to in-process execution of
        other trust levels: importing a package runs its code, so third-party packages are installed as
        plugins instead and run in isolated workers (ADR-0021). Skipped ones are listed in ``skipped``.
        """
        for ep in entry_points(group=group):
            dist_name = ep.dist.name if ep.dist else "unknown"
            trust = TrustLevel.CORE if dist_name in CORE_DISTRIBUTIONS else TrustLevel.COMMUNITY
            if not may_load_in_process(dist_name):
                self.skipped.append(dist_name)
                continue
            register_fn = ep.load()

            def registrar(definition: NodeDefinition[Any], _trust: TrustLevel = trust, _src: str = dist_name):
                self.register(definition, trust=_trust, source=_src)

            register_fn(registrar)


def default_registry() -> NodeRegistry:
    registry = NodeRegistry()
    registry.load_entry_points()
    return registry


__all__ = [
    "CORE_DISTRIBUTIONS",
    "ENTRY_POINT_GROUP",
    "NodeRegistry",
    "Registrar",
    "RegistryEntry",
    "RegistryError",
    "TrustLevel",
    "default_registry",
    "semver_key",
]
