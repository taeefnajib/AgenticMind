"""NVIDIA Auto Ontology adapter nodes (blueprint §9, ADR-0019).

Auto Ontology is an external, deployed service: it ingests a source database, compiles a semantic layer
(business terms, their columns, SQL attributes and relationships) and answers questions with a text-to-SQL
agent grounded in that layer. These nodes use its public gateway API, pinned to one upstream commit.
They are an adapter, so nothing here re-implements Auto Ontology.
"""

from __future__ import annotations

import re
from typing import Any

from agenticmind_node_sdk import (
    ExecutionPolicy,
    InputPort,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OntologyHandle,
    OutputPort,
    Permissions,
    PortType,
    SecretRef,
    ToolHandle,
    define_node,
    secret_field,
    ui,
)
from agenticmind_node_sdk.handles import TOOL_NAME_PATTERN, handle_value
from pydantic import Field

from .auto_ontology import API_VERSION, UPSTREAM_COMMIT, AutoOntologyClient, OntologyAnswer

PACKAGE = "agenticmind-nodes-semantic"
ONTOLOGY_PERMISSIONS = Permissions(network=True, credentials=True)
_PIN = f"Auto Ontology API {API_VERSION} (commit {UPSTREAM_COMMIT[:7]})"


def client_for(ctx: NodeContext, raw: Any) -> tuple[AutoOntologyClient, OntologyHandle]:
    handle = OntologyHandle.model_validate(raw)
    token = ctx.secret(handle.api_token) if handle.api_token is not None else None
    return AutoOntologyClient(handle.base_url, token, timeout=handle.timeout_seconds), handle


def term_line(term: dict[str, Any]) -> str:
    name = term.get("name") or term.get("id")
    synonyms = [s for s in (term.get("synonyms") or []) if s]
    desc = (term.get("description") or "").strip()
    line = f"- {name}"
    if synonyms:
        line += f" (also: {', '.join(synonyms[:5])})"
    if desc:
        line += f": {desc[:300]}"
    return line


def answer_result(ans: OntologyAnswer, max_rows: int) -> dict[str, Any]:
    """Shaped like NL-to-SQL's result, so Chat shows the SQL and rows the same way."""
    rows = ans.rows[:max_rows]
    return {
        "answer": ans.answer,
        "sql": ans.sql,
        "columns": ans.columns,
        "rows": rows,
        "row_count": len(ans.rows),
        "truncated": len(ans.rows) > max_rows,
        "source": "auto_ontology",
        "steps": [s.get("label") or s.get("node") for s in ans.steps][:50],
    }


# ---- Connection ---------------------------------------------------------------------------------
class ConnectionConfig(NodeConfig):
    base_url: str = Field(
        default="http://localhost:3000",
        description="The Auto Ontology gateway (its APP_URL; port 3000 in Docker Compose). Not the internal :3001.",
    )
    api_token: SecretRef | None = secret_field("Auto Ontology API token secret, e.g. AUTO_ONTOLOGY_API_KEY")
    require_ready: bool = Field(
        default=True, description="Fail unless the semantic layer has been compiled (questions need it)."
    )
    timeout_seconds: float = Field(default=30, gt=0, le=600, json_schema_extra=ui(advanced=True))
    target_db: str = Field(
        default="",
        description="Pin questions to one source database (empty = let Auto Ontology choose).",
        json_schema_extra=ui(advanced=True),
    )


