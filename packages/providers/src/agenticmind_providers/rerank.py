"""Rerankers: score (query, passage) pairs more precisely than first-stage retrieval (ADR-0015).

* ``fastembed`` - local open-source cross-encoder (default ``Xenova/ms-marco-MiniLM-L-6-v2``, ~80 MB,
  downloaded once). Raw logits are mapped to 0..1 with a sigmoid so thresholds are interpretable.
* ``overlap`` - deterministic term-overlap scorer for tests and offline demos (low quality).

LLM-based reranking lives in the Reranker node itself (it needs the chat provider and a secret).
"""

from __future__ import annotations

import asyncio
import math
import re
import threading
from pathlib import Path
from typing import Any

from . import ProviderError

DEFAULT_RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
_MODELS: dict[tuple[str, str], Any] = {}
_LOCK = threading.Lock()
_WORD = re.compile(r"\w+", re.UNICODE)


def rerank_supported_models() -> list[str]:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return sorted(m["model"] for m in TextCrossEncoder.list_supported_models())


def _cross_encoder(model: str, cache_dir: Path | None) -> Any:
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
    except ImportError as exc:  # pragma: no cover - depends on installed extras
        raise ProviderError(
            "PROVIDER_NOT_INSTALLED", "Local reranking needs the 'fastembed' package (agenticmind-providers[local])"
        ) from exc
    key = (model, str(cache_dir or ""))
    with _LOCK:
        if key not in _MODELS:
            if model not in rerank_supported_models():
                raise ProviderError("RERANK_MODEL_UNKNOWN", f"fastembed has no reranker '{model}'")
            kwargs: dict[str, Any] = {"model_name": model}
            if cache_dir is not None:
                kwargs["cache_dir"] = str(cache_dir)
            try:
                _MODELS[key] = TextCrossEncoder(**kwargs)
            except Exception as exc:  # noqa: BLE001 - download/IO errors vary
                raise ProviderError(
                    "RERANK_MODEL_LOAD_FAILED", f"Could not load '{model}': {exc}", retryable=True
                ) from exc
        return _MODELS[key]


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x)) if x > -60 else 0.0


def _overlap_scores(query: str, passages: list[str]) -> list[float]:
    q = {w for w in _WORD.findall(query.lower()) if len(w) > 2}
    out = []
    for p in passages:
        words = set(_WORD.findall(p.lower()))
        out.append(round(len(q & words) / len(q), 4) if q else 0.0)
    return out


async def rerank_scores(
    provider: str, query: str, passages: list[str], *, model: str = DEFAULT_RERANK_MODEL, cache_dir: Path | None = None
) -> list[float]:
    """Relevance in 0..1 for each passage, in input order."""
    if not passages:
        return []
    if provider == "overlap":
        return _overlap_scores(query, passages)
    if provider != "fastembed":
        raise ProviderError("RERANK_PROVIDER_UNKNOWN", f"Unknown reranker provider '{provider}'")

    def run() -> list[float]:
        encoder = _cross_encoder(model, cache_dir)
        return [round(_sigmoid(float(s)), 6) for s in encoder.rerank(query, passages)]

    return await asyncio.to_thread(run)
