"""Model nodes pass their reasoning effort to providers; trace summaries keep token counts (ADR-0041)."""

from __future__ import annotations

from agenticmind_node_sdk.testing import invoke_node
from agenticmind_nodes_core import models
from agenticmind_nodes_core.models import llm_v12
from agenticmind_providers import ChatResponse, ModelUsage
from agenticmind_runtime.redaction import REDACTED, summarize


class Capture:
    def __init__(self):
        self.requests = []

    async def chat(self, request, *, api_key=None, base_url=None):
        self.requests.append(request)
        return ChatResponse(text="ok", usage=ModelUsage(provider="x", model="m"))


async def test_llm_node_sends_effort_only_when_set(monkeypatch):
    cap = Capture()
    monkeypatch.setattr(models, "get_provider", lambda name: cap)
    await invoke_node(
        llm_v12, inputs={"prompt": "hi"}, config={"provider": "mock", "model": "m", "reasoning_effort": "low"}
    )
    await invoke_node(llm_v12, inputs={"prompt": "hi"}, config={"provider": "mock", "model": "m"})
    assert [r.reasoning_effort for r in cap.requests] == ["low", None]


def test_token_counts_are_not_redacted_but_credentials_are():
    out = summarize(
        {
            "input_tokens": 5,
            "output_tokens": 7,
            "max_tokens": 9,
            "token_budget": 3,
            "access_token": "a",
            "refresh_token": "b",
            "access_tokens": ["c"],
            "api_key": "d",
        }
    )
    assert (
        out["input_tokens"] == 5 and out["output_tokens"] == 7 and out["max_tokens"] == 9 and out["token_budget"] == 3
    )
    assert {out[k] for k in ("access_token", "refresh_token", "access_tokens", "api_key")} == {REDACTED}
