"""Memory nodes (ADR-0012).

Short-term: **Conversation History** loads the recent turns of a conversation; **Save Conversation Turn**
appends the user's message and the reply. Long-term: **Remember** stores one fact and **Recall** finds
relevant facts; the **Memory Tool** lets an Agent do either. Every write requires an explicit
consent value wired into the graph: nothing is stored silently.
"""

from __future__ import annotations

from typing import Any, Literal

from agenticmind_node_sdk import (
    MAX_MEMORY_CHARS,
    MAX_MESSAGE_CHARS,
    ConversationTurn,
    ExecutionPolicy,
    InputPort,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    Permissions,
    PortType,
    ToolHandle,
    define_node,
    ui,
    valid_key,
)
from agenticmind_node_sdk.handles import handle_value
from agenticmind_node_sdk.memory import KEY_PATTERN
from pydantic import Field

from .tools import PACKAGE, ToolError, _description_field, _name_field, implementation

MEMORY = Permissions(memory=True)


def _key(value: Any, what: str) -> str:
    if not valid_key(value):
        raise NodeError("MEMORY_INVALID_KEY", f"Invalid {what} {value!r}: use 1-128 letters, digits or _ . : @ -")
    return value


def _namespace_field() -> Any:
    return Field(
        default="default",
        pattern=KEY_PATTERN,
        description="Memory group, e.g. one per end user. Recall only sees memories in the same namespace.",
    )


def _retention_field(default: int) -> Any:
    return Field(default=default, ge=0, le=3650, description="Days to keep (0 = until deleted).")


# ---- short-term ----------------------------------------------------------------------------------
class HistoryConfig(NodeConfig):
    max_turns: int = Field(default=10, ge=0, le=100, description="Most recent messages to load.")
    max_chars: int = Field(
        default=8_000, ge=200, le=200_000, description="Drop the oldest messages beyond this many characters."
    )


@define_node(
    type="agenticmind.agents.conversation_history",
    version="1.0.0",
    display_name="Conversation History",
    category="memory",
    package=PACKAGE,
    description="Loads the recent messages of a conversation (short-term memory) for an Agent's history input.",
    inputs=[InputPort(name="conversation_id", type=PortType.STRING, description="Which conversation")],
    outputs=[
        OutputPort(name="messages", type=PortType.JSON, description="[{role, content}] oldest first"),
        OutputPort(name="transcript", type=PortType.STRING, description="The same as plain text"),
        OutputPort(name="count", type=PortType.NUMBER),
    ],
    config=HistoryConfig,
    permissions=MEMORY,
    tags=["memory"],
)
async def conversation_history(ctx: NodeContext, inputs, config: HistoryConfig) -> NodeResult:
    cid = _key(inputs["conversation_id"], "conversation id")
    turns = ctx.memory.recent_turns(cid, config.max_turns)
    kept: list[ConversationTurn] = []
    total = 0
    for turn in reversed(turns):  # keep the newest messages within the character budget
        total += len(turn.content)
        if total > config.max_chars and kept:
            break
        kept.append(turn)
    kept.reverse()
    messages = [{"role": t.role, "content": t.content} for t in kept]
    transcript = "\n".join(f"{t.role}: {t.content}" for t in kept)
    return NodeResult(outputs={"messages": messages, "transcript": transcript, "count": len(messages)})


class SaveTurnConfig(NodeConfig):
    retention_days: int = _retention_field(30)


@define_node(
    type="agenticmind.agents.save_turn",
    version="1.0.0",
    display_name="Save Conversation Turn",
    category="memory",
    package=PACKAGE,
    description="Appends the user's message and the reply to the conversation, so the next turn can see them.",
    inputs=[
        InputPort(name="conversation_id", type=PortType.STRING),
        InputPort(name="user", type=PortType.STRING, description="The user's message"),
        InputPort(name="assistant", type=PortType.STRING, description="The reply"),
    ],
    outputs=[OutputPort(name="saved", type=PortType.NUMBER, description="Messages stored")],
    config=SaveTurnConfig,
    permissions=MEMORY,
    execution=ExecutionPolicy(max_attempts=1, idempotent=False),
    tags=["memory"],
)
async def save_turn(ctx: NodeContext, inputs, config: SaveTurnConfig) -> NodeResult:
    cid = _key(inputs["conversation_id"], "conversation id")
    turns = [
        ConversationTurn(role="user", content=inputs["user"][:MAX_MESSAGE_CHARS]),
        ConversationTurn(role="assistant", content=inputs["assistant"][:MAX_MESSAGE_CHARS]),
    ]
    ctx.memory.append_turns(cid, turns, run_id=ctx.run_id, retention_days=config.retention_days)
    return NodeResult(outputs={"saved": len(turns)})