@define_node(
    type="agenticmind.semantic.auto_ontology",
    version="1.0.0",
    display_name="Auto Ontology",
    category="semantic",
    package=PACKAGE,
    description="Connects to an NVIDIA Auto Ontology deployment and checks it is reachable, authorised and ready.",
    outputs=[
        OutputPort(name="ontology", type=PortType.ONTOLOGY),
        OutputPort(name="status", type=PortType.JSON, description="Reachability, auth, compilation and pin"),
    ],
    config=ConnectionConfig,
    permissions=ONTOLOGY_PERMISSIONS,
    tags=["semantic", "ontology", "nvidia"],
)
async def auto_ontology(ctx: NodeContext, inputs, config: ConnectionConfig) -> NodeResult:
    if config.api_token is None:
        raise NodeError(
            "ONTOLOGY_NOT_CONFIGURED",
            'Set an API token secret (e.g. {"$secret": "AUTO_ONTOLOGY_API_KEY"}); mint one in Auto Ontology '
            "under the user menu → API Tokens",
        )
    handle = OntologyHandle(
        base_url=config.base_url,
        api_token=config.api_token,
        timeout_seconds=config.timeout_seconds,
        target_db=config.target_db or None,
    )
    client, _ = client_for(ctx, handle_value(handle))
    async with client:
        health = await client.health()
        status = await client.semantic_status()  # authenticated: proves the token works
    ready = bool(status.get("calculated"))
    report = {
        "base_url": handle.base_url,
        "healthy": health.get("status") == "ok",
        "authenticated": True,
        "semantic_layer_ready": ready,
        "compiling": bool(status.get("running")),
        "last_success_at": status.get("last_success_at"),
        "pinned": {"api_version": API_VERSION, "commit": UPSTREAM_COMMIT},
        "capabilities": {"search_terms": True, "term_details": True, "ask_question": ready},
    }
    await ctx.emit("ontology_status", report)
    if not report["healthy"]:
        raise NodeError("ONTOLOGY_UNAVAILABLE", f"Auto Ontology reports it is degraded: {health}")
    if config.require_ready and not ready:
        raise NodeError(
            "ONTOLOGY_NOT_READY",
            "The semantic layer has not been compiled yet"
            + (" (compilation is running)" if report["compiling"] else "")
            + ". Turn on Settings → Semantic Compilation in Auto Ontology and wait until it is ready.",
        )
    return NodeResult(outputs={"ontology": handle_value(handle), "status": report})


# ---- Term Search ------------------------------------------------------------------------------
class TermSearchConfig(NodeConfig):
    query: str = Field(default="", description="Name filter (case-insensitive substring); the input overrides it.")
    limit: int = Field(default=25, ge=1, le=100)


@define_node(
    type="agenticmind.semantic.ontology_terms",
    version="1.0.0",
    display_name="Ontology Term Search",
    category="semantic",
    package=PACKAGE,
    description="Finds business terms in the Auto Ontology semantic layer by name.",
    inputs=[
        InputPort(name="ontology", type=PortType.ONTOLOGY),
        InputPort(name="query", type=PortType.STRING, required=False),
    ],
    outputs=[
        OutputPort(name="terms", type=PortType.JSON, description="[{id, name, synonyms, description, …}]"),
        OutputPort(name="text", type=PortType.STRING),
        OutputPort(name="total", type=PortType.NUMBER),
    ],
    config=TermSearchConfig,
    permissions=ONTOLOGY_PERMISSIONS,
    tags=["semantic", "ontology"],
)
async def ontology_terms(ctx: NodeContext, inputs, config: TermSearchConfig) -> NodeResult:
    query = str(inputs.get("query") or config.query or "").strip()
    client, _ = client_for(ctx, inputs["ontology"])
    async with client:
        page = await client.list_terms(query or None, config.limit)
    terms = [t for t in page.get("terms", []) if isinstance(t, dict)]
    text = "\n".join(term_line(t) for t in terms) or f"No terms match {query!r}."
    return NodeResult(outputs={"terms": terms, "text": text, "total": int(page.get("total", len(terms)))})


# ---- Term Details -----------------------------------------------------------------------------
class TermDetailsConfig(NodeConfig):
    term: str = Field(default="", description="Term id or exact name; the input overrides it.")
    sql_attributes: bool = Field(default=True, description="Include the term's SQL attributes (measures).")


async def resolve_term(client: AutoOntologyClient, ref: str) -> dict[str, Any]:
    page = await client.list_terms(ref, 100)
    for t in page.get("terms", []):
        if isinstance(t, dict) and (t.get("id") == ref or str(t.get("name", "")).lower() == ref.lower()):
            return await client.get_term(str(t["id"]))
    try:
        return await client.get_term(ref)
    except NodeError as exc:
        if exc.code == "ONTOLOGY_NOT_FOUND":
            raise NodeError("ONTOLOGY_TERM_NOT_FOUND", f"No term with id or name {ref!r}") from exc
        raise


