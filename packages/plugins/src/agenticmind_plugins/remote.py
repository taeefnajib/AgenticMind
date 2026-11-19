"""Plugin nodes as ordinary registry entries whose handler runs in an isolated worker (ADR-0021).

The host never imports plugin code: the manifest comes from the worker's ``describe`` output, config is
validated against the manifest's JSON Schema, and each invocation is a fresh worker process that receives
only the inputs, its config, and the secrets its own config references (if it has ``credentials``).
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agenticmind_ir import iter_secret_refs
from agenticmind_node_sdk import (
    ModelUsage,
    NodeConfig,
    NodeContext,
    NodeDefinition,
    NodeError,
    NodeManifest,
    NodeResult,
    SecretRef,
    TransientNodeError,
)
from jsonschema import Draft202012Validator
from pydantic import ConfigDict, ValidationError

from .package import PluginError, run_worker


class PluginConfig(NodeConfig):
    """Config of a plugin node as seen by the host: validated against the manifest's JSON Schema."""

    model_config = ConfigDict(extra="allow")


@dataclass(frozen=True)
class RemoteNodeDefinition(NodeDefinition[PluginConfig]):
    plugin_dir: Path = Path()
    work_root: Path = Path()
    storage_dir: Path = Path()

    def parse_config(self, raw: Mapping[str, Any]) -> PluginConfig:
        schema = self.manifest.config_schema or {"type": "object"}
        errors = sorted(Draft202012Validator(schema).iter_errors(dict(raw)), key=lambda e: list(e.path))
        if errors:  # same error type as core nodes, so validation reports CONFIG_INVALID per field
            raise ValidationError.from_exception_data(
                "PluginConfig",
                [
                    {
                        "type": "value_error",
                        "loc": tuple(e.path),
                        "input": e.instance,
                        "ctx": {"error": ValueError(e.message)},
                    }
                    for e in errors
                ],
            )
        return PluginConfig.model_validate(dict(raw))


def make_definition(
    manifest: dict[str, Any], *, plugin_dir: Path, work_root: Path, storage_dir: Path
) -> RemoteNodeDefinition:
    parsed = NodeManifest.model_validate(manifest)
    plugin_dir, work_root, storage_dir = plugin_dir.resolve(), work_root.resolve(), storage_dir.resolve()

    async def handler(ctx: NodeContext, inputs: Mapping[str, Any], config: PluginConfig) -> NodeResult:
        raw = config.model_dump(mode="json")
        secrets: dict[str, str] = {}
        if parsed.permissions.credentials:
            for name in iter_secret_refs(raw):
                secrets[name] = ctx.secret(SecretRef(name=name))  # ctx enforces the node's own references
        workdir = work_root / f"{ctx.invocation_id}-{uuid.uuid4().hex[:6]}"
        payload = {
            "type": parsed.type,
            "version": parsed.version,
            "inputs": dict(inputs),
            "config": raw,
            "run_input": dict(ctx.run_input),
            "secrets": secrets,
            "storage_dir": str(storage_dir) if parsed.permissions.filesystem else None,
            "run_id": ctx.run_id,
            "project_id": ctx.project_id,
            "node_id": ctx.node_id,
            "invocation_id": ctx.invocation_id,
            "attempt": ctx.attempt,
        }
        if parsed.permissions.filesystem:
            storage_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = await asyncio.to_thread(
                run_worker,
                "run",
                plugin_dir,
                workdir=workdir,
                payload=payload,
                timeout=parsed.execution.timeout_seconds,
            )
        except PluginError as exc:
            raise NodeError(exc.code, exc.message) from exc
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        for ev in result.get("events") or []:
            await ctx.emit(str(ev.get("type", "plugin_event"))[:64], ev.get("data") or {})
        if "error" in result:
            err = result["error"]
            code, message = str(err.get("code", "PLUGIN_ERROR")), str(err.get("message", ""))
            if err.get("retryable"):
                raise TransientNodeError(code, message)
            raise NodeError(code, message)
        usage = result.get("usage")
        branches = result.get("branches")
        return NodeResult(
            outputs=result.get("outputs") or {},
            branches=frozenset(branches) if branches is not None else None,
            run_outputs=result.get("run_outputs") or {},
            usage=ModelUsage.model_validate(usage) if usage else None,
        )

    return RemoteNodeDefinition(
        manifest=parsed,
        config_model=PluginConfig,
        handler=handler,
        plugin_dir=plugin_dir,
        work_root=work_root,
        storage_dir=storage_dir,
    )
