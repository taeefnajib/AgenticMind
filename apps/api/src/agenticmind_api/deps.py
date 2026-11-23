from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agenticmind_registry import NodeRegistry
from agenticmind_runtime import LocalArtifactStore, SecretResolver
from fastapi import Request

from .auth import AuthStore
from .deploy_manager import DeploymentManager
from .eval_manager import EvaluationManager
from .eval_store import EvalStore
from .memory_store import SqlMemoryStore
from .plugin_manager import PluginManager
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
    artifacts: LocalArtifactStore
    memory: SqlMemoryStore
    evals: EvalStore
    eval_runs: EvaluationManager
    deployments: DeploymentManager
    plugins: PluginManager
    auth: AuthStore
    triggers: Any = None  # TriggerService (ADR-0035), set by create_app
    observability: Any = None  # ObservabilityService (ADR-0037), set by create_app


def services(request: Request) -> Services:
    return request.app.state.services
