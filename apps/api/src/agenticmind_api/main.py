"""AgenticMind control-plane API."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agenticmind_registry import NodeRegistry, default_registry
from agenticmind_runtime import EnvSecretResolver, LocalArtifactStore, SecretResolver
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .db import make_engine, make_session_factory
from .deploy_manager import DeploymentManager, DeploymentStore
from .deps import Services
from .eval_manager import EvaluationManager
from .eval_store import EvalStore
from .memory_store import SqlMemoryStore
from .plugin_manager import PluginManager
from .routers import deployments as deployment_routes
from .routers import evaluations as evaluation_routes
from .routers import files, nodes, plugins, projects, runs
from .routers import memory as memory_routes
from .run_manager import RunManager
from .settings import Settings
from .store import Store

log = logging.getLogger("agenticmind.api")


def create_app(
    settings: Settings | None = None,
    *,
    registry: NodeRegistry | None = None,
    secrets: SecretResolver | None = None,
) -> FastAPI:
    settings = settings or Settings()
    registry = registry or default_registry()
    secrets = secrets or EnvSecretResolver(prefix=settings.secret_env_prefix)
    session_factory = make_session_factory(make_engine(settings.database_url))
    store = Store(session_factory)
    memory = SqlMemoryStore(session_factory)
    evals = EvalStore(session_factory)
    artifacts = LocalArtifactStore(settings.data_dir / "artifacts", max_bytes=settings.max_upload_bytes)
    run_manager = RunManager(
        store,
        registry,
        secrets,
        in_process_trust=settings.in_process_trust,
        max_concurrency=settings.max_concurrency,
        artifacts=artifacts,
        data_dir=settings.data_dir,
        memory=memory,
    )

    eval_runs = EvaluationManager(
        store,
        evals,
        registry,
        secrets,
        artifacts=artifacts,
        data_dir=settings.data_dir,
        memory=memory,
        max_concurrency=settings.max_concurrency,
    )

    plugins_manager = PluginManager(session_factory, registry, data_dir=settings.data_dir)
    loaded = plugins_manager.load_all()  # from stored manifests: no plugin code runs in this process
    if loaded:
        log.info("registered %d node package(s) as isolated plugins", loaded)
    if registry.skipped:
        log.warning(
            "not loading non-core node packages from this environment (install them as plugins instead): %s",
            ", ".join(sorted(set(registry.skipped))),
        )
    deployments = DeploymentManager(
        store,
        DeploymentStore(session_factory),
        registry,
        secrets,
        artifacts=artifacts,
        data_dir=settings.data_dir,
        local_enabled=settings.local_deploy,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        orphaned = store.fail_orphaned_runs()
        if orphaned:
            log.warning("marked %d interrupted run(s) as failed", orphaned)
        interrupted = evals.fail_interrupted()
        if interrupted:
            log.warning("marked %d interrupted evaluation(s) as failed", interrupted)
        interrupted_deploys = deployments.deployments.fail_interrupted()
        if interrupted_deploys:
            log.warning("marked %d interrupted deployment(s) as failed", interrupted_deploys)
        yield
        await deployments.shutdown()
        await eval_runs.shutdown()
        await run_manager.shutdown()

    app = FastAPI(
        title="AgenticMind API",
        version="0.1.0",
        description="Control plane for the AgenticMind visual AI engineering platform.",
        lifespan=lifespan,
    )
    app.state.services = Services(
        settings=settings,
        registry=registry,
        store=store,
        secrets=secrets,
        runs=run_manager,
        artifacts=artifacts,
        memory=memory,
        evals=evals,
        eval_runs=eval_runs,
        deployments=deployments,
        plugins=plugins_manager,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition", "X-AgenticMind-Revision", "X-AgenticMind-Content-Hash"],
    )
    app.include_router(nodes.router)
    app.include_router(projects.router)
    app.include_router(runs.router)
    app.include_router(files.router)
    app.include_router(memory_routes.router)
    app.include_router(evaluation_routes.router)
    app.include_router(deployment_routes.router)
    app.include_router(plugins.router)
    if settings.web_dist and settings.web_dist.is_dir():
        app.mount("/", StaticFiles(directory=settings.web_dist, html=True), name="web")
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory agenticmind_api.main:app_factory``."""
    logging.basicConfig(level=logging.INFO)
    from .envfile import load_env_file

    load_env_file()  # before Settings() so .env values apply to settings and secrets
    return create_app()
