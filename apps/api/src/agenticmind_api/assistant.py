"""AI workflow designer: natural-language requests to validated IR graph changes (ADR-0022).

The model proposes the *whole* graph (nodes + edges) as JSON. The server never trusts it: the proposal is
parsed as IR, validated with the same pipeline as the editor, and fed back with its diagnostics for up to
``max_attempts`` repairs. Positions of existing nodes are kept; new nodes are laid out by graph depth.
Nothing is saved here; the client applies (and saves) an accepted proposal.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from agenticmind_ir import IRLoadError, load_project
from agenticmind_node_sdk import NodeManifest
from agenticmind_providers import ChatMessage, ChatRequest, ModelProvider, ProviderError
from agenticmind_registry import NodeRegistry
from agenticmind_runtime import SecretResolver, validate_project

#: Diagnostics about the operator's environment, not the graph; reported but never "repaired" by the model.
ENVIRONMENT_CODES = frozenset({"SECRET_NOT_CONFIGURED"})
MAX_HISTORY = 12
X_STEP, Y_STEP = 320, 170
#: Proposals may not set a model's output budget below this (a tiny budget leaves reasoning models no text).
MIN_MAX_TOKENS = 512


class AssistantError(Exception):
    def __init__(self, code: str, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# ---- catalog --------------------------------------------------------------------------------------
def _semver_key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v)[:3])


def latest_manifests(registry: NodeRegistry) -> list[NodeManifest]:
    """Newest version of each permitted node type (unapproved package nodes are not offered)."""
    best: dict[str, NodeManifest] = {}
    for entry in registry.entries():
        if not entry.permitted:
            continue
        m = entry.manifest
        if m.type not in best or _semver_key(m.version) > _semver_key(best[m.type].version):
            best[m.type] = m
    return sorted(best.values(), key=lambda m: (m.category, m.type))


def _schema_type(spec: dict[str, Any]) -> str:
    if "enum" in spec:
        return "|".join(json.dumps(v) for v in spec["enum"])
    if "const" in spec:
        return json.dumps(spec["const"])
    if spec.get("x-secret"):
        return 'secret {"$secret": NAME}'
    if "anyOf" in spec:
        parts = [_schema_type(s) for s in spec["anyOf"] if s.get("type") != "null"]
        return "|".join(p for p in parts if p) or "any"
    if "$ref" in spec:
        return "object"
    t = spec.get("type", "any")
    if t in ("number", "integer") and ("minimum" in spec or "maximum" in spec):
        return f"{t} {spec.get('minimum', '')}..{spec.get('maximum', '')}"
    if t == "array":
        return f"array<{_schema_type(spec.get('items') or {})}>"
    return str(t)


def _short(text: str, limit: int = 110) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _t(port_type: Any) -> str:
    return str(getattr(port_type, "value", port_type))


def describe_manifest(m: NodeManifest) -> str:
    lines = [f"- {m.type}@{m.version} [{m.category}] {m.display_name}: {_short(m.description, 160)}"]
    if m.inputs:
        ins = ", ".join(
            f"{p.name}:{_t(p.type)}{'' if p.required else '?'}{'(many)' if p.cardinality.value == 'multiple' else ''}"
            for p in m.inputs
        )
        lines.append(f"    in: {ins}")
    if m.outputs:
        lines.append("    out: " + ", ".join(f"{p.name}:{_t(p.type)}" for p in m.outputs))
    typed = [p.name for p in [*m.inputs, *m.outputs] if p.type_from_config]
    if typed:
        lines.append(f"    port types follow config ({', '.join(sorted(set(typed)))})")
    if m.branches_from_config:
        lines.append(f"    control branches: names from config '{m.branches_from_config}' plus {m.branches}")
    elif m.branches != ["done"]:
        lines.append(f"    control branches: {', '.join(m.branches)}")
    props: dict[str, Any] = m.config_schema.get("properties") or {}
    required = set(m.config_schema.get("required") or [])
    if props:
        parts = []
        for name, spec in props.items():
            item = f"{name}{'*' if name in required else ''}: {_schema_type(spec)}"
            if "default" in spec and spec["default"] not in (None, "", [], {}):
                item += f" = {json.dumps(spec['default'])[:60]}"
            if spec.get("description"):
                item += f" ({_short(spec['description'], 90)})"
            parts.append(item)
        lines.append("    config: " + "; ".join(parts))
    return "\n".join(lines)


def catalog_text(registry: NodeRegistry) -> str:
    return "\n".join(describe_manifest(m) for m in latest_manifests(registry))


SYSTEM_PROMPT = """You are the AgenticMind workflow designer. You turn the user's request into a change to a
visual AI workflow graph (AgenticMind IR v1.0) and explain it briefly.

