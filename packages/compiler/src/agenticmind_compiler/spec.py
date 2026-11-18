"""What the exported API looks like: endpoints, request/response schemas and runtime requirements.

Endpoints come from the graph's own contracts (ADR-0016): every Output node becomes ``POST /<key>`` (plus
``POST /run`` for all outputs when there are several). The request fields are the Manual Inputs that
endpoint actually needs (its run-target ancestors), and the response fields are the outputs, typed from
the port feeding each Output node. ``metadata.api.endpoints`` in the IR can override the defaults.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agenticmind_ir import ProjectDocument, iter_secret_refs
from agenticmind_registry import NodeRegistry
from agenticmind_runtime import PlanError, compile_plan

MANUAL_INPUT = "agenticmind.core.manual_input"
OUTPUT = "agenticmind.core.output"
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_PATH = re.compile(r"^/[a-z0-9][a-z0-9/_-]{0,80}$")
RESERVED_PATHS = {"/healthz", "/docs", "/openapi.json", "/redoc"}

#: JSON Schema for values on each port type (shared by request models, response models and OpenAPI).
PORT_SCHEMAS: dict[str, dict[str, Any]] = {
    "String": {"type": "string"},
    "Number": {"type": "number"},
    "Boolean": {"type": "boolean"},
    "JSON": {},
    "Any": {},
    "Document": {"type": "object", "description": "Document {id, content, source, metadata}"},
    "DocumentSet": {"type": "array", "items": {"type": "object"}, "description": "List of documents"},
}


class CompileError(Exception):
    def __init__(self, message: str, diagnostics: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or []


@dataclass
class RequestField:
    key: str
    value_type: str
    required: bool
    default: Any = None


@dataclass
class ResponseField:
    key: str
    port_type: str
    node_id: str


@dataclass
class Endpoint:
    name: str
    path: str
    summary: str
    targets: list[str]
    auth: str = "api_key"
    rate_limit_per_minute: int = 60
    request: list[RequestField] = field(default_factory=list)
    response: list[ResponseField] = field(default_factory=list)
    node_ids: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    offline: bool = False
    suspends: bool = False


_ARTIFACT_ID = re.compile(r"^art_[a-f0-9]{16}$")


def artifact_ids(value: Any) -> set[str]:
    """Uploaded-file IDs referenced anywhere in a node config."""
    if isinstance(value, str):
        return {value} if _ARTIFACT_ID.match(value) else set()
    if isinstance(value, dict):
        return set().union(set(), *(artifact_ids(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(set(), *(artifact_ids(v) for v in value))
    return set()


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (s if s and s[0].isalpha() else f"x_{s}")[:40] or "endpoint"


def _outputs(doc: ProjectDocument) -> dict[str, Any]:
    return {n.id: n for n in doc.nodes if n.type == OUTPUT}


def default_endpoints(doc: ProjectDocument) -> list[dict[str, Any]]:
    """One endpoint per output key. Output nodes sharing a key (alternative branches) share an endpoint."""
    by_key: dict[str, list[str]] = {}
    for n in sorted(_outputs(doc).values(), key=lambda n: n.id):
        by_key.setdefault(str(n.config.get("key", n.id)), []).append(n.id)
    specs = []
    used: set[str] = set()
    for key, ids in sorted(by_key.items()):
        name = slug(key)
        if name == "run" or name in used:
            name = f"{name}_output"
        used.add(name)
        specs.append(
            {"name": name, "path": "/" + name.replace("_", "-"), "outputs": ids, "summary": f"Returns '{key}'"}
        )
    if len(by_key) > 1:
        all_ids = [i for ids in by_key.values() for i in ids]
        specs.append({"name": "run", "path": "/run", "outputs": all_ids, "summary": "Runs every output"})
    return specs


def _port_type(doc: ProjectDocument, registry: NodeRegistry, output_id: str) -> str:
    from agenticmind_node_sdk import resolve_port_type

    edge = next((e for e in doc.edges if e.kind.value == "data" and e.target.node == output_id), None)
    if edge is None:
        return "Any"
    src = next(n for n in doc.nodes if n.id == edge.source.node)
    entry = registry.get(src.type, src.version)
    port = entry.definition.manifest.output(edge.source.port) if entry else None
    return resolve_port_type(port, src.config).value if port else "Any"


def build_endpoints(doc: ProjectDocument, registry: NodeRegistry) -> list[Endpoint]:
    """Resolve and validate the endpoint list (raises ``CompileError`` with diagnostics)."""
    outs = _outputs(doc)
    if not outs:
        raise CompileError("The graph has no Output node, so there is nothing an API could return")
    by_key = {str(n.config.get("key", n.id)): n.id for n in outs.values()}
    api = (doc.metadata or {}).get("api") if isinstance(doc.metadata, dict) else None
    raw = api.get("endpoints") if isinstance(api, dict) and api.get("endpoints") else default_endpoints(doc)
    problems: list[dict[str, Any]] = []
    endpoints: list[Endpoint] = []
    seen_names: set[str] = set()
    seen_paths: set[str] = set()
    for i, spec in enumerate(raw if isinstance(raw, list) else []):
        where = f"metadata.api.endpoints[{i}]"
        if not isinstance(spec, dict):
            problems.append({"code": "API_ENDPOINT_INVALID", "path": where, "message": "must be an object"})
            continue
        name, path = str(spec.get("name", "")), str(spec.get("path", ""))
        if not _NAME.match(name) or name in seen_names:
            problems.append(
                {"code": "API_ENDPOINT_INVALID", "path": where, "message": f"invalid or duplicate name '{name}'"}
            )
            continue
        if not _PATH.match(path) or path in seen_paths or path in RESERVED_PATHS:
            problems.append(
                {
                    "code": "API_ENDPOINT_INVALID",
                    "path": where,
                    "message": f"invalid, duplicate or reserved path '{path}'",
                }
            )
            continue
        targets = []
        for ref in spec.get("outputs") or []:
            node_id = ref if ref in outs else by_key.get(str(ref))
            if node_id is None:
                problems.append(
                    {"code": "API_UNKNOWN_OUTPUT", "path": where, "message": f"no Output node or key '{ref}'"}
                )
            elif node_id not in targets:
                targets.append(node_id)
        if not targets:
            problems.append({"code": "API_ENDPOINT_INVALID", "path": where, "message": "needs at least one output"})
            continue
        auth = spec.get("auth", "api_key")
        if auth not in ("api_key", "none"):
            problems.append({"code": "API_ENDPOINT_INVALID", "path": where, "message": "auth must be api_key or none"})
            continue
        rate = spec.get("rate_limit_per_minute", 60)
        if not isinstance(rate, int) or not 0 <= rate <= 100_000:
            problems.append(
                {"code": "API_ENDPOINT_INVALID", "path": where, "message": "rate_limit_per_minute must be 0..100000"}
            )
            continue
        seen_names.add(name)
        seen_paths.add(path)
        endpoints.append(
            Endpoint(
                name=name,
                path=path,
                summary=str(spec.get("summary") or name),
                targets=targets,
                auth=auth,
                rate_limit_per_minute=rate,
            )
        )
    if problems:
        raise CompileError("The API definition is invalid", problems)

    nodes = {n.id: n for n in doc.nodes}
    for ep in endpoints:
        try:
            plan = compile_plan(doc, registry, targets=ep.targets)
        except PlanError as exc:
            raise CompileError(
                f"Endpoint '{ep.name}': the graph is not executable", exc.report.to_public()["diagnostics"]
            ) from exc
        ep.node_ids = sorted(plan.nodes)
        ep.request = [
            RequestField(
                key=str(nodes[i].config.get("key", "input")),
                value_type=str(nodes[i].config.get("value_type", "String")),
                required=bool(nodes[i].config.get("required", True)) and nodes[i].config.get("default") is None,
                default=nodes[i].config.get("default"),
            )
            for i in ep.node_ids
            if nodes[i].type == MANUAL_INPUT
        ]
        keys = [f.key for f in ep.request]
        if len(keys) != len(set(keys)):
            raise CompileError(f"Endpoint '{ep.name}': two Manual Inputs read the same key")
        fields: dict[str, ResponseField] = {}
        for t in ep.targets:
            key, ptype = str(nodes[t].config.get("key", t)), _port_type(doc, registry, t)
            if key in fields and fields[key].port_type != ptype:  # alternative branches with different types
                fields[key].port_type = "Any"
            fields.setdefault(key, ResponseField(key=key, port_type=ptype, node_id=t))
        ep.response = list(fields.values())
        ep.secrets = sorted({ref for i in ep.node_ids for ref in iter_secret_refs(nodes[i].config)})
        manifests = [plan.nodes[i].entry.definition.manifest for i in ep.node_ids]
        ep.suspends = any(m.can_suspend for m in manifests)
        no_files = any(
            plan.nodes[i].entry.definition.manifest.permissions.artifacts and not artifact_ids(nodes[i].config)
            for i in ep.node_ids
        )
        # runnable without keys, network, model downloads, services or missing files: safe for a smoke test
        ep.offline = (
            not ep.secrets
            and not ep.suspends
            and not no_files
            and not any(m.permissions.network or m.permissions.filesystem or m.permissions.database for m in manifests)
        )
    return endpoints


def request_schema(ep: Endpoint) -> dict[str, Any]:
    props = {}
    for f in ep.request:
        schema = dict(PORT_SCHEMAS.get(f.value_type, {}))
        if f.default is not None:
            schema["default"] = f.default
        props[f.key] = schema
    return {
        "title": f"{camel(ep.name)}Request",
        "type": "object",
        "properties": props,
        "required": [f.key for f in ep.request if f.required],
        "additionalProperties": False,
    }


def response_schema(ep: Endpoint) -> dict[str, Any]:
    return {
        "title": f"{camel(ep.name)}Response",
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "duration_ms": {"type": "number"},
            "outputs": {
                "type": "object",
                "properties": {r.key: dict(PORT_SCHEMAS.get(r.port_type, {})) for r in ep.response},
            },
        },
        "required": ["run_id", "duration_ms", "outputs"],
    }


def camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_")) or "Endpoint"


def example_request(ep: Endpoint) -> dict[str, Any]:
    samples = {"String": "example", "Number": 1, "Boolean": True, "JSON": {}}
    return {f.key: f.default if f.default is not None else samples.get(f.value_type, "example") for f in ep.request}
