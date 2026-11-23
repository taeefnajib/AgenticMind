"""Code node (ADR-0034): run Python or JavaScript in the sandbox.

The code gets the connected value as ``inputs`` and returns a JSON value: in Python set ``result`` (or define
``main(inputs)``), in JavaScript ``return`` it. Isolation is decided by the operator (``AGENTICMIND_SANDBOX``),
not by the graph: Docker containers by default, or a local process for trusted use.
"""

from __future__ import annotations

from typing import Literal

from agenticmind_node_sdk import (
    ExecutionPolicy,
    InputPort,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    Permissions,
    PortType,
    define_node,
    ui,
)
from pydantic import Field

from .io import PACKAGE

PY_EXAMPLE = """# `inputs` holds the connected value. Set `result` to a JSON value.
numbers = inputs or [3, 1, 4, 1, 5, 9]
result = {"count": len(numbers), "mean": sum(numbers) / len(numbers), "max": max(numbers)}
"""


class CodeConfig(NodeConfig):
    language: Literal["python", "javascript"] = "python"
    code: str = Field(
        default=PY_EXAMPLE, min_length=1, max_length=200_000,
        description="Python: set `result` (or define main(inputs)). JavaScript: `return` the value. "
        "The connected value is `inputs`.",
        json_schema_extra=ui(widget="code"),
    )  # fmt: skip
    timeout_seconds: float = Field(default=30, ge=1, le=3600, description="Capped by the server's limit.")
    memory_mb: int = Field(default=256, ge=32, le=16_384, json_schema_extra=ui(advanced=True))
    network: bool = Field(
        default=False, description="Ask for network access (only if the server allows it).",
        json_schema_extra=ui(advanced=True),
    )  # fmt: skip
    on_error: Literal["fail", "output"] = Field(
        default="fail", description="fail: an exception fails the node. output: return it in `error`."
    )


async def run_code(language: str, code: str, inputs: object, *, timeout: float, memory_mb: int, network: bool):  # noqa: ANN201
    from agenticmind_sandbox import Limits, SandboxError, execute

    try:
        return await execute(language, code, inputs, Limits(timeout_seconds=timeout, memory_mb=memory_mb,
                                                            network=network))  # type: ignore[arg-type]  # fmt: skip
    except SandboxError as exc:
        raise NodeError(exc.code, exc.message) from exc


def error_text(error: dict | None) -> str:
    if not error:
        return "The code failed"
    head = f"{error.get('type', 'Error')}: {error.get('message', '')}".strip()
    tb = str(error.get("traceback") or "").strip()
    return f"{head}\n{tb[-3000:]}" if tb and tb != head else head


@define_node(
    type="agenticmind.core.code",
    version="1.0.0",
    display_name="Code",
    category="transform",
    package=PACKAGE,
    description="Runs Python or JavaScript in a sandbox (Docker by default) on the connected value.",
    inputs=[
        InputPort(name="inputs", type=PortType.ANY, required=False, description="Available to the code as `inputs`")
    ],
    outputs=[
        OutputPort(name="result", type=PortType.ANY, description="The value the code returned"),
        OutputPort(name="stdout", type=PortType.STRING, description="What the code printed"),
        OutputPort(name="error", type=PortType.JSON, description="{type, message, traceback} or null"),
        OutputPort(name="report", type=PortType.JSON, description="Backend, duration, exit code, notes"),
    ],
    config=CodeConfig,
    permissions=Permissions(subprocess=True),
    # the sandbox enforces the code's own timeout; this outer limit also covers a first image pull
    execution=ExecutionPolicy(timeout_seconds=900, max_attempts=1, idempotent=False),
    tags=["code", "python", "javascript", "sandbox"],
)
async def code(ctx: NodeContext, inputs, config: CodeConfig) -> NodeResult:
    r = await run_code(config.language, config.code, inputs.get("inputs"), timeout=config.timeout_seconds,
                       memory_mb=config.memory_mb, network=config.network)  # fmt: skip
    report = {"backend": r.backend, "duration_ms": r.duration_ms, "exit_code": r.exit_code,
              "timed_out": r.timed_out, "notes": r.notes}  # fmt: skip
    if r.stderr.strip():
        report["stderr"] = r.stderr[-4000:]
    await ctx.emit("code_executed", {"language": config.language, "ok": r.ok, **report})
    if not r.ok and config.on_error == "fail":
        raise NodeError("CODE_FAILED", error_text(r.error))
    return NodeResult(outputs={"result": r.result if r.ok else None, "stdout": r.stdout,
                               "error": None if r.ok else r.error, "report": report})  # fmt: skip