# Graph rules
- A graph has nodes {{"id", "type", "version", "config", "ui"?: {{"label"}}}} and edges.
- Node ids: short snake_case, unique (pattern ^[A-Za-z_][A-Za-z0-9_-]{{0,63}}$). Edge ids: unique ("e1", "c1", ...).
- Data edge: {{"id", "kind": "data", "source": {{"node", "port": <output port>}},
  "target": {{"node", "port": <input port>}}}}.
  Types must match: same type, target Any accepts everything, target JSON accepts String/Number/Boolean/JSON.
  Use agenticmind.core.to_text to turn other values into String. Each single input takes one edge.
  Every required input (no "?") must be connected.
- Control edge: {{"id", "kind": "control", "source": {{"node", "port": <branch name>}},
  "target": {{"node", "port": "trigger"}}}}.
  Only use branches listed for the source node. A node with incoming control edges runs only when one
  of those branches fires; the others are skipped. Data edges alone already order execution.
- No cycles (use agenticmind.core.loop/loop_end for iteration). Every node must be connected.
- Inputs come from agenticmind.core.manual_input nodes (config.key = run input field name); results leave
  through agenticmind.core.output nodes (config.key = result field name).
- Use only node types and versions from the catalog; prefer the listed version. Config keys must come from
  that node's config list; "*" marks required keys. Omit keys to use their defaults.
- Language models: agenticmind.models.llm (or agents) with "provider": "{provider}", "model": "{model}",
  "api_key": {{"$secret": "{secret}"}}. Never write a literal key; secrets are always {{"$secret": "NAME"}}.
- Never set a small max_tokens to keep answers short (models also spend tokens on reasoning, and a small
  budget returns nothing): keep the default and constrain length in the system prompt instead.
- Templates (agenticmind.core.text_template) use {{{{ text }}}} and {{{{ variables.field }}}} placeholders.

# Changing an existing graph
The current graph is given. Keep its node ids, types, versions and configs unless the request needs a change,
and return the COMPLETE resulting graph (unchanged nodes included). Removing a node also removes its edges.

# Reply format
Return ONLY one JSON object, no markdown fences:
{{"reply": "<2-5 sentences: what you built or changed and how to run it>",
  "change": null | {{"name"?: "<project name, only for a new workflow in an empty or default project>",
                    "description"?: "<one sentence>", "nodes": [...], "edges": [...]}}}}
Use "change": null when the user only asks a question or the request is unclear (then ask in "reply").

# Node catalog (type@version [category] name: description / ports / config)
{catalog}
"""


# ---- result ---------------------------------------------------------------------------------------
@dataclass
class DesignResult:
    reply: str
    document: dict[str, Any] | None = None
    changes: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    valid: bool | None = None
    #: No graph errors other than environment ones (e.g. a secret not set on this machine).
    applicable: bool = False
    attempts: int = 0
    model: str = ""
    tokens: int = 0

    def to_public(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "document": self.document,
            "changes": self.changes,
            "diagnostics": self.diagnostics,
            "valid": self.valid,
            "applicable": self.applicable,
            "attempts": self.attempts,
            "model": self.model,
            "tokens": self.tokens,
        }


# ---- helpers --------------------------------------------------------------------------------------
def extract_json(text: str) -> dict[str, Any]:
    """The first JSON object in a model reply (tolerates code fences and surrounding prose)."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        value = json.loads(cleaned)
    except ValueError:
        start = cleaned.find("{")
        if start < 0:
            raise ValueError("The reply contained no JSON object") from None
        value, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    if not isinstance(value, dict):
        raise ValueError("The reply must be a JSON object")
    return value


def compact_graph(doc: dict[str, Any]) -> dict[str, Any]:
    """The current graph as the model sees it (no positions; labels kept)."""
    nodes = []
    for n in doc.get("nodes") or []:
        item = {k: n[k] for k in ("id", "type", "version", "config") if k in n}
        label = (n.get("ui") or {}).get("label")
        if label:
            item["ui"] = {"label": label}
        nodes.append(item)
    return {
        "name": (doc.get("project") or {}).get("name", ""),
        "description": (doc.get("project") or {}).get("description", ""),
        "nodes": nodes,
        "edges": doc.get("edges") or [],
    }


