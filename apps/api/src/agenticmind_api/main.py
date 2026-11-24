"""AgenticMind control-plane API."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from typing import Any

from agenticmind_registry import NodeRegistry, default_registry
from agenticmind_runtime import EnvSecretResolver, LocalArtifactStore, SecretResolver
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .auth import AuthStore
from .db import make_engine, make_session_factory
from .deploy_manager import DeploymentManager, DeploymentStore
from .deps import Services
from .eval_manager import EvaluationManager
from .eval_store import EvalStore
from .git_sync import GitSyncService
from .memory_store import SqlMemoryStore
from .observability import ObservabilityService
from .plugin_manager import PluginManager
from .routers import assistant as assistant_routes
from .routers import auth as auth_routes
from .routers import deployments as deployment_routes
from .routers import evaluations as evaluation_routes
from .routers import files, nodes, plugins, projects, runs
from .routers import git as git_routes
from .routers import memory as memory_routes
from .routers import observability as observability_routes
from .routers import secrets as secret_routes
from .routers import settings as settings_routes
from .routers import triggers as trigger_routes
from .run_manager import RunManager
from .secrets_store import ChainedSecrets, SecretVault, load_key
from .security import ApiGuard
from .settings import Settings
from .store import Store
from .triggers.service import TriggerService

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
    vault = SecretVault(session_factory, load_key(settings.data_dir))
    secrets = ChainedSecrets(secrets, vault)  # environment first, then Settings → Secrets
    auth = AuthStore(session_factory, session_days=settings.session_days)
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
        worker_id=settings.worker_id,
        lease_seconds=settings.lease_seconds,
        max_run_attempts=settings.max_run_attempts,
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

    mcp_app: Any = None
    mcp_manager: Any = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with mcp_manager.run() if mcp_manager is not None else nullcontext():
            async with _core_lifespan(_app):
                await triggers.start()
                try:
                    yield
                finally:
                    await triggers.stop()
                    await observability.drain()
                    await git_sync.drain()

    @asynccontextmanager
    async def _core_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # runs with a job are recovered by workers (expired leases); older runs without one are interrupted
        orphaned = store.fail_unqueued_orphans()
        if orphaned:
            log.warning("marked %d interrupted run(s) as failed", orphaned)
        if settings.embedded_worker:
            await run_manager.start_worker()
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
        auth=auth,
    )
    triggers = TriggerService(app.state.services)
    app.state.services.triggers = triggers
    observability = ObservabilityService(app.state.services)
    app.state.services.observability = observability
    run_manager.finish_hooks.append(observability.on_run_finished)
    git_sync = GitSyncService(app.state.services)
    app.state.services.git = git_sync
    if settings.auth_mode not in ("required", "off"):
        raise ValueError(f"AGENTICMIND_AUTH must be 'required' or 'off', not {settings.auth_mode!r}")
    if settings.auth_mode == "off":
        log.warning(
            "sign-in is off (the default): anyone who can reach this API can use it. Keep it on localhost, or "
            "set AGENTICMIND_AUTH=required before exposing it to a network"
        )
    # inner to CORS (added below, so it wraps this): refusals still carry CORS headers
    app.add_middleware(ApiGuard, auth=auth, settings=settings)
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
    app.include_router(assistant_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(secret_routes.router)
    app.include_router(trigger_routes.router)
    app.include_router(observability_routes.router)
    app.include_router(git_routes.router)
    if settings.mcp_server:
        from .mcp_endpoint import build_mcp_app

        mcp_app, mcp_manager = build_mcp_app(app.state.services)
        # after every /api router, so it only receives /api paths nothing else matched (i.e. /api/mcp)
        app.mount("/api", mcp_app)
    if settings.web_dist and settings.web_dist.is_dir():
        app.mount("/", StaticFiles(directory=settings.web_dist, html=True), name="web")
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory agenticmind_api.main:app_factory``."""
    logging.basicConfig(level=logging.INFO)
    from .envfile import load_env_file

    load_env_file()  # before Settings() so .env values apply to settings and secrets
    return create_app()
