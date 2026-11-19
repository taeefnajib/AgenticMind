"""Isolated node worker (ADR-0021). The only code that imports third-party node packages.

Run as ``python -I -m agenticmind_plugins.worker <describe|run> <plugin_dir>`` in a fresh process. The
parent gives it a scrubbed environment (no host secrets), its own working directory and a timeout.

* ``describe``: register the plugin's nodes and print their manifests as JSON.
* ``run``: read one invocation request (JSON) on stdin, run the handler, print the result (JSON).

The plugin directory is appended to ``sys.path`` (never prepended), so a plugin cannot shadow the SDK or
the standard library. Results go to stdout as one JSON document; anything the plugin prints is captured
separately so it cannot corrupt the protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import socket
import sys
import traceback
from pathlib import Path
from typing import Any

PROTOCOL = 1
ENTRY_POINT_GROUP = "agenticmind.nodes"


def _load(plugin_dir: str) -> dict[tuple[str, str], tuple[Any, str, str]]:
    """``{(type, version): (definition, distribution, dist_version)}`` for the plugin's nodes."""
    from importlib.metadata import distributions

    sys.path.append(plugin_dir)
    found: dict[tuple[str, str], tuple[Any, str, str]] = {}
    for dist in distributions(path=[plugin_dir]):
        for ep in dist.entry_points:
            if ep.group != ENTRY_POINT_GROUP:
                continue
            register = ep.load()

            def registrar(definition: Any, _d: str = dist.metadata["Name"], _v: str = dist.version) -> None:
                found[definition.key] = (definition, _d, _v)

            register(registrar)
    return found


def describe(plugin_dir: str) -> dict[str, Any]:
    nodes = _load(plugin_dir)
    return {
        "protocol": PROTOCOL,
        "nodes": [
            {"distribution": dist, "version": ver, "manifest": d.manifest.model_dump(mode="json", by_alias=True)}
            for (d, dist, ver) in nodes.values()
        ],
    }


class _NetworkDenied(OSError):
    pass


def _deny_network() -> None:
    """Guard for nodes without the ``network`` permission: outbound connections fail loudly.

    This is a guard against accidental use, not a sandbox (native code could bypass it); the process
    boundary, the scrubbed environment and the per-invocation directory are the isolation."""

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise _NetworkDenied("network access is not permitted for this node (it lacks the 'network' permission)")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]
    socket.getaddrinfo = refuse  # type: ignore[assignment]


async def _run(req: dict[str, Any]) -> dict[str, Any]:
    from agenticmind_node_sdk import NodeContext, NodeError, TransientNodeError

    nodes = _load(req["plugin_dir"])
    key = (req["type"], req["version"])
    if key not in nodes:
        return {"error": {"code": "PLUGIN_NODE_MISSING", "message": f"{key[0]}@{key[1]} is not in this package"}}
    definition = nodes[key][0]
    perms = definition.manifest.permissions
    if not perms.network:
        _deny_network()
    events: list[dict[str, Any]] = []

    async def emit(event_type: str, data: Any) -> None:
        events.append({"type": event_type, "data": data})

    secrets: dict[str, str] = req.get("secrets") or {}
    storage = req.get("storage_dir")
    ctx = NodeContext(
        run_id=req["run_id"],
        project_id=req["project_id"],
        node_id=req["node_id"],
        invocation_id=req["invocation_id"],
        attempt=req.get("attempt", 1),
        run_input=req.get("run_input") or {},
        allowed_secrets=frozenset(secrets) if perms.credentials else frozenset(),
        _secret_lookup=secrets.get,
        _emitter=emit,
        _storage_root=Path(storage) if (storage and perms.filesystem) else None,
    )
    try:
        config = definition.parse_config(req.get("config") or {})
        result = await definition.handler(ctx, dict(req.get("inputs") or {}), config)
    except TransientNodeError as exc:
        return {"error": {"code": exc.code, "message": exc.message, "retryable": True}, "events": events}
    except NodeError as exc:
        return {"error": {"code": exc.code, "message": exc.message, "retryable": False}, "events": events}
    except _NetworkDenied as exc:
        return {"error": {"code": "PLUGIN_NETWORK_DENIED", "message": str(exc)}, "events": events}
    out: dict[str, Any] = {
        "outputs": dict(result.outputs),
        "branches": sorted(result.branches) if result.branches is not None else None,
        "run_outputs": dict(result.run_outputs),
        "usage": result.usage.model_dump(mode="json") if result.usage else None,
        "events": events,
    }
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("describe", "run"):
        print(json.dumps({"error": {"code": "PLUGIN_PROTOCOL", "message": "usage: describe|run <plugin_dir>"}}))
        return 2
    mode, plugin_dir = argv[0], argv[1]
    captured = io.StringIO()  # anything the plugin prints must not corrupt the JSON protocol
    try:
        with contextlib.redirect_stdout(captured):
            if mode == "describe":
                result = describe(plugin_dir)
            else:
                req = json.loads(sys.stdin.read())
                req["plugin_dir"] = plugin_dir
                result = asyncio.run(_run(req))
    except Exception as exc:  # noqa: BLE001 - reported to the parent, never crashes silently
        result = {
            "error": {
                "code": "PLUGIN_CRASHED",
                "message": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc()[-4000:],
            }
        }
    result["stdout"] = captured.getvalue()[-4000:]
    try:
        payload = json.dumps(result)
    except (TypeError, ValueError) as exc:
        payload = json.dumps({"error": {"code": "PLUGIN_OUTPUT_NOT_JSON", "message": f"outputs must be JSON: {exc}"}})
    sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
