import pytest
from agenticmind_node_sdk import NodeError
from agenticmind_node_sdk.testing import invoke_node
from agenticmind_nodes_core import ALL_NODES
from agenticmind_nodes_core.control import conditional
from agenticmind_nodes_core.io import manual_input, output
from agenticmind_nodes_core.models import llm
from agenticmind_nodes_core.transform import (
    collect,
    json_select,
    parse_json,
    text_template,
    text_transform,
    to_text,
)


def test_all_nodes_have_unique_types_and_descriptions():
    keys = [d.key for d in ALL_NODES]
    assert len(keys) == len(set(keys))
    assert all(d.manifest.description for d in ALL_NODES)


async def test_manual_input_reads_run_input_and_types():
    r = await invoke_node(manual_input, config={"key": "q"}, run_input={"q": "hi"})
    assert r.outputs == {"value": "hi"}
    r = await invoke_node(manual_input, config={"key": "n", "value_type": "Number"}, run_input={"n": 3})
    assert r.outputs == {"value": 3}
    with pytest.raises(NodeError) as exc:
        await invoke_node(manual_input, config={"key": "q"})
    assert exc.value.code == "INPUT_MISSING"
    with pytest.raises(NodeError) as exc:
        await invoke_node(manual_input, config={"key": "n", "value_type": "Number"}, run_input={"n": "x"})
    assert exc.value.code == "INPUT_TYPE_MISMATCH"
    r = await invoke_node(manual_input, config={"key": "q", "default": "dflt"})
    assert r.outputs == {"value": "dflt"}


async def test_output_contributes_run_outputs():
    r = await invoke_node(output, inputs={"value": 5}, config={"key": "answer"})
    assert r.run_outputs == {"answer": 5}


async def test_text_template():
    r = await invoke_node(
        text_template,
        inputs={"text": "Ada", "variables": {"user": {"role": "admin"}}},
        config={"template": "Hi {{ text }} ({{user.role}})"},
    )
    assert r.outputs["text"] == "Hi Ada (admin)"
    with pytest.raises(NodeError) as exc:
        await invoke_node(text_template, config={"template": "{{ missing }}"})
    assert exc.value.code == "TEMPLATE_MISSING_VARIABLE"
    r = await invoke_node(text_template, config={"template": "[{{ missing }}]", "strict": False})
    assert r.outputs["text"] == "[]"


@pytest.mark.parametrize(
    "op,expected", [("uppercase", "HELLO WORLD"), ("slugify", "hello-world"), ("trim", "Hello World")]
)
async def test_text_transform(op, expected):
    r = await invoke_node(text_transform, inputs={"text": " Hello World "}, config={"operation": op})
    assert r.outputs["text"].strip() == expected


async def test_json_select_and_conversions():
    r = await invoke_node(json_select, inputs={"data": {"a": [{"b": 1}]}}, config={"path": "a.0.b"})
    assert r.outputs["value"] == 1
    with pytest.raises(NodeError):
        await invoke_node(json_select, inputs={"data": {}}, config={"path": "x"})
    assert (await invoke_node(to_text, inputs={"value": {"a": 1}})).outputs["text"] == '{"a": 1}'
    assert (await invoke_node(parse_json, inputs={"text": "[1,2]"})).outputs["value"] == [1, 2]
    with pytest.raises(NodeError):
        await invoke_node(parse_json, inputs={"text": "{bad"})
    assert (await invoke_node(collect, inputs={"items": [1, "a"]})).outputs["items"] == [1, "a"]


async def test_conditional_selects_branch():
    r = await invoke_node(conditional, inputs={"value": 10}, config={"operator": "greater_than", "operand": 5})
    assert r.branches == frozenset({"true"})
    r = await invoke_node(conditional, inputs={"value": ""}, config={"operator": "is_truthy"})
    assert r.branches == frozenset({"false"})
    with pytest.raises(NodeError):
        await invoke_node(conditional, inputs={"value": "x"}, config={"operator": "less_than", "operand": 1})


async def test_llm_mock_reports_usage():
    r = await invoke_node(llm, inputs={"prompt": "what is RAG"}, config={"provider": "mock", "model": "demo"})
    assert r.outputs["text"] == "[mock:demo] what is RAG"
    assert r.usage is not None and r.usage.provider == "mock"


def test_llm_config_requires_credentials_for_openrouter():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        llm.parse_config({})  # default provider is openrouter
    cfg = llm.parse_config({"api_key": {"$secret": "OPENROUTER_API_KEY"}})
    assert cfg.provider == "openrouter" and cfg.model == "google/gemini-3.8-flash"
    with pytest.raises(ValidationError):
        llm.parse_config({"provider": "openai_compatible"})


async def test_llm_system_input_and_legacy_version():
    from agenticmind_nodes_core.models import llm_v1

    r = await invoke_node(llm, inputs={"prompt": "hi", "system": "be brief"}, config={"provider": "mock", "model": "m"})
    assert r.outputs["text"] == "[mock:m] hi"
    legacy = await invoke_node(llm_v1, inputs={"prompt": "hi"})
    assert legacy.outputs["text"] == "[mock:mock-1] hi"
    assert llm_v1.manifest.version == "1.0.0" and llm.manifest.version == "1.1.0"


async def test_llm_secret_reaches_provider(monkeypatch):
    import httpx

    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "cost": 0.00001},
            },
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    r = await invoke_node(
        llm,
        inputs={"prompt": "hi"},
        config={"api_key": {"$secret": "OPENROUTER_API_KEY"}},
        secrets={"OPENROUTER_API_KEY": "sk-or-unit"},
    )
    assert seen == ["Bearer sk-or-unit"] and r.outputs["text"] == "hello"
    assert r.usage.estimated_cost_usd == pytest.approx(0.00001)


async def test_llm_empty_response_is_an_error(monkeypatch):
    import httpx

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
                )
            ),
            **kw,
        ),
    )
    with pytest.raises(NodeError) as exc:
        await invoke_node(llm, inputs={"prompt": "hi"}, config={"api_key": {"$secret": "K"}}, secrets={"K": "v"})
    assert exc.value.code == "MODEL_EMPTY_RESPONSE" and "length" in str(exc.value)
