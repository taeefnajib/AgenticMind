"""Embedding providers.

* ``fastembed`` (default): local open-source ONNX models (e.g. ``BAAI/bge-small-en-v1.5``), CPU, no key.
  The model is downloaded once into the runtime's model cache. Requires the ``local`` extra.
* ``openai_compatible``: any OpenAI-style ``POST {base_url}/embeddings`` API. Defaults to OpenRouter
  (``https://openrouter.ai/api/v1``), which also serves Gemini, OpenAI, Qwen and BGE embedding models.
* ``hash``: deterministic feature-hashing embedder. No downloads, no network; meant for tests and
  offline demos. Retrieval quality is far below a real model and it is labelled as such.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import threading
from pathlib import Path
from typing import Any, Literal, Protocol

from agenticmind_node_sdk import ModelUsage
from pydantic import BaseModel

from . import ProviderError

EmbedKind = Literal["document", "query"]

DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_EMBEDDING_MODEL = "google/gemini-embedding-001"


class EmbeddingResult(BaseModel):
    vectors: list[list[float]]
    dimensions: int
    usage: ModelUsage | None = None


class EmbeddingProvider(Protocol):
    name: str

    async def embed(
        self,
        texts: list[str],
        *,
        model: str,
        kind: EmbedKind = "document",
        dimensions: int | None = None,
        batch_size: int = 32,
        api_key: str | None = None,
        base_url: str | None = None,
        cache_dir: Path | None = None,
    ) -> EmbeddingResult: ...


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


# -- hash ---------------------------------------------------------------------------------------
_WORD = re.compile(r"\w+", re.UNICODE)


class HashEmbeddings:
    """Feature hashing of unigrams + bigrams into a fixed-size, L2-normalised vector."""

    name = "hash"

    async def embed(self, texts: list[str], *, model: str = "hash", dimensions: int | None = None, **_: Any):
        dims = dimensions or 256
        vectors = []
        for text in texts:
            words = [w.lower() for w in _WORD.findall(text)]
            features = words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]
            vec = [0.0] * dims
            for feat in features:
                digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
                h = int.from_bytes(digest, "big")
                vec[h % dims] += 1.0 if (h >> 63) & 1 else -1.0
            vectors.append(_normalize(vec))
        return EmbeddingResult(vectors=vectors, dimensions=dims)


# -- fastembed ----------------------------------------------------------------------------------
_FASTEMBED_MODELS: dict[tuple[str, str], Any] = {}
_FASTEMBED_LOCK = threading.Lock()


def fastembed_supported_models() -> list[str]:
    from fastembed import TextEmbedding

    return sorted(m["model"] for m in TextEmbedding.list_supported_models())


class FastEmbedEmbeddings:
    name = "fastembed"

    def _model(self, model: str, cache_dir: Path | None) -> Any:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - depends on installed extras
            raise ProviderError(
                "PROVIDER_NOT_INSTALLED",
                "Local embeddings need the 'fastembed' package (install agenticmind-providers[local])",
            ) from exc
        key = (model, str(cache_dir or ""))
        with _FASTEMBED_LOCK:
            if key not in _FASTEMBED_MODELS:
                if model not in fastembed_supported_models():
                    raise ProviderError("EMBEDDING_MODEL_UNKNOWN", f"fastembed does not support model '{model}'")
                kwargs: dict[str, Any] = {"model_name": model}
                if cache_dir is not None:
                    kwargs["cache_dir"] = str(cache_dir)
                try:
                    _FASTEMBED_MODELS[key] = TextEmbedding(**kwargs)
                except Exception as exc:  # noqa: BLE001 - download/IO errors vary
                    raise ProviderError(
                        "EMBEDDING_MODEL_LOAD_FAILED", f"Could not load '{model}': {exc}", retryable=True
                    ) from exc
            return _FASTEMBED_MODELS[key]

    def _embed_sync(self, texts: list[str], model: str, kind: EmbedKind, batch_size: int, cache_dir: Path | None):
        m = self._model(model, cache_dir)
        if kind == "query":
            arrays = list(m.query_embed(texts))
        else:
            arrays = list(m.passage_embed(texts, batch_size=batch_size))
        return [[float(x) for x in arr] for arr in arrays]

    async def embed(
        self,
        texts: list[str],
        *,
        model: str = DEFAULT_FASTEMBED_MODEL,
        kind: EmbedKind = "document",
        batch_size: int = 32,
        cache_dir: Path | None = None,
        **_: Any,
    ) -> EmbeddingResult:
        vectors = await asyncio.to_thread(self._embed_sync, texts, model, kind, batch_size, cache_dir)
        dims = len(vectors[0]) if vectors else 0
        return EmbeddingResult(
            vectors=vectors,
            dimensions=dims,
            usage=ModelUsage(provider=self.name, model=model, estimated_cost_usd=0.0),
        )


# -- OpenAI-compatible HTTP -----------------------------------------------------------------------
class OpenAICompatibleEmbeddings:
    name = "openai_compatible"

    async def embed(
        self,
        texts: list[str],
        *,
        model: str = DEFAULT_OPENROUTER_EMBEDDING_MODEL,
        dimensions: int | None = None,
        batch_size: int = 32,
        api_key: str | None = None,
        base_url: str | None = None,
        **_: Any,
    ) -> EmbeddingResult:
        import httpx

        url = (base_url or DEFAULT_OPENROUTER_URL).rstrip("/") + "/embeddings"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        vectors: list[list[float]] = []
        prompt_tokens = 0
        async with httpx.AsyncClient(timeout=60) as client:
            for start in range(0, len(texts), batch_size):
                body: dict[str, Any] = {"model": model, "input": texts[start : start + batch_size]}
                if dimensions:
                    body["dimensions"] = dimensions
                try:
                    resp = await client.post(url, json=body, headers=headers)
                except httpx.HTTPError as exc:
                    raise ProviderError(
                        "PROVIDER_UNAVAILABLE", f"Embedding request failed: {exc}", retryable=True
                    ) from exc
                if resp.status_code in (401, 403):
                    raise ProviderError(
                        "PROVIDER_AUTH_FAILED", f"Embedding provider rejected the credentials ({resp.status_code})"
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise ProviderError(
                        "PROVIDER_UNAVAILABLE", f"Embedding provider returned {resp.status_code}", retryable=True
                    )
                if resp.status_code >= 400:
                    raise ProviderError(
                        "PROVIDER_BAD_REQUEST", f"Embedding provider returned {resp.status_code}: {resp.text[:300]}"
                    )
                payload = resp.json()
                data = payload.get("data")
                if not isinstance(data, list) or len(data) != len(body["input"]):
                    raise ProviderError("PROVIDER_BAD_RESPONSE", "Embedding response has an unexpected shape")
                data = sorted(data, key=lambda d: d.get("index", 0))
                vectors.extend([float(x) for x in item["embedding"]] for item in data)
                usage = payload.get("usage") or {}
                prompt_tokens += int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)
        dims = len(vectors[0]) if vectors else 0
        return EmbeddingResult(
            vectors=vectors,
            dimensions=dims,
            usage=ModelUsage(provider=self.name, model=model, input_tokens=prompt_tokens),
        )


_EMBEDDERS: dict[str, EmbeddingProvider] = {
    "hash": HashEmbeddings(),
    "fastembed": FastEmbedEmbeddings(),
    "openai_compatible": OpenAICompatibleEmbeddings(),
}


def get_embedding_provider(name: str) -> EmbeddingProvider:
    try:
        return _EMBEDDERS[name]
    except KeyError as exc:
        raise ProviderError("PROVIDER_UNKNOWN", f"Unknown embedding provider {name!r}") from exc


def register_embedding_provider(provider: EmbeddingProvider) -> None:
    _EMBEDDERS[provider.name] = provider
