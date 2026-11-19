"""AgenticMind nodes by acme (``agenticmind.nodes`` entry point)."""

from __future__ import annotations

from typing import Any

from .nodes import word_stats

ALL_NODES = (word_stats,)


def register(registrar: Any) -> None:
    for definition in ALL_NODES:
        registrar(definition)
