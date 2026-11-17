"""Secret resolution. The IR only stores references; values come from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol


class SecretResolver(Protocol):
    def get(self, name: str) -> str | None: ...


class EnvSecretResolver:
    """Resolves ``NAME`` from the environment variable ``{prefix}NAME``."""

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix

    def get(self, name: str) -> str | None:
        value = os.environ.get(f"{self.prefix}{name}")
        return value or None


class MappingSecretResolver:
    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def get(self, name: str) -> str | None:
        return self._values.get(name)