@define_node(
    type="agenticmind.semantic.ontology_term",
    version="1.0.0",
    display_name="Ontology Term Details",
    category="semantic",
    package=PACKAGE,
    description="Fetches one business term: its tables, related terms and SQL attributes.",
    inputs=[
        InputPort(name="ontology", type=PortType.ONTOLOGY),
        InputPort(name="term", type=PortType.STRING, required=False),
    ],
    outputs=[OutputPort(name="term", type=PortType.JSON), OutputPort(name="text", type=PortType.STRING)],
    config=TermDetailsConfig,
    permissions=ONTOLOGY_PERMISSIONS,
    tags=["semantic", "ontology"],
)
async def ontology_term(ctx: NodeContext, inputs, config: TermDetailsConfig) -> NodeResult:
    ref = str(inputs.get("term") or config.term or "").strip()
    if not ref:
        raise NodeError("ONTOLOGY_TERM_NOT_FOUND", "Give a term id or name")
    client, _ = client_for(ctx, inputs["ontology"])
    async with client:
        term = await resolve_term(client, ref)
        if config.sql_attributes:
            term["sql_attributes"] = await client.term_sql_attributes(str(term["id"]))
    return NodeResult(outputs={"term": term, "text": term_text(term)})


def term_text(term: dict[str, Any]) -> str:
    lines = [term_line(term)]
    tables = [t.get("name") for t in term.get("tables", []) if t.get("name")]
    if tables:
        lines.append(f"  tables: {', '.join(tables[:10])}")
    related = [t.get("name") for t in term.get("related_terms", []) if t.get("name")]
    if related:
        lines.append(f"  related: {', '.join(related[:10])}")
    for attr in term.get("sql_attributes", [])[:10]:
        expr = attr.get("expression") or attr.get("sql") or ""
        lines.append(f"  measure {attr.get('name')}: {expr}"[:300])
    return "\n".join(lines)


# ---- Semantic Context (for NL-to-SQL) -----------------------------------------------------------
class ContextConfig(NodeConfig):
    max_terms: int = Field(default=5, ge=1, le=20, description="Terms to include (best matches first).")


_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are by for from had has have how in is it many much of on or the to "
    "was were what when which who with".split()
)


