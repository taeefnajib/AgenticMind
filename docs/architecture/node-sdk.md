# Node SDK: authoring and testing nodes

A node is a **manifest** (machine-readable contract) bound to an async **handler**. Nodes are testable
without the UI, API or scheduler.

## Minimal node

```python
from typing import Literal
from pydantic import Field
from agenticmind_node_sdk import (
    InputPort, NodeConfig, NodeContext, NodeResult, OutputPort, PortType, define_node, ui,
)

class ReverseConfig(NodeConfig):
    mode: Literal["chars", "words"] = "chars"
    keep_case: bool = Field(default=True, json_schema_extra=ui(advanced=True))

@define_node(
    type="acme.text.reverse",          # stable ID: <publisher>.<family>.<name>
    version="1.0.0",                   # semver of the contract
    display_name="Reverse",
    category="transform",
    package="acme-nodes",
    description="Reverses text.",
    inputs=[InputPort(name="text", type=PortType.STRING)],
    outputs=[OutputPort(name="text", type=PortType.STRING)],
    config=ReverseConfig,
)
async def reverse(ctx: NodeContext, inputs, config: ReverseConfig) -> NodeResult:
    text = inputs["text"]
    out = " ".join(reversed(text.split())) if config.mode == "words" else text[::-1]
    return NodeResult(outputs={"text": out})
```

## Contract fields

| Field | Notes |
|---|---|
| Identity | `type`, `version`, `display_name`, `category`, `package` |
| Configuration | A `NodeConfig` subclass (unknown keys rejected). Its JSON Schema is published in the manifest and drives the inspector form. UI hints via `ui(widget="textarea", advanced=True, placeholder=...)`; hints never change semantics. |
| Inputs | `InputPort(name, type, required=True, cardinality=SINGLE\|MULTIPLE, type_from_config=None)` |
| Outputs | `OutputPort(name, type, type_from_config=None)`. All declared outputs must be returned. |
| Branches | Control outputs; default `["done"]`. Branching nodes return `NodeResult(branches=frozenset({...}))`. |
| Execution | `ExecutionPolicy(timeout_seconds, max_attempts, idempotent)` |
| Permissions | `Permissions(network, filesystem, database, subprocess, credentials)`, declared up front |
| Compatibility | `Compatibility(ir_versions, adapters)` |
| Modes | Which experiences (`no_code`, `low_code`, `developer`) can safely represent the node |

`type_from_config` lets a port's type follow a config field (Manual Input's output type is its
`value_type`). Validator, runtime and UI all resolve it the same way.

## Types

`Any, String, Number, Boolean, JSON, Document, DocumentSet, EmbeddingVector, VectorStoreHandle,
ChatMessage, Tool, AgentState, EvaluationDataset, HTTPRequest, HTTPResponse`.

Compatibility is strict: equal types, any type into `Any`, and `String`/`Number`/`Boolean` into `JSON`
(subtype acceptance, no value change). Everything else needs an explicit conversion node (`To Text`,
`Parse JSON`). The runtime checks produced values against declared types.

## Documents

`Document` (`id`, `content`, `source` {uri, filename, media_type, artifact_id, page, section}, `metadata`,
`parent_id`, `offsets` {start, end}, `ingestion_version`) and `RetrievedDocument` (+ `retrieval_method`,
`score`, `rerank_score`) live in `agenticmind_node_sdk.documents`. On ports they travel as plain JSON;
use `parse_documents(...)` / `to_values(...)` to convert, and the runtime validates `Document` and
`DocumentSet` ports against these schemas. IDs are deterministic (`<artifact>:p<page>#c<chunk>`), so
citations and evaluation datasets stay stable across runs. `offsets` are exact character spans in the
parent's content; `ingestion_version` records the node chain that produced a document.

## Handles

Shared resources travel as *handles* (`EmbeddingModelHandle`, `VectorStoreHandle` in
`agenticmind_node_sdk.handles`) on `EmbeddingModel` / `VectorStoreHandle` ports. A handle is plain
config plus secret references. A consumer with `credentials` may call `ctx.secret(handle.api_key)`; the
runtime delegates the handle's references along the edge. Never put secret values into outputs.