def _depths(node_ids: list[str], edges: list[dict[str, Any]]) -> dict[str, int]:
    preds: dict[str, set[str]] = defaultdict(set)
    ids = set(node_ids)
    for e in edges:
        s, t = (e.get("source") or {}).get("node"), (e.get("target") or {}).get("node")
        if s in ids and t in ids and s != t:
            preds[t].add(s)
    depth: dict[str, int] = {}

    def visit(n: str, stack: frozenset[str]) -> int:
        if n in depth:
            return depth[n]
        if n in stack:  # cycle: validation reports it; just stop here
            return 0
        d = max((visit(p, stack | {n}) + 1 for p in preds[n]), default=0)
        depth[n] = d
        return d

    for n in node_ids:
        visit(n, frozenset())
    return depth


def layout(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], previous: dict[str, Any]) -> None:
    """Keep positions of nodes that already existed; place the rest in columns by depth."""
    old_pos = {
        n["id"]: (n.get("ui") or {}).get("position")
        for n in previous.get("nodes") or []
        if (n.get("ui") or {}).get("position")
    }
    kept_ys = [p["y"] for nid, p in old_pos.items() if any(n.get("id") == nid for n in nodes)]
    base_y = (max(kept_ys) + Y_STEP + 40) if kept_ys else 0
    depth = _depths([n["id"] for n in nodes if "id" in n], edges)
    slots: dict[int, int] = defaultdict(int)
    for n in nodes:
        ui = dict(n.get("ui") or {})
        if n.get("id") in old_pos:
            ui["position"] = old_pos[n["id"]]
        else:
            d = depth.get(n.get("id", ""), 0)
            ui["position"] = {"x": d * X_STEP, "y": base_y + slots[d] * Y_STEP}
            slots[d] += 1
        n["ui"] = {k: v for k, v in ui.items() if v is not None}


def _edge_key(e: dict[str, Any]) -> tuple[Any, ...]:
    s, t = e.get("source") or {}, e.get("target") or {}
    return (e.get("kind"), s.get("node"), s.get("port"), t.get("node"), t.get("port"))