# ---- long-term -----------------------------------------------------------------------------------
class RememberConfig(NodeConfig):
    namespace: str = _namespace_field()
    retention_days: int = _retention_field(365)


@define_node(
    type="agenticmind.agents.remember",
    version="1.0.0",
    display_name="Remember",
    category="memory",
    package=PACKAGE,
    description="Stores one fact in long-term memory, only when 'consent' is true.",
    inputs=[
        InputPort(name="content", type=PortType.STRING, description="The fact to keep"),
        InputPort(name="consent", type=PortType.BOOLEAN, description="Must be true, e.g. from a user setting"),
        InputPort(name="namespace", type=PortType.STRING, required=False, description="Overrides the config"),
    ],
    outputs=[
        OutputPort(name="stored", type=PortType.BOOLEAN),
        OutputPort(name="memory_id", type=PortType.STRING, description="Empty when nothing was stored"),
    ],
    config=RememberConfig,
    permissions=MEMORY,
    execution=ExecutionPolicy(max_attempts=1, idempotent=False),
    tags=["memory"],
)
async def remember(ctx: NodeContext, inputs, config: RememberConfig) -> NodeResult:
    namespace = _key(inputs.get("namespace") or config.namespace, "namespace")
    content = inputs["content"].strip()
    if inputs["consent"] is not True:
        await ctx.emit("memory_not_stored", {"reason": "no_consent"})
        return NodeResult(outputs={"stored": False, "memory_id": ""})
    if not content:
        raise NodeError("MEMORY_EMPTY", "Nothing to remember: content is empty")
    if len(content) > MAX_MEMORY_CHARS:
        raise NodeError("MEMORY_TOO_LONG", f"A memory may hold at most {MAX_MEMORY_CHARS} characters")
    record = ctx.memory.remember(
        namespace,
        content,
        metadata=None,
        source={"run_id": ctx.run_id, "node_id": ctx.node_id},
        retention_days=config.retention_days,
    )
    await ctx.emit("memory_stored", {"memory_id": record.id, "namespace": namespace})
    return NodeResult(outputs={"stored": True, "memory_id": record.id})


class RecallConfig(NodeConfig):
    namespace: str = _namespace_field()
    top_k: int = Field(default=5, ge=1, le=50)


@define_node(
    type="agenticmind.agents.recall",
    version="1.0.0",
    display_name="Recall",
    category="memory",
    package=PACKAGE,
    description="Finds long-term memories for a query: keyword matches first, then the most recent ones.",
    inputs=[
        InputPort(name="query", type=PortType.STRING),
        InputPort(name="namespace", type=PortType.STRING, required=False, description="Overrides the config"),
    ],
    outputs=[
        OutputPort(name="memories", type=PortType.JSON, description="[{id, content, score, match, created_at}]"),
        OutputPort(name="text", type=PortType.STRING, description="One memory per line, for a prompt"),
    ],
    config=RecallConfig,
    permissions=MEMORY,
    tags=["memory"],
)
async def recall(ctx: NodeContext, inputs, config: RecallConfig) -> NodeResult:
    namespace = _key(inputs.get("namespace") or config.namespace, "namespace")
    hits = ctx.memory.search(namespace, inputs["query"], config.top_k)
    memories = [_hit_json(h) for h in hits]
    return NodeResult(outputs={"memories": memories, "text": "\n".join(f"- {m['content']}" for m in memories)})


def _hit_json(hit: Any) -> dict[str, Any]:
    return {
        "id": hit.memory.id,
        "content": hit.memory.content,
        "score": hit.score,
        "match": hit.match,
        "created_at": hit.memory.created_at,
    }


