"""Code Interpreter Tool (ADR-0034): lets an agent write and run code in the sandbox.

The model sends ``code`` (and optionally ``inputs``); the tool returns the result and what the code printed,
or the exception with its traceback so the model can fix the code and try again. Each call is a fresh
sandbox: nothing persists between calls.
"""

from __future__ import annotations

from typing import Any, Literal

from agenticmind_node_sdk import (
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    PortType,
    ToolHandle,
    define_node,
)
from agenticmind_node_sdk.handles import handle_value
from pydantic import Field

from .tools import PACKAGE, ToolError, _description_field, _name_field, implementation

_HOW = {
    "python": "Python 3 (standard library only). Set `result` to the value to return, or print it.",
    "javascript": "JavaScript (Node.js, no modules). The code is an async function body: `return` the value.",
}


class CodeToolConfig(NodeConfig):
    name: str = _name_field("run_code")
    description: str = _description_field(
        "Runs code in a sandbox and returns its result and printed output. Use it for exact calculations, data "
        "processing and checking your reasoning; each call starts fresh."
    )
    language: Literal["python", "javascript"] = "python"
    timeout_seconds: float = Field(default=30, ge=1, le=600)
    memory_mb: int = Field(default=256, ge=32, le=8192)


@define_node(
    type="agenticmind.agents.code_tool",
    version="1.0.0",
    display_name="Code Interpreter Tool",
    category="tools",
    package=PACKAGE,
    description="Lets an agent write and run Python or JavaScript in a sandbox (Docker by default).",
    outputs=[OutputPort(name="tool", type=PortType.TOOL)],
    config=CodeToolConfig,
    tags=["tools", "code", "sandbox"],
)
async def code_tool(ctx: NodeContext, inputs, config: CodeToolConfig) -> NodeResult:
    handle = ToolHandle(
        tool_type="code",
        name=config.name,
        description=f"{config.description}\nLanguage: {_HOW[config.language]}",
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Complete program to run"},
                "inputs": {"description": "Optional JSON data, available to the code as `inputs`"},
            },
            "required": ["code"],
            "additionalProperties": False,
        },
        binding={
            "language": config.language,
            "timeout_seconds": config.timeout_seconds,
            "memory_mb": config.memory_mb,
        },  # fmt: skip
        permissions=["subprocess"],
    )
    return NodeResult(outputs={"tool": handle_value(handle)})


@implementation("code")
async def _run_code(ctx: NodeContext, tool: ToolHandle, args: dict[str, Any]) -> Any:
    from agenticmind_nodes_core.code import error_text, run_code

    source = str(args.get("code", ""))
    if not source.strip():
        raise ToolError("code must not be empty")
    b = tool.binding
    try:
        r = await run_code(b.get("language", "python"), source, args.get("inputs"),
                           timeout=float(b.get("timeout_seconds", 30)), memory_mb=int(b.get("memory_mb", 256)),
                           network=False)  # fmt: skip
    except NodeError as exc:  # sandbox unavailable / disabled: the model cannot fix that, but should know
        raise ToolError(f"{exc.code}: {exc.message}") from exc
    if not r.ok:
        printed = f"\nPrinted before the error:\n{r.stdout[-2000:]}" if r.stdout else ""
        raise ToolError(error_text(r.error) + printed)
    return {"result": r.result, "stdout": r.stdout[-8000:]}
