"""``/api/mcp``: saved projects exposed as Model Context Protocol tools over Streamable HTTP (ADR-0033).

A project is exposed when its latest revision has ``metadata.mcp.expose = true`` (the IDE's *Expose as MCP
tool* switch). Calling the tool starts an ordinary run of that revision, so it is recorded with its trace like
any other run, and returns the run's outputs.

Access: the usual ``/api`` rules apply (cross-site writes refused; a session when sign-in is required). When
``AGENTICMIND_MCP_TOKEN`` is set, MCP clients authenticate with ``Authorization: Bearer <token>`` instead, and
the token is then required for ``/api/mcp`` even with sign-in off. Host headers are checked (DNS-rebinding
protection) against ``AGENTICMIND_MCP_ALLOWED_HOSTS``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agenticmind_ir import load_project
from agenticmind_runtime import PlanError
from agenticmind_runtime.mcp_server import (
    ExposedGraph,
    RunReply,
    build_mcp_server,
    describe,
    exposure,
    graph_input_schema,
    mcp_tool_name,
)

from .deps import Services

log = logging.getLogger("agenticmind.api.mcp")
MCP_PATH = "/api/mcp"


def exposed_graphs(svc: Services) -> list[ExposedGraph]:
    graphs: list[ExposedGraph] = []
    for project in svc.store.list_projects():
        rev = svc.store.get_revision(project.id)
        if rev is None:
            continue
        try:
            doc = load_project(rev.document)
        except Exception:  # noqa: BLE001 - one broken project must not hide the others
            log.warning("project %s: latest revision does not load; not exposed over MCP", project.id)
            continue
        meta = exposure(doc)
        if meta is None:
            continue
        name = mcp_tool_name(str(meta.get("tool_name") or doc.project.name or project.id))
        description = describe(doc, meta.get("description") if isinstance(meta.get("description"), str) else None)

        async def run(inputs: dict[str, Any], project_id: str = project.id, rev: Any = rev, doc: Any = doc) -> RunReply:
            try:
                plan = svc.runs.plan(doc)
            except PlanError as exc:
                return RunReply(status="failed", error={"code": "PLAN_INVALID", "message": str(exc)})
            run_id = svc.runs.start(project_id, rev, plan, inputs)
            try:
                await svc.runs.wait(run_id, timeout=svc.settings.mcp_run_timeout)
            except TimeoutError:
                return RunReply(
                    status="running",
                    run_id=run_id,
                    error={"message": f"still running after {svc.settings.mcp_run_timeout:.0f}s (run {run_id})"},
                )
            row = svc.store.get_run(run_id)
            if row is None:  # pragma: no cover - the run was just created
                return RunReply(status="failed", run_id=run_id, error={"message": "run record missing"})
            return RunReply(status=row.status, outputs=row.outputs or {}, error=row.error, run_id=run_id)

        graphs.append(ExposedGraph(name, description, graph_input_schema(doc), run))
    return graphs


def build_mcp_app(svc: Services) -> tuple[Any, Any]:
    """(Starlette app serving ``/mcp``, session manager whose ``run()`` the host lifespan must enter)."""
    from mcp.server.transport_security import TransportSecuritySettings

    async def list_graphs() -> list[ExposedGraph]:
        return await asyncio.to_thread(exposed_graphs, svc)

    server = build_mcp_server(list_graphs)
    hosts = list(svc.settings.mcp_allowed_hosts)
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=list(svc.settings.cors_origins),
        ),
    )
    return app, server.session_manager