# ---- agent tool ----------------------------------------------------------------------------------
class MemoryToolConfig(NodeConfig):
    mode: Literal["recall", "save"] = Field(
        default="recall", description="recall: the agent may search memories. save: it may store new ones."
    )
    name: str = _name_field("recall_memory")
    description: str = _description_field(
        "Search long-term memory for facts the user shared in earlier conversations (preferences, names, "
        "details). Use it before saying you don't know something about the user."
    )
    namespace: str = _namespace_field()
    top_k: int = Field(default=5, ge=1, le=20, json_schema_extra=ui(advanced=True))
    retention_days: int = _retention_field(365)


SAVE_DESCRIPTION = (
    "Store one short fact the user explicitly wants remembered for future conversations (e.g. a preference). "
    "Write it as a standalone sentence. Never store secrets, passwords or payment details."
)


@define_node(
    type="agenticmind.agents.tool_memory",
    version="1.0.0",
    display_name="Memory Tool",
    category="tools",
    package=PACKAGE,
    description="Lets an Agent recall long-term memories, or save new ones when 'consent' is true.",
    inputs=[
        InputPort(
            name="consent",
            type=PortType.BOOLEAN,
            required=False,
            description="save mode: saving works only when this is true",
        ),
        InputPort(name="namespace", type=PortType.STRING, required=False, description="Overrides the config"),
    ],
    outputs=[OutputPort(name="tool", type=PortType.TOOL)],
    config=MemoryToolConfig,
    tags=["tools", "memory"],
)
async def memory_tool(ctx: NodeContext, inputs, config: MemoryToolConfig) -> NodeResult:
    namespace = _key(inputs.get("namespace") or config.namespace, "namespace")
    if config.mode == "save":
        name = "save_memory" if config.name == "recall_memory" else config.name
        description = SAVE_DESCRIPTION if config.description == MemoryToolConfig().description else config.description
        parameters = {
            "type": "object",
            "properties": {"content": {"type": "string", "description": "The fact, as one sentence"}},
            "required": ["content"],
        }
    else:
        name, description = config.name, config.description
        parameters = {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to look for"}},
            "required": ["query"],
        }
    handle = ToolHandle(
        tool_type=f"memory_{config.mode}",
        name=name,
        description=description,
        parameters=parameters,
        binding={
            "namespace": namespace,
            "top_k": config.top_k,
            "retention_days": config.retention_days,
            "consent": inputs.get("consent") is True,
        },
        permissions=["memory"],
    )
    return NodeResult(outputs={"tool": handle_value(handle)})


@implementation("memory_recall")
async def _memory_recall(ctx: NodeContext, tool: ToolHandle, args: dict[str, Any]) -> Any:
    hits = ctx.memory.search(tool.binding["namespace"], args["query"], int(tool.binding.get("top_k", 5)))
    if not hits:
        return {"memories": [], "note": "No memories saved yet."}
    return {
        "memories": [{"content": h.memory.content, "saved": h.memory.created_at, "match": h.match} for h in hits],
        "note": "'keyword' entries share words with the query; 'recent' entries are the latest other memories.",
    }


@implementation("memory_save")
async def _memory_save(ctx: NodeContext, tool: ToolHandle, args: dict[str, Any]) -> Any:
    if tool.binding.get("consent") is not True:
        await ctx.emit("memory_not_stored", {"reason": "no_consent"})
        raise ToolError("Saving memories is turned off: the user has not given consent. Do not claim it was saved.")
    content = str(args["content"]).strip()
    if not content or len(content) > MAX_MEMORY_CHARS:
        raise ToolError(f"A memory must be 1-{MAX_MEMORY_CHARS} characters")
    record = ctx.memory.remember(
        tool.binding["namespace"],
        content,
        metadata={"via": "agent_tool"},
        source={"run_id": ctx.run_id, "node_id": ctx.node_id, "tool": tool.name},
        retention_days=int(tool.binding.get("retention_days", 365)),
    )
    await ctx.emit("memory_stored", {"memory_id": record.id, "namespace": record.namespace})
    return {"saved": True, "memory_id": record.id}
