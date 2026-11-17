"""AgenticMind control-plane API."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agenticmind_registry import NodeRegistry, default_registry
from agenticmind_runtime import EnvSecretResolver, SecretResolver
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .db import make_engine, make_session_factory
from .deps import Services
from .routers import nodes, projects, runs
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
    store = Store(make_session_factory(make_engine(settings.database_url)))
    run_manager = RunManager(
        store,
        registry,
        secrets,
        in_process_trust=settings.in_process_trust,
        max_concurrency=settings.max_concurrency,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        orphaned = store.fail_orphaned_runs()
        if orphaned:
            log.warning("marked %d interrupted run(s) as failed", orphaned)
        yield
        await run_manager.shutdown()

    app = FastAPI(
        title="AgenticMind API",
        version="0.1.0",
        description="Control plane for the AgenticMind visual AI engineering platform.",
        lifespan=lifespan,
    )
    app.state.services = Services(
        settings=settings, registry=registry, store=store, secrets=secrets, runs=run_manager
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
    if settings.web_dist and settings.web_dist.is_dir():
        app.mount("/", StaticFiles(directory=settings.web_dist, html=True), name="web")
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory agenticmind_api.main:app_factory``."""
    logging.basicConfig(level=logging.INFO)
    return create_app()
