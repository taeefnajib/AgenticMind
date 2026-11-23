"""AgenticMind core node package."""

from __future__ import annotations

from typing import Any

from agenticmind_node_sdk import NodeDefinition

from .approval import human_approval
from .code import code
from .control import conditional
from .flow import join, loop, loop_end, loop_end_v1, router
from .guardrails import input_guard, output_guard, pii_redactor
from .io import delay, image_input, manual_input, output
from .models import llm, llm_v1, llm_v12, model_router
from .text_ai import classifier, summarizer
from .transform import collect, json_select, parse_json, text_template, text_transform, to_text
from .triggers import chat_trigger, queue_trigger, schedule_trigger, webhook_trigger

ALL_NODES: tuple[NodeDefinition[Any], ...] = (
    manual_input,
    image_input,
    webhook_trigger,
    schedule_trigger,
    queue_trigger,
    chat_trigger,
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
    llm_v12,
    llm,
    llm_v1,
    model_router,
    classifier,
    summarizer,
    input_guard,
    output_guard,
    pii_redactor,
    code,
)


def register(registrar: Any) -> None:
    """Entry point for the ``agenticmind.nodes`` group."""
    for definition in ALL_NODES:
        registrar(definition)


__all__ = ["ALL_NODES", "register"]