def diff_graphs(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    old = {n["id"]: n for n in before.get("nodes") or [] if "id" in n}
    new = {n["id"]: n for n in after.get("nodes") or [] if "id" in n}

    def sig(n: dict[str, Any]) -> tuple[Any, ...]:
        return (
            n.get("type"),
            n.get("version"),
            json.dumps(n.get("config") or {}, sort_keys=True),
            (n.get("ui") or {}).get("label"),
        )

    old_edges = {_edge_key(e) for e in before.get("edges") or []}
    new_edges = {_edge_key(e) for e in after.get("edges") or []}
    return {
        "added": [{"id": i, "type": new[i].get("type")} for i in new if i not in old],
        "removed": [{"id": i, "type": old[i].get("type")} for i in old if i not in new],
        "modified": [{"id": i, "type": new[i].get("type")} for i in new if i in old and sig(new[i]) != sig(old[i])],
        "edges_added": len(new_edges - old_edges),
        "edges_removed": len(old_edges - new_edges),
    }


def _diag_text(diags: list[dict[str, Any]]) -> str:
    lines = []
    for d in diags[:25]:
        where = d.get("node_id") or d.get("edge_id") or d.get("path") or ""
        lines.append(f"- {d.get('code')} {where}: {d.get('message')}")
    return "\n".join(lines)


# ---- the designer ---------------------------------------------------------------------------------
@dataclass
class WorkflowDesigner:
    registry: NodeRegistry
    secrets: SecretResolver
    provider: ModelProvider
    model: str
    api_key_secret: str | None = "OPENROUTER_API_KEY"
    base_url: str | None = None
    in_process_trust: Any = None
    max_attempts: int = 3
    max_tokens: int = 8000

    def _api_key(self) -> str | None:
        if self.provider.name == "mock" or not self.api_key_secret:
            return None
        key = self.secrets.get(self.api_key_secret)
        if not key and self.provider.name == "openrouter":
            raise AssistantError(
                "ASSISTANT_NOT_CONFIGURED",
                f"The AI assistant needs the {self.api_key_secret} secret. Add it to .env and restart the API.",
                status=400,
            )
        return key

    def system_prompt(self) -> str:
        return SYSTEM_PROMPT.format(
            provider=self.provider.name if self.provider.name in ("openrouter", "openai_compatible") else "openrouter",
            model=self.model,
            secret=self.api_key_secret or "OPENROUTER_API_KEY",
            catalog=catalog_text(self.registry),
        )

    def _validate(self, raw: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        kwargs: dict[str, Any] = {"secrets": self.secrets}
        if self.in_process_trust is not None:
            kwargs["in_process_trust"] = self.in_process_trust
        try:
            load_project(raw)
        except IRLoadError as exc:
            return [
                {"code": i["code"], "severity": "error", "message": i["message"], "path": i["path"]} for i in exc.issues
            ], False
        _doc, report = validate_project(raw, self.registry, **kwargs)
        public = report.to_public()
        return public["diagnostics"], public["valid"]

    async def design(
        self, message: str, document: dict[str, Any], history: list[dict[str, str]] | None = None
    ) -> DesignResult:
        api_key = self._api_key()
        messages: list[ChatMessage] = []
        for h in (history or [])[-MAX_HISTORY:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                messages.append(ChatMessage(role=h["role"], content=str(h["content"])[:4000]))
        graph = json.dumps(compact_graph(document), ensure_ascii=False)
        messages.append(ChatMessage(role="user", content=f"Current graph:\n{graph}\n\nRequest: {message}"))

        result = DesignResult(reply="", model=self.model)
        for attempt in range(1, self.max_attempts + 1):
            result.attempts = attempt
            try:
                resp = await self.provider.chat(
                    ChatRequest(
                        model=self.model,
                        system=self.system_prompt(),
                        messages=messages,
                        temperature=0.1,
                        max_tokens=self.max_tokens,
                    ),
                    api_key=api_key,
                    base_url=self.base_url,
                )
            except ProviderError as exc:
                raise AssistantError(exc.code, str(exc)) from exc
            result.tokens += (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0)
            if resp.model:
                result.model = resp.model
            messages.append(ChatMessage(role="assistant", content=resp.text))
            try:
                payload = extract_json(resp.text)
            except ValueError as exc:
                messages.append(ChatMessage(role="user", content=f"{exc}. Reply with the JSON object only."))
                continue
            result.reply = str(payload.get("reply") or "").strip()
            change = payload.get("change")
            if not change:
                result.document = None
                result.valid = None
                return result
            if not isinstance(change, dict):
                messages.append(ChatMessage(role="user", content='"change" must be null or an object.'))
                continue
            proposed = self._apply(document, change)
            result.document = proposed
            result.changes = diff_graphs(document, proposed)
            result.diagnostics, result.valid = self._validate(proposed)
            blocking = [
                d for d in result.diagnostics if d["severity"] == "error" and d["code"] not in ENVIRONMENT_CODES
            ]
            result.applicable = not blocking
            if not blocking:
                return result
            messages.append(
                ChatMessage(
                    role="user",
                    content="The graph failed validation:\n"
                    + _diag_text(blocking)
                    + "\nFix these problems and return the complete corrected JSON object. The user never saw the "
                    'failed attempt: keep "reply" about the whole workflow, not about this fix.',
                )
            )
        if not result.reply:
            result.reply = "I could not produce a valid change for that request. Try rephrasing it."
        return result

    @staticmethod
    def _apply(document: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
        nodes = [dict(n) for n in change.get("nodes") or [] if isinstance(n, dict)]
        edges = [dict(e) for e in change.get("edges") or [] if isinstance(e, dict)]
        for n in nodes:
            n.setdefault("config", {})
            mt = n["config"].get("max_tokens") if isinstance(n["config"], dict) else None
            if isinstance(mt, int) and not isinstance(mt, bool) and mt < MIN_MAX_TOKENS:
                n["config"]["max_tokens"] = MIN_MAX_TOKENS
            ui = n.get("ui")
            n["ui"] = {"label": ui["label"]} if isinstance(ui, dict) and ui.get("label") else {}
        layout(nodes, edges, document)
        project = dict(document.get("project") or {})
        if isinstance(change.get("name"), str) and change["name"].strip():
            project["name"] = change["name"].strip()[:200]
        if isinstance(change.get("description"), str):
            project["description"] = change["description"].strip()[:5000]
        return {
            "ir_version": document.get("ir_version", "1.0"),
            "project": project,
            "runtime": document.get("runtime") or {"engine": "agenticmind", "profile": "development"},
            "nodes": nodes,
            "edges": edges,
            "metadata": document.get("metadata") or {},
        }
