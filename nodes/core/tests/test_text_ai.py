"""Classifier and Summarizer nodes with scripted models."""

from __future__ import annotations

import importlib

import pytest
from agenticmind_node_sdk import ModelUsage, NodeError
from agenticmind_node_sdk.manifest import effective_branches
from agenticmind_node_sdk.testing import invoke_node
from agenticmind_providers import ChatResponse, ToolCall

mod = importlib.import_module("agenticmind_nodes_core.text_ai")
MOCK = {"provider": "mock", "model": "m"}
USAGE = ModelUsage(provider="x", model="m", input_tokens=4, output_tokens=2, estimated_cost_usd=0.001)


def reply(text="", calls=None):
    return ChatResponse(text=text, tool_calls=calls or [], usage=USAGE)


class Scripted:
    def __init__(self, fn):
        self.fn = fn
        self.requests = []

    async def chat(self, request, *, api_key=None, base_url=None):
        self.requests.append(request)
        return self.fn(request)


def use(monkeypatch, fn):
    s = Scripted(fn)
    monkeypatch.setattr(mod, "get_provider", lambda name: s)
    return s


def classify(args):
    return lambda r: reply(calls=[ToolCall(id="1", name="classify", arguments=args)])


async def test_classifier_picks_a_label_and_branch(monkeypatch):
    s = use(monkeypatch, classify({"label": "negative", "confidence": 0.9, "reason": "complaint"}))
    r = await invoke_node(mod.classifier, inputs={"text": "This is awful, refund me"}, config=MOCK)
    assert r.outputs == {"label": "negative", "labels": ["negative"], "confidence": 0.9, "reason": "complaint"}
    assert r.branches == frozenset({"negative"})
    assert "- positive:" in s.requests[0].system and s.requests[0].tool_choice == "required"
    assert effective_branches(mod.classifier.manifest, {}) == ["positive", "negative", "neutral", "default"]


async def test_classifier_multi_label_and_low_confidence(monkeypatch):
    labels = [{"name": "billing"}, {"name": "bug"}, {"name": "praise", "examples": ["love it"]}]
    use(monkeypatch, classify({"labels": ["billing", "bug", "made_up"], "confidence": 0.8}))
    r = await invoke_node(mod.classifier, inputs={"text": {"text": "charged twice and the app crashed"}},
                          config={**MOCK, "labels": labels, "multi_label": True})  # fmt: skip
    assert r.outputs["labels"] == ["billing", "bug"] and r.branches == frozenset({"billing", "bug"})
    use(monkeypatch, classify({"label": "billing", "confidence": 0.3}))
    low = await invoke_node(mod.classifier, inputs={"text": "hmm"}, config={**MOCK, "labels": labels,
                                                                           "min_confidence": 0.6})  # fmt: skip
    assert low.outputs["label"] == "default" and low.branches == frozenset({"default"})
    assert "below 0.60" in low.outputs["reason"]
    use(monkeypatch, classify({"label": "none", "confidence": 0.9}))
    none = await invoke_node(mod.classifier, inputs={"text": "hello"}, config={**MOCK, "labels": labels})
    assert none.branches == frozenset({"default"})


async def test_classifier_text_fallback_and_validation(monkeypatch):
    use(monkeypatch, lambda r: reply("I would say this is Positive."))
    r = await invoke_node(mod.classifier, inputs={"text": "great"}, config=MOCK)
    assert r.outputs["label"] == "positive"
    with pytest.raises(Exception, match="reserved"):
        mod.ClassifierConfig.model_validate({**MOCK, "labels": [{"name": "a"}, {"name": "default"}]})
    with pytest.raises(Exception, match="at least two"):
        mod.ClassifierConfig.model_validate({**MOCK, "labels": [{"name": "a"}]})
    with pytest.raises(NodeError) as exc:
        await invoke_node(mod.classifier, inputs={"text": "  "}, config=MOCK)
    assert exc.value.code == "TEXT_EMPTY"


def test_split_text_cuts_at_boundaries():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 30 for i in range(10))
    chunks = mod.split_text(text, 400)
    assert all(len(c) <= 400 for c in chunks) and len(chunks) > 1
    assert all(c.startswith("Paragraph") for c in chunks)
    assert " ".join(chunks).split() == text.split()  # nothing lost
    assert mod.split_text("x" * 1000, 300) == ["x" * 300] * 3 + ["x" * 100]


def summarise_fn(request):
    p = request.messages[0].content
    if "Combine them" in p:
        return reply(f"FINAL({p.count('[Part ')})")
    if "Summarise this part" in p:
        return reply("part:" + p.split("Text:\n", 1)[1][:12])
    if "Update the summary" in p:
        return reply(p.split("Current summary:\n", 1)[1].split("\n", 1)[0] + "+")
    return reply("STUFFED")


async def test_summarizer_strategies(monkeypatch):
    s = use(monkeypatch, summarise_fn)
    short = await invoke_node(mod.summarizer, inputs={"text": "A short note."}, config={**MOCK, "style": "bullets"})
    assert short.outputs["summary"] == "STUFFED" and short.outputs["result"]["strategy"] == "stuff"
    assert "bulleted list" in s.requests[0].messages[0].content
    docs = [{"text": f"Section {i}. " + "fact " * 200} for i in range(4)]
    mr = await invoke_node(mod.summarizer, inputs={"text": docs}, config={**MOCK, "chunk_chars": 1100})
    res = mr.outputs["result"]
    assert res["strategy"] == "map_reduce" and res["chunks"] == 4 and mr.outputs["summary"] == "FINAL(4)"
    assert res["calls"] == 5 and mr.usage.input_tokens == 20 and mr.usage.estimated_cost_usd == 0.005
    rf = await invoke_node(mod.summarizer, inputs={"text": docs},
                           config={**MOCK, "chunk_chars": 1100, "strategy": "refine"})  # fmt: skip
    assert rf.outputs["summary"] == "STUFFED+++" and len(rf.outputs["result"]["partials"]) == 4


async def test_summarizer_limits(monkeypatch):
    use(monkeypatch, summarise_fn)
    with pytest.raises(NodeError) as exc:
        await invoke_node(mod.summarizer, inputs={"text": "y " * 5000}, config={**MOCK, "chunk_chars": 500,
                                                                               "max_chunks": 3})  # fmt: skip
    assert exc.value.code == "TEXT_TOO_LONG"
    # tiny chunks: hierarchical reduce must still terminate
    r = await invoke_node(mod.summarizer, inputs={"text": "z. " * 800}, config={**MOCK, "chunk_chars": 500})
    assert r.outputs["summary"].startswith("FINAL")