def match_terms(question: str, terms: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    """Rank terms by how many of their name/synonym words the question mentions (deterministic)."""
    words = {w for w in _WORD.findall(question.lower()) if w not in _STOP}
    words |= {w.rstrip("s") for w in words}
    scored = []
    for i, t in enumerate(terms):
        names = [t.get("name") or "", *(t.get("synonyms") or [])]
        best = 0.0
        for n in names:
            tokens = [w for w in _WORD.findall(n.lower()) if w not in _STOP]
            if tokens:
                hit = sum(1 for w in tokens if w in words or w.rstrip("s") in words)
                best = max(best, hit / len(tokens) + (0.01 * hit))
        if best > 0:
            scored.append((-best, i, t))
    return [t for _, _, t in sorted(scored)[:k]]


@define_node(
    type="agenticmind.semantic.ontology_context",
    version="1.0.0",
    display_name="Ontology Context",
    category="semantic",
    package=PACKAGE,
    description="Looks up the business terms a question mentions, as context for NL-to-SQL or an LLM.",
    inputs=[InputPort(name="ontology", type=PortType.ONTOLOGY), InputPort(name="question", type=PortType.STRING)],
    outputs=[
        OutputPort(name="context", type=PortType.STRING, description="Definitions, tables and measures"),
        OutputPort(name="terms", type=PortType.JSON),
    ],
    config=ContextConfig,
    permissions=ONTOLOGY_PERMISSIONS,
    tags=["semantic", "ontology"],
)
async def ontology_context(ctx: NodeContext, inputs, config: ContextConfig) -> NodeResult:
    question = str(inputs.get("question") or "")
    client, _ = client_for(ctx, inputs["ontology"])
    async with client:
        page = await client.list_terms()  # every term; the semantic layer is a curated vocabulary
        picked = match_terms(question, [t for t in page.get("terms", []) if isinstance(t, dict)], config.max_terms)
        details = []
        for t in picked:
            term = await client.get_term(str(t["id"]))
            term["sql_attributes"] = await client.term_sql_attributes(str(t["id"]))
            details.append(term)
    if not details:
        return NodeResult(outputs={"context": "", "terms": []})
    text = "Business terms from the semantic layer:\n" + "\n".join(term_text(t) for t in details)
    return NodeResult(outputs={"context": text, "terms": details})


# ---- Ask ----------------------------------------------------------------------------------------
class AskConfig(NodeConfig):
    evidence: str = Field(
        default="",
        description="Optional authoritative facts sent with the question (the input overrides it).",
        json_schema_extra=ui(widget="textarea"),
    )
    max_rows: int = Field(default=100, ge=1, le=1000, description="Rows kept in the result.")
    timeout_seconds: float = Field(
        default=300, gt=0, le=1800, description="A run makes many model calls; tens of seconds is normal."
    )


@define_node(
    type="agenticmind.semantic.ontology_ask",
    version="1.0.0",
    display_name="Ask Auto Ontology",
    category="semantic",
    package=PACKAGE,
    description="Answers a question with Auto Ontology's semantic text-to-SQL agent: answer, SQL, rows and steps.",
    inputs=[
        InputPort(name="ontology", type=PortType.ONTOLOGY),
        InputPort(name="question", type=PortType.STRING),
        InputPort(name="evidence", type=PortType.STRING, required=False),
    ],
    outputs=[
        OutputPort(name="answer", type=PortType.STRING),
        OutputPort(name="sql", type=PortType.STRING),
        OutputPort(name="rows", type=PortType.JSON),
        OutputPort(name="result", type=PortType.JSON, description="{answer, sql, columns, rows, row_count, steps}"),
    ],
    config=AskConfig,
    permissions=ONTOLOGY_PERMISSIONS,
    # an upstream run is many sequential model calls (minutes); the node's own timeout bounds it. Not retried
    # automatically: a retry would repeat the whole expensive run.
    execution=ExecutionPolicy(timeout_seconds=1800, max_attempts=1, idempotent=False),
    tags=["semantic", "ontology", "sql"],
)
async def ontology_ask(ctx: NodeContext, inputs, config: AskConfig) -> NodeResult:
    question = str(inputs.get("question") or "").strip()
    if not question:
        raise NodeError("QUESTION_EMPTY", "The question is empty")
    client, handle = client_for(ctx, inputs["ontology"])

    async def on_step(step: dict[str, Any]) -> None:
        if step.get("phase") != "start":
            await ctx.emit("ontology_step", {k: str(v)[:300] for k, v in step.items()})

    async with client:
        ans = await client.ask(
            question,
            evidence=str(inputs.get("evidence") or config.evidence or "") or None,
            target_db=handle.target_db,
            timeout=config.timeout_seconds,
            on_step=on_step,
        )
    result = answer_result(ans, config.max_rows)
    return NodeResult(outputs={"answer": ans.answer, "sql": ans.sql, "rows": result["rows"], "result": result})


# ---- Agent tool ---------------------------------------------------------------------------------
class OntologyToolConfig(NodeConfig):
    name: str = Field(default="business_data", pattern=TOOL_NAME_PATTERN)
    description: str = Field(
        default="Query the company's governed business data through its semantic layer. "
        "action=search_terms looks up business terms by name; action=ask_question answers a question "
        "with SQL and rows (slow: tens of seconds, so ask one well-formed question).",
        max_length=2000,
        json_schema_extra=ui(widget="textarea"),
    )
    max_rows: int = Field(default=50, ge=1, le=500)
    timeout_seconds: float = Field(default=300, gt=0, le=1800, json_schema_extra=ui(advanced=True))


@define_node(
    type="agenticmind.semantic.tool_ontology",
    version="1.0.0",
    display_name="Auto Ontology Tool",
    category="tools",
    package=PACKAGE,
    description="Lets an Agent search business terms and ask questions through Auto Ontology.",
    inputs=[InputPort(name="ontology", type=PortType.ONTOLOGY)],
    outputs=[OutputPort(name="tool", type=PortType.TOOL)],
    config=OntologyToolConfig,
    permissions=ONTOLOGY_PERMISSIONS,
    tags=["tools", "ontology"],
)
async def ontology_tool(ctx: NodeContext, inputs, config: OntologyToolConfig) -> NodeResult:
    OntologyHandle.model_validate(inputs["ontology"])
    handle = ToolHandle(
        tool_type="auto_ontology",
        name=config.name,
        description=f"{config.description}\n({_PIN})",
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["search_terms", "ask_question"]},
                "query": {"type": "string", "description": "search_terms: a term name or part of one"},
                "question": {"type": "string", "description": "ask_question: the question in plain language"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        binding={
            "ontology": inputs["ontology"],
            "max_rows": config.max_rows,
            "timeout_seconds": config.timeout_seconds,
        },
        permissions=["network", "credentials"],
    )
    return NodeResult(outputs={"tool": handle_value(handle)})


ONTOLOGY_NODES = (auto_ontology, ontology_terms, ontology_term, ontology_context, ontology_ask, ontology_tool)
