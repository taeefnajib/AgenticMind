import pytest
from agenticmind_providers import ChatMessage, ChatRequest, ProviderError, get_provider


async def test_mock_provider_is_deterministic_and_labelled():
    p = get_provider("mock")
    req = ChatRequest(model="m1", messages=[ChatMessage(role="user", content="hello world")])
    a = await p.chat(req)
    b = await p.chat(req)
    assert a == b
    assert a.text == "[mock:m1] hello world"
    assert a.usage.input_tokens == 2 and a.usage.estimated_cost_usd == 0.0


def test_unknown_provider():
    with pytest.raises(ProviderError):
        get_provider("nope")