## Writing a tool for agents

A tool is a node that outputs a `ToolHandle` on a `Tool` port, plus an implementation the Agent calls:

```python
from agenticmind_node_sdk import NodeResult, OutputPort, PortType, ToolHandle, define_node
from agenticmind_node_sdk.handles import handle_value
from agenticmind_nodes_agents.tools import ToolError, implementation

@define_node(type="acme.tools.weather", version="1.0.0", display_name="Weather Tool", category="tools",
             package="acme-nodes", outputs=[OutputPort(name="tool", type=PortType.TOOL)])
async def weather_tool(ctx, inputs, config):
    return NodeResult(outputs={"tool": handle_value(ToolHandle(
        tool_type="acme_weather", name="get_weather",
        description="Current weather for a city.",
        parameters={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        permissions=["network"],
    ))})

@implementation("acme_weather")
async def run_weather(ctx, tool, args):
    if not args["city"].strip():
        raise ToolError("city must not be empty")   # shown to the model, which can retry
    return {"city": args["city"], "temp_c": 3}
```

Arguments are validated against `parameters` (JSON Schema 2020-12) before the implementation runs.
Return JSON-serialisable data; raise `ToolError` for problems the model should see. `permissions` must be a
subset of what the Agent node declares.

## Local storage

With `Permissions(filesystem=True)`, `ctx.storage_path("vectors", "qdrant")` returns a persistent
directory under the runtime's data dir (`STORAGE_UNAVAILABLE` otherwise).

## Project files

Declare `Permissions(artifacts=True)` to read uploaded project files:

```python
info = ctx.artifact_info(file_id)      # filename, media_type, size, sha256
data = ctx.read_artifact(file_id)      # bytes; ARTIFACT_NOT_FOUND / ARTIFACT_ACCESS_DENIED otherwise
```

Do CPU-heavy parsing with `await asyncio.to_thread(...)` so other runs keep flowing. In tests pass
`artifacts=MemoryArtifacts({"art_1": ("doc.pdf", data)})` to `invoke_node`.

## Secrets

```python
from agenticmind_node_sdk import Permissions, SecretRef, secret_field

class Cfg(NodeConfig):
    api_key: SecretRef | None = secret_field("Provider key")

@define_node(..., config=Cfg, permissions=Permissions(network=True, credentials=True))
async def handler(ctx, inputs, config):
    key = ctx.secret(config.api_key)   # allowed only for refs in this node's own config
```

In the IR the value is `{"$secret": "PROVIDER_KEY"}`; literal values are a validation error.

## Errors

Raise `NodeError(code, message)` for permanent failures and `TransientNodeError(code, message)` for
retryable ones. Codes should be stable `UPPER_SNAKE_CASE`. Any other exception becomes
`NODE_UNHANDLED_EXCEPTION` and is not retried.

## Observability

Return `usage=ModelUsage(...)` for model calls; emit custom trace events with
`await ctx.emit("tool_call", {...})`. Everything is redacted by the runtime.

## Testing

```python
from agenticmind_node_sdk.testing import invoke_node

async def test_reverse():
    r = await invoke_node(reverse, inputs={"text": "abc"}, config={"mode": "chars"})
    assert r.outputs == {"text": "cba"}
```

`invoke_node` parses config, enforces secret permissions and checks outputs against the manifest.

## Packaging & discovery

Expose a `register(registrar)` function and declare an entry point:

```toml
[project.entry-points."agenticmind.nodes"]
acme = "acme_nodes:register"
```

```python
def register(registrar):
    for d in (reverse,):
        registrar(d)
```

The registry assigns **trust** from the distribution, never from the package: `agenticmind-nodes-core` is
`core`; everything else is `community`. Only `core` runs in-process today (ADR-0004); operators can opt
other levels in with `AGENTICMIND_IN_PROCESS_TRUST` at their own risk.
