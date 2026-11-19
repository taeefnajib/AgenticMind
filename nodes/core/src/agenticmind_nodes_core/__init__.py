"""AgenticMind core node package."""

from __future__ import annotations

from typing import Any

from agenticmind_node_sdk import NodeDefinition

from .approval import human_approval
from .control import conditional
from .flow import join, loop, loop_end, loop_end_v1, router
from .io import delay, manual_input, output
from .models import llm, llm_v1, model_router
from .transform import collect, json_select, parse_json, text_template, text_transform, to_text

ALL_NODES: tuple[NodeDefinition[Any], ...] = (
    manual_input,
    output,
    delay,
    text_template,
    text_transform,
    json_select,
    to_text,
    parse_json,
    collect,
    conditional,
    loop,
    loop_end,
    loop_end_v1,
    router,
    join,
    human_approval,
    llm,
    llm_v1,
    model_router,
)


def register(registrar: Any) -> None:
    """Entry point for the ``agenticmind.nodes`` group."""
    for definition in ALL_NODES:
        registrar(definition)


__all__ = ["ALL_NODES", "register"]
