"""Model Router: keyword, classifier and hybrid routing; forced routes; fallbacks; usage."""

from __future__ import annotations

import json

import pytest
from agenticmind_node_sdk import ModelUsage, NodeError
from agenticmind_node_sdk.testing import invoke_node
from agenticmind_nodes_core import models
from agenticmind_nodes_core.models import keyword_route, model_router, parse_route_choice
from agenticmind_providers import ChatResponse, ProviderError

KEY = {"api_key": {"$secret": "OR_KEY"}}
SECRETS = {"OR_KEY": "fixture-value"}


class FakeProvider:
    """Answers classifier requests from ``choices`` and everything else with the model id."""

    def __init__(self, choices: list[str] | None = None, fail: ProviderError | None = None) -> None:
        self.choices = list(choices or [])
        self.fail = fail
        self.requests = []

    async def chat(self, request, *, api_key=None, base_url=None):
        self.requests.append((request, api_key))
        is_classifier = request.system and "route requests" in request.system
        if is_classifier and self.fail:
            raise self.fail
        text = self.choices.pop(0) if is_classifier else f"answer from {request.model}"
        usage = ModelUsage(
            provider="openrouter", model=request.model, input_tokens=10, output_tokens=2, estimated_cost_usd=0.001
        )
        return ChatResponse(text=text, usage=usage)


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        provider = FakeProvider(**kw)
        monkeypatch.setattr(models, "get_provider", lambda name: provider)
        return provider

    return install


async def route(prompt, fake_provider, **config):
    return await invoke_node(model_router, inputs={"prompt": prompt}, config={**KEY, **config}, secrets=SECRETS)


async def test_keywords_pick_the_first_matching_route_without_a_classifier_call(fake):
    p = fake()
    r = await route("Why does this Python function raise a KeyError?", p, strategy="keywords")
    assert r.outputs == {"text": "answer from qwen/qwen3-coder", "route": "code", "model": "qwen/qwen3-coder"}
    assert len(p.requests) == 1 and p.requests[0][1] == "fixture-value"
    # whole words only: "planet" does not contain the keyword "plan"; nothing matches → default route
    r = await route("Name the largest planet", p, strategy="keywords")
    assert r.outputs["route"] == "general" and r.outputs["model"] == "google/gemini-3.8-flash"


async def test_classifier_chooses_from_route_descriptions_and_usage_adds_up(fake):
    p = fake(choices=['Sure: {"route": "reasoning", "reason": "multi-step maths"}'])
    r = await route("A train leaves at 3pm going 80 km/h...", p, strategy="classifier")
    assert r.outputs["route"] == "reasoning" and r.outputs["text"] == "answer from anthropic/claude-sonnet-5.5"
    classifier, _ = p.requests[0]
    assert classifier.model == "google/gemini-3.1-flash-lite" and classifier.max_tokens >= 512
    assert "- code: Writing, explaining" in classifier.messages[0].content
    assert r.usage.input_tokens == 20 and r.usage.estimated_cost_usd == pytest.approx(0.002)
    assert r.usage.model == "anthropic/claude-sonnet-5.5"


async def test_hybrid_uses_keywords_first_then_the_classifier(fake):
    p = fake(choices=["quick"])  # a bare route name is accepted too
    r = await route("Please calculate 17% of 240", p)  # default strategy: hybrid
    assert r.outputs["route"] == "reasoning" and len(p.requests) == 1  # keyword, no classifier call
    r = await route("hello there!", p)
    assert r.outputs["route"] == "quick" and len(p.requests) == 3


async def test_invalid_or_failed_classifier_falls_back_to_the_default_route(fake):
    p = fake(choices=['{"route": "astrology"}'])
    r = await route("tell me something", p, strategy="classifier", default_route="quick")
    assert r.outputs["route"] == "quick"
    p = fake(fail=ProviderError("PROVIDER_ERROR", "bad request"))
    r = await route("tell me something", p, strategy="classifier")
    assert r.outputs["route"] == "general"
    p = fake(fail=ProviderError("PROVIDER_TIMEOUT", "slow", retryable=True))
    with pytest.raises(NodeError) as exc:  # transient: let the engine retry the node
        await route("tell me something", p, strategy="classifier")
    assert exc.value.code == "PROVIDER_TIMEOUT"


async def test_forced_route_custom_routes_and_system_prompts(fake):
    p = fake()
    routes = [
        {"name": "legal", "description": "contracts", "model": "vendor/legal-1", "system_prompt": "Cite clauses."},
        {"name": "chat", "model": "vendor/chat-1"},
    ]
    r = await invoke_node(
        model_router,
        inputs={"prompt": "hi", "route": "legal"},
        config={**KEY, "routes": routes, "default_route": "chat", "system_prompt": "Be brief."},
        secrets=SECRETS,
    )
    assert r.outputs["model"] == "vendor/legal-1" and p.requests[0][0].system == "Cite clauses."
    with pytest.raises(NodeError) as exc:
        await invoke_node(
            model_router,
            inputs={"prompt": "hi", "route": "nope"},
            config={**KEY, "routes": routes, "default_route": "chat"},
            secrets=SECRETS,
        )
    assert exc.value.code == "ROUTE_UNKNOWN"


def test_config_is_validated():
    cfg = models.ModelRouterConfig
    with pytest.raises(ValueError, match="default_route"):
        cfg(**KEY, default_route="missing")
    with pytest.raises(ValueError, match="unique"):
        cfg(**KEY, routes=[{"name": "a", "model": "m"}, {"name": "a", "model": "n"}], default_route="a")
    with pytest.raises(ValueError, match="pattern"):
        cfg(**KEY, routes=[{"name": "Has Space", "model": "m"}], default_route="Has Space")
    with pytest.raises(ValueError, match="api_key"):
        cfg()
    assert [r.name for r in cfg(**KEY).routes] == ["general", "quick", "reasoning", "code"]


def test_helpers():
    routes = models._default_routes()
    assert keyword_route("Fix this SQL query", routes)[0].name == "code"
    assert keyword_route("step by step please", routes)[1] == "keyword 'step by step'"
    assert keyword_route("nothing here", routes) is None
    names = {"a", "b"}
    assert parse_route_choice('```json\n{"route": "b", "reason": "x"}\n```', names) == ("b", "x")
    assert parse_route_choice('"A".', names) == ("a", "")
    assert parse_route_choice(json.dumps({"route": "z"}), names) is None
