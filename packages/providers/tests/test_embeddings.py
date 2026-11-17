import json
import math
import os

import httpx
import pytest
from agenticmind_providers import ProviderError
from agenticmind_providers.embeddings import (
    HashEmbeddings,
    OpenAICompatibleEmbeddings,
    get_embedding_provider,
)


def cos(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_hash_embeddings_are_deterministic_normalised_and_meaningful():
    e = HashEmbeddings()
    r = await e.embed(["annual leave policy", "annual leave policy", "server rack cooling"], dimensions=128)
    assert r.dimensions == 128
    a, b, c = r.vectors
    assert a == b
    assert math.isclose(sum(x * x for x in a), 1.0, rel_tol=1e-9)
    q = (await e.embed(["how much annual leave"], dimensions=128)).vectors[0]
    assert cos(q, a) > cos(q, c)


def make_transport(captured):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append((str(request.url), request.headers.get("authorization"), body))
        n = len(body["input"])
        if body["input"][0] == "fail":
            return httpx.Response(429, json={"error": "rate limited"})
        data = [{"index": i, "embedding": [float(i), 1.0]} for i in reversed(range(n))]
        return httpx.Response(200, json={"data": data, "usage": {"prompt_tokens": 7}})

    return httpx.MockTransport(handler)


async def test_openai_compatible_batches_orders_and_authenticates(monkeypatch):
    captured = []
    transport = make_transport(captured)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    r = await OpenAICompatibleEmbeddings().embed(
        ["a", "b", "c"], model="google/gemini-embedding-001", api_key="k", batch_size=2
    )
    assert r.vectors == [[0.0, 1.0], [1.0, 1.0], [0.0, 1.0]]  # re-ordered by index within each batch
    assert r.usage.input_tokens == 14 and r.dimensions == 2
    url, auth, body = captured[0]
    assert url == "https://openrouter.ai/api/v1/embeddings" and auth == "Bearer k"
    assert body == {"model": "google/gemini-embedding-001", "input": ["a", "b"]}


async def test_openai_compatible_errors_are_classified(monkeypatch):
    transport = make_transport([])
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    with pytest.raises(ProviderError) as exc:
        await OpenAICompatibleEmbeddings().embed(["fail"], model="m")
    assert exc.value.code == "PROVIDER_UNAVAILABLE" and exc.value.retryable


def test_registry():
    assert get_embedding_provider("fastembed").name == "fastembed"
    with pytest.raises(ProviderError):
        get_embedding_provider("nope")


@pytest.mark.skipif(not os.environ.get("AGENTICMIND_TEST_FASTEMBED"), reason="downloads a ~65 MB model")
async def test_fastembed_real_model(tmp_path):
    e = get_embedding_provider("fastembed")
    docs = await e.embed(
        ["Employees receive 25 days of annual leave.", "Servers are cooled by fans."], cache_dir=tmp_path
    )
    q = await e.embed(["How many vacation days do I get?"], kind="query", cache_dir=tmp_path)
    assert docs.dimensions == 384
    assert cos(q.vectors[0], docs.vectors[0]) > cos(q.vectors[0], docs.vectors[1])
