"""Memory stores: give each run project-scoped memory access (ADR-0012).

The API persists memory in its database (``SqlMemoryStore``); the CLI and tests use the in-process
``InMemoryMemoryStore``. Both hand nodes a ``MemoryAccess`` limited to one project.
"""

from __future__ import annotations

from typing import Protocol

from agenticmind_node_sdk import InMemoryMemory, MemoryAccess


class MemoryStore(Protocol):
    def scoped(self, project_id: str) -> MemoryAccess: ...


class InMemoryMemoryStore:
    """Non-persistent store, one ``InMemoryMemory`` per project."""

    def __init__(self) -> None:
        self.projects: dict[str, InMemoryMemory] = {}

    def scoped(self, project_id: str) -> InMemoryMemory:
        return self.projects.setdefault(project_id, InMemoryMemory())
