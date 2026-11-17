from __future__ import annotations

from dataclasses import dataclass

from agenticmind_registry import NodeRegistry
from agenticmind_runtime import SecretResolver
from fastapi import Request

from .run_manager import RunManager
from .settings import Settings
from .store import Store


@dataclass
class Services:
    settings: Settings
    registry: NodeRegistry
    store: Store
    secrets: SecretResolver
    runs: RunManager


def services(request: Request) -> Services:
    return request.app.state.services
