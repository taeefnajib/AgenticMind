"""Human Approval: a durable pause point (ADR-0013).

On first execution the node suspends the run with a request (what to review, instructions) and a JSON
Schema for the response. Nothing waits in memory: the run is checkpointed and ends with status
``waiting``. When someone responds through ``POST /api/runs/{id}/resume``, the run continues and this node
activates its ``approved`` or ``rejected`` branch.
"""

from __future__ import annotations

from typing import Any

from agenticmind_node_sdk import (
    InputPort,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    PortType,
    SuspendNode,
    define_node,
    ui,
)
from pydantic import Field

from .io import PACKAGE

DECISIONS = ("approved", "rejected")


class ApprovalConfig(NodeConfig):
    title: str = Field(default="Approval needed", min_length=1, max_length=200)
    instructions: str = Field(
        default="Review the content and approve or reject it.",
        max_length=4000,
        description="Shown to the reviewer.",
        json_schema_extra=ui(widget="textarea"),
    )
    allow_edit: bool = Field(default=False, description="Let the reviewer change the value before approving.")
    require_comment_on_reject: bool = Field(default=True, description="Rejections must explain why.")


def response_schema(config: ApprovalConfig) -> dict[str, Any]:
    props: dict[str, Any] = {
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "comment": {"type": "string", "maxLength": 4000},
        "reviewer": {"type": "string", "maxLength": 200},
    }
    if config.allow_edit:
        props["value"] = {"description": "Edited value (optional)"}
    schema: dict[str, Any] = {
        "type": "object",
        "properties": props,
        "required": ["decision"],
        "additionalProperties": False,
    }
    if config.require_comment_on_reject:
        schema["if"] = {"properties": {"decision": {"const": "rejected"}}}
        schema["then"] = {"required": ["comment"], "properties": {"comment": {"minLength": 1}}}
    return schema


@define_node(
    type="agenticmind.core.human_approval",
    version="1.0.0",
    display_name="Human Approval",
    category="human",
    package=PACKAGE,
    description="Pauses the run until a person approves or rejects (optionally editing the value), then continues.",
    inputs=[InputPort(name="value", type=PortType.ANY, description="What the reviewer sees")],
    outputs=[
        OutputPort(name="value", type=PortType.ANY, description="The value, as approved (or edited)"),
        OutputPort(name="decision", type=PortType.STRING, description="approved or rejected"),
        OutputPort(name="comment", type=PortType.STRING),
        OutputPort(name="reviewer", type=PortType.STRING),
    ],
    branches=list(DECISIONS),
    config=ApprovalConfig,
    can_suspend=True,
    tags=["human", "control"],
)
async def human_approval(ctx: NodeContext, inputs, config: ApprovalConfig) -> NodeResult:
    response = ctx.resume_value
    if response is None:
        raise SuspendNode(
            request={
                "kind": "approval",
                "title": config.title,
                "instructions": config.instructions,
                "value": inputs["value"],
                "allow_edit": config.allow_edit,
                "require_comment_on_reject": config.require_comment_on_reject,
            },
            response_schema=response_schema(config),
        )
    # The API validates against the schema; re-check here so the node is safe on its own.
    if not isinstance(response, dict) or response.get("decision") not in DECISIONS:
        raise NodeError("APPROVAL_INVALID_RESPONSE", "Response must include decision: approved or rejected")
    decision = response["decision"]
    comment = str(response.get("comment") or "")
    if decision == "rejected" and config.require_comment_on_reject and not comment.strip():
        raise NodeError("APPROVAL_INVALID_RESPONSE", "A rejection needs a comment")
    value = response["value"] if config.allow_edit and "value" in response else inputs["value"]
    await ctx.emit("approval_decision", {"decision": decision, "edited": value is not inputs["value"]})
    return NodeResult(
        outputs={
            "value": value,
            "decision": decision,
            "comment": comment,
            "reviewer": str(response.get("reviewer") or ""),
        },
        branches=frozenset({decision}),
    )
