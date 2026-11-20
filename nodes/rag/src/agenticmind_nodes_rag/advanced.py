"""Advanced retrieval (ADR-0015): Keyword Search, Hybrid Search, Reranker, Query Rewriter, Context Compressor.

Typical query graph::

    question → Query Rewriter → Hybrid Search (vector + BM25, fused) → Reranker → Context Compressor
             → Context Builder → LLM → Citation Builder

Every stage keeps document IDs and sources intact, so citations and evaluation still work end to end.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Literal

from agenticmind_node_sdk import (
    EmbeddingModelHandle,
    ExecutionPolicy,
    InputPort,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    Permissions,
    PortType,
    RetrievedDocument,
    SecretRef,
    TransientNodeError,
    VectorStoreHandle,
    define_node,
    keyword_scores,
    secret_field,
    ui,
)
from agenticmind_providers import (
    ChatMessage,
    ChatProviderName,
    ChatRequest,
    ProviderError,
    ToolSpec,
    check_chat_provider,
    get_provider,
)
from agenticmind_providers.chat import DEFAULT_OPENROUTER_MODEL
from agenticmind_providers.rerank import DEFAULT_RERANK_MODEL, rerank_scores
from pydantic import Field, JsonValue, model_validator

from .retrieval import _STORE_PERMISSIONS, PACKAGE, embed_texts, open_keyword_index, open_vector_store, store_call

MAX_QUERIES = 8


def _retrieved(document: dict[str, Any], method: str, score: float, rerank: float | None = None) -> RetrievedDocument:
    base = {k: v for k, v in document.items() if k in RetrievedDocument.model_fields}
    base.update(retrieval_method=method, score=round(score, 6))
    if rerank is not None:
        base["rerank_score"] = round(rerank, 6)
    return RetrievedDocument.model_validate(base)


def _out(docs: list[RetrievedDocument]) -> list[dict[str, Any]]:
    return [d.model_dump(mode="json", exclude_none=True) for d in docs]


def _parse_retrieved(value: Any) -> list[RetrievedDocument]:
    if not isinstance(value, list):
        raise NodeError("DOCUMENTS_INVALID", "Expected a list of documents")
    docs = []
    for item in value:
        if not isinstance(item, dict):
            raise NodeError("DOCUMENTS_INVALID", "Expected a list of documents")
        item = {"retrieval_method": "unknown", "score": 0.0, **item}
        docs.append(
            RetrievedDocument.model_validate({k: v for k, v in item.items() if k in RetrievedDocument.model_fields})
        )
    return docs


# ---- fusion -------------------------------------------------------------------------------------
def rrf_fuse(ranked_lists: list[list[str]], k: int = 60) -> dict[str, float]:
    """Reciprocal rank fusion: sum of 1 / (k + rank) over every list an item appears in."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


def _minmax(scores: dict[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    lo, hi = min(scores.values()), max(scores.values())
    if hi - lo < 1e-12:
        return {k: 1.0 for k in scores}
    return {k: (v - lo) / (hi - lo) for k, v in scores.items()}


def weighted_fuse(vector: dict[str, float], keyword: dict[str, float], vector_weight: float) -> dict[str, float]:
    """Min-max normalise each method's scores, then mix: w * vector + (1 - w) * keyword (missing = 0)."""
    v, kw = _minmax(vector), _minmax(keyword)
    return {i: vector_weight * v.get(i, 0.0) + (1 - vector_weight) * kw.get(i, 0.0) for i in set(v) | set(kw)}


# ---- Keyword Search -----------------------------------------------------------------------------
class KeywordSearchConfig(NodeConfig):
    top_k: int = Field(default=5, ge=1, le=100)
    filters: dict[str, JsonValue] = Field(
        default_factory=dict,
        description='Exact-match filters, e.g. {"filename": "handbook.pdf", "metadata.lang": "en"}.',
        json_schema_extra=ui(advanced=True),
    )


async def _keyword_hits(ctx: NodeContext, handle: VectorStoreHandle, query: str, k: int, filters: dict) -> list:
    index = open_keyword_index(ctx, handle)
    if not await store_call(index.exists):
        return []
    return await store_call(index.search, query, k, filters or None)


@define_node(
    type="agenticmind.rag.keyword_search",
    version="1.0.0",
    display_name="Keyword Search",
    category="rag",
    package=PACKAGE,
    description="BM25 keyword search over the chunks indexed by Index Documents (exact terms, names, codes).",
    inputs=[
        InputPort(name="query", type=PortType.STRING),
        InputPort(name="store", type=PortType.VECTOR_STORE_HANDLE, description="Same Vector Store as indexing"),
    ],
    outputs=[OutputPort(name="documents", type=PortType.DOCUMENT_SET)],
    config=KeywordSearchConfig,
    permissions=_STORE_PERMISSIONS,
    execution=ExecutionPolicy(timeout_seconds=60, max_attempts=2, idempotent=True),
    tags=["rag", "retrieval"],
)
async def keyword_search(ctx: NodeContext, inputs, config: KeywordSearchConfig) -> NodeResult:
    query = inputs["query"].strip()
    if not query:
        raise NodeError("QUERY_EMPTY", "The query is empty")
    handle = VectorStoreHandle.model_validate(inputs["store"])
    if not await store_call(open_keyword_index(ctx, handle).exists):
        raise NodeError(
            "KEYWORD_INDEX_NOT_FOUND",
            f"No keyword index for '{handle.collection}'. Run Index Documents (1.1.0, keyword_index on) first.",
        )
    hits = await _keyword_hits(ctx, handle, query, config.top_k, config.filters)
    docs = [_retrieved(h.document, "keyword", h.score) for h in hits]
    await ctx.emit("retrieved", {"query": query, "hits": len(docs), "method": "keyword"})
    return NodeResult(outputs={"documents": _out(docs)})


# ---- Hybrid Search ------------------------------------------------------------------------------
class HybridSearchConfig(NodeConfig):
    top_k: int = Field(default=8, ge=1, le=100, description="Results after fusion.")
    candidates: int = Field(default=20, ge=1, le=200, description="Results fetched per method and query.")
    fusion: Literal["rrf", "weighted"] = Field(
        default="rrf",
        description="rrf: reciprocal rank fusion (robust, ignores score scales). weighted: mix normalised scores.",
    )
    vector_weight: float = Field(default=0.5, ge=0, le=1, description="weighted fusion: share of the vector score.")
    rrf_k: int = Field(default=60, ge=1, le=1000, json_schema_extra=ui(advanced=True))
    filters: dict[str, JsonValue] = Field(default_factory=dict, json_schema_extra=ui(advanced=True))


@define_node(
    type="agenticmind.rag.hybrid_search",
    version="1.0.0",
    display_name="Hybrid Search",
    category="rag",
    package=PACKAGE,
    description="Combines vector (meaning) and keyword (exact terms) search, for one or several query variants.",
    inputs=[
        InputPort(name="query", type=PortType.STRING),
        InputPort(
            name="queries",
            type=PortType.JSON,
            required=False,
            description="Extra query variants (e.g. from Query Rewriter); all results are fused",
        ),
        InputPort(name="model", type=PortType.EMBEDDING_MODEL),
        InputPort(name="store", type=PortType.VECTOR_STORE_HANDLE),
    ],
    outputs=[OutputPort(name="documents", type=PortType.DOCUMENT_SET)],
    config=HybridSearchConfig,
    permissions=_STORE_PERMISSIONS,
    execution=ExecutionPolicy(timeout_seconds=120, max_attempts=2, idempotent=True),
    tags=["rag", "retrieval"],
)
async def hybrid_search(ctx: NodeContext, inputs, config: HybridSearchConfig) -> NodeResult:
    query = inputs["query"].strip()
    if not query:
        raise NodeError("QUERY_EMPTY", "The query is empty")
    queries = [query]
    for q in inputs.get("queries") or []:
        if isinstance(q, str) and q.strip() and q.strip() not in queries:
            queries.append(q.strip())
    queries = queries[:MAX_QUERIES]
    model = EmbeddingModelHandle.model_validate(inputs["model"])
    handle = VectorStoreHandle.model_validate(inputs["store"])
    store = open_vector_store(ctx, handle)
    if not await store_call(store.exists):
        raise NodeError(
            "COLLECTION_NOT_FOUND",
            f"Collection '{handle.collection}' is empty or missing. Run the ingestion part (Index Documents) first.",
        )
    keyword_available = await store_call(open_keyword_index(ctx, handle).exists)
    if not keyword_available:
        await ctx.emit("keyword_index_missing", {"collection": handle.collection, "note": "vector results only"})

    embedded = await embed_texts(ctx, model, queries, "query")
    documents: dict[str, dict[str, Any]] = {}
    vector_lists: list[list[str]] = []
    keyword_lists: list[list[str]] = []
    vector_best: dict[str, float] = {}
    keyword_best: dict[str, float] = {}
    for q, vec in zip(queries, embedded.vectors, strict=True):
        hits = await store_call(store.query, vec, config.candidates, config.filters or None)
        mismatched = {h.embedding_model for h in hits if h.embedding_model and h.embedding_model != model.model_id}
        if mismatched:
            raise NodeError(
                "EMBEDDING_MODEL_MISMATCH",
                f"The collection was indexed with {', '.join(sorted(mismatched))} but the query uses "
                f"{model.model_id}. Re-index with the same Embedding Model.",
            )
        vector_lists.append([h.id for h in hits])
        for h in hits:
            documents.setdefault(h.id, h.document)
            vector_best[h.id] = max(vector_best.get(h.id, float("-inf")), h.score)
        if keyword_available:
            khits = await _keyword_hits(ctx, handle, q, config.candidates, config.filters)
            keyword_lists.append([h.id for h in khits])
            for h in khits:
                documents.setdefault(h.id, h.document)
                keyword_best[h.id] = max(keyword_best.get(h.id, float("-inf")), h.score)

    if config.fusion == "rrf":
        fused = rrf_fuse(vector_lists + keyword_lists, k=config.rrf_k)
    else:
        fused = weighted_fuse(vector_best, keyword_best, config.vector_weight)
    ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[: config.top_k]
    docs = [_retrieved(documents[i], "hybrid", s) for i, s in ranked]

    def rank_in(lists: list[list[str]], doc_id: str) -> int | None:
        ranks = [lst.index(doc_id) + 1 for lst in lists if doc_id in lst]
        return min(ranks) if ranks else None

    await ctx.emit(
        "retrieved",
        {
            "query": query,
            "queries": len(queries),
            "method": "hybrid",
            "fusion": config.fusion,
            "vector_hits": len(vector_best),
            "keyword_hits": len(keyword_best),
            "found_by_both": len(set(vector_best) & set(keyword_best)),
            "top": [
                {
                    "id": i,
                    "score": round(s, 6),
                    "vector_rank": rank_in(vector_lists, i),
                    "keyword_rank": rank_in(keyword_lists, i),
                }
                for i, s in ranked[:5]
            ],
        },
    )
    usage = embedded.usage.model_copy(update={"output_tokens": 0}) if embedded.usage else None
    return NodeResult(outputs={"documents": _out(docs)}, usage=usage)


# ---- LLM helper ---------------------------------------------------------------------------------
class _LLMFields(NodeConfig):
    llm_provider: ChatProviderName = Field(default="openrouter", json_schema_extra=ui(advanced=True))
    llm_model: str = Field(default=DEFAULT_OPENROUTER_MODEL, json_schema_extra=ui(advanced=True))
    api_key: SecretRef | None = secret_field("API key for LLM mode, e.g. OPENROUTER_API_KEY")
    base_url: str | None = Field(default=None, json_schema_extra=ui(advanced=True))
    # generous: reasoning models spend output tokens thinking before answering
    max_tokens: int = Field(default=4000, ge=64, le=100_000, json_schema_extra=ui(advanced=True))

    def _uses_llm(self) -> bool:
        return True

    @model_validator(mode="after")
    def _check_llm(self) -> _LLMFields:
        if self._uses_llm():
            check_chat_provider(self.llm_provider, self.api_key, self.base_url)
        return self


async def _forced(
    ctx: NodeContext, config: _LLMFields, *, system: str, user: str, tool: ToolSpec, temperature: float = 0.0
) -> tuple[dict[str, Any] | None, Any]:
    request = ChatRequest(
        model=config.llm_model,
        system=system,
        messages=[ChatMessage(role="user", content=user)],
        temperature=temperature,
        max_tokens=config.max_tokens,
        tools=[tool],
        tool_choice="required",
    )
    try:
        api_key = ctx.secret(config.api_key) if config.api_key else None
        resp = await get_provider(config.llm_provider).chat(request, api_key=api_key, base_url=config.base_url)
    except ProviderError as exc:
        if exc.retryable:
            raise TransientNodeError(exc.code, str(exc)) from exc
        raise NodeError(exc.code, str(exc)) from exc
    call = next((c for c in resp.tool_calls if c.name == tool.name), None)
    if call is None and resp.finish_reason == "length":
        raise TransientNodeError(
            "MODEL_OUTPUT_TRUNCATED",
            f"The model ran out of output tokens (max_tokens={config.max_tokens}); raise max_tokens.",
        )
    return (call.arguments if call else None), resp.usage


# ---- Reranker -----------------------------------------------------------------------------------
class RerankerConfig(_LLMFields):
    provider: Literal["fastembed", "llm", "overlap"] = Field(
        default="fastembed",
        description="fastembed: local cross-encoder (default, no key; downloads ~80 MB once). llm: a chat model "
        "scores each passage. overlap: word-overlap test scorer (offline, low quality).",
    )
    model: str = Field(default=DEFAULT_RERANK_MODEL, description="Cross-encoder for fastembed.")
    candidates: int = Field(default=20, ge=1, le=200, description="How many incoming results to rescore.")
    top_k: int = Field(default=5, ge=1, le=100, description="Results to keep after reranking.")
    score_threshold: float | None = Field(
        default=None, ge=0, le=1, description="Drop results with a rerank score below this (0..1)."
    )

    def _uses_llm(self) -> bool:
        return self.provider == "llm"


@define_node(
    type="agenticmind.rag.reranker",
    version="1.0.0",
    display_name="Reranker",
    category="rag",
    package=PACKAGE,
    description="Rescores retrieved chunks against the query with a cross-encoder (or an LLM) and keeps the best.",
    inputs=[
        InputPort(name="query", type=PortType.STRING),
        InputPort(name="documents", type=PortType.DOCUMENT_SET),
    ],
    outputs=[OutputPort(name="documents", type=PortType.DOCUMENT_SET)],
    config=RerankerConfig,
    permissions=Permissions(network=True, filesystem=True, credentials=True),
    execution=ExecutionPolicy(timeout_seconds=300, max_attempts=2, idempotent=True),
    tags=["rag", "retrieval"],
)
async def reranker(ctx: NodeContext, inputs, config: RerankerConfig) -> NodeResult:
    query = inputs["query"].strip()
    docs = _parse_retrieved(inputs["documents"])[: config.candidates]
    if not docs:
        return NodeResult(outputs={"documents": []})
    usage = None
    if config.provider == "llm":
        listing = "\n\n".join(f"[{i}] {d.content[:1500]}" for i, d in enumerate(docs))
        tool = ToolSpec(
            name="submit_scores",
            description="Relevance of each passage to the query",
            parameters={
                "type": "object",
                "properties": {
                    "scores": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "index": {"type": "integer"},
                                "score": {"type": "number", "description": "0 = irrelevant, 10 = answers it"},
                            },
                            "required": ["index", "score"],
                        },
                    }
                },
                "required": ["scores"],
            },
        )
        args, usage = await _forced(
            ctx,
            config,
            system="Score how well each passage helps answer the query, from 0 to 10. Judge each passage on its "
            "own content. Score every passage exactly once.",
            user=f"Query: {query}\n\nPassages:\n{listing}",
            tool=tool,
        )
        given: dict[int, float] = {}
        for item in (args or {}).get("scores") or []:
            if (
                isinstance(item, dict)
                and isinstance(item.get("index"), int)
                and isinstance(item.get("score"), (int, float))
            ):
                given[item["index"]] = max(0.0, min(10.0, float(item["score"]))) / 10
        scores = [given.get(i, 0.0) for i in range(len(docs))]
    else:
        cache = ctx.storage_path("models") if config.provider == "fastembed" else None
        try:
            scores = await rerank_scores(
                config.provider, query, [d.content for d in docs], model=config.model, cache_dir=cache
            )
        except ProviderError as exc:
            if exc.retryable:
                raise TransientNodeError(exc.code, str(exc)) from exc
            raise NodeError(exc.code, str(exc)) from exc
    order = sorted(range(len(docs)), key=lambda i: (-scores[i], i))
    kept = [
        docs[i].model_copy(update={"rerank_score": round(scores[i], 6)})
        for i in order
        if config.score_threshold is None or scores[i] >= config.score_threshold
    ][: config.top_k]
    await ctx.emit(
        "reranked",
        {
            "provider": config.provider,
            "candidates": len(docs),
            "kept": len(kept),
            "moves": [{"id": docs[i].id, "from": i + 1, "to": n + 1} for n, i in enumerate(order[: config.top_k])],
        },
    )
    return NodeResult(outputs={"documents": _out(kept)}, usage=usage)


# ---- Query Rewriter -----------------------------------------------------------------------------
class QueryRewriterConfig(_LLMFields):
    variants: int = Field(default=3, ge=0, le=MAX_QUERIES - 1, description="Alternative phrasings to generate.")
    temperature: float = Field(default=0.3, ge=0, le=2, json_schema_extra=ui(advanced=True))


@define_node(
    type="agenticmind.rag.query_rewriter",
    version="1.0.0",
    display_name="Query Rewriter",
    category="rag",
    package=PACKAGE,
    description="Makes follow-up questions standalone (using chat history) and adds alternative phrasings.",
    inputs=[
        InputPort(name="query", type=PortType.STRING),
        InputPort(name="history", type=PortType.JSON, required=False, description="[{role, content}] earlier turns"),
    ],
    outputs=[
        OutputPort(name="query", type=PortType.STRING, description="Standalone query"),
        OutputPort(name="queries", type=PortType.JSON, description="Standalone query + variants (for Hybrid Search)"),
    ],
    config=QueryRewriterConfig,
    permissions=Permissions(network=True, credentials=True),
    execution=ExecutionPolicy(timeout_seconds=120, max_attempts=2, idempotent=True),
    tags=["rag", "retrieval"],
)
async def query_rewriter(ctx: NodeContext, inputs, config: QueryRewriterConfig) -> NodeResult:
    original = inputs["query"].strip()
    if not original:
        raise NodeError("QUERY_EMPTY", "The query is empty")
    history = [
        f"{m['role']}: {m['content']}"
        for m in (inputs.get("history") or [])[-10:]
        if isinstance(m, dict) and isinstance(m.get("content"), str) and m.get("role") in ("user", "assistant")
    ]
    system = (
        "You prepare search queries for a document search engine. Rewrite the user's latest question as a "
        "standalone search query: resolve pronouns and references using the conversation, keep names, numbers "
        "and technical terms exactly, and add nothing that was not asked."
        + (
            f" Also give {config.variants} differently worded variants (synonyms, related terms)."
            if config.variants
            else ""
        )
        + ("\n\nConversation so far:\n" + "\n".join(history) if history else "")
    )
    tool = ToolSpec(
        name="submit_queries",
        description="Submit the search queries",
        parameters={
            "type": "object",
            "properties": {
                "standalone": {"type": "string"},
                "variants": {"type": "array", "items": {"type": "string"}, "maxItems": config.variants},
            },
            "required": ["standalone", "variants"] if config.variants else ["standalone"],
        },
    )
    args, usage = await _forced(ctx, config, system=system, user=original, tool=tool, temperature=config.temperature)
    standalone = str((args or {}).get("standalone") or "").strip()
    if not standalone:  # never block retrieval on the rewriter
        await ctx.emit("rewrite_fallback", {"reason": "no rewritten query returned"})
        standalone = original
    queries = [standalone]
    for v in (args or {}).get("variants") or []:
        if isinstance(v, str) and v.strip() and v.strip() not in queries and len(queries) <= config.variants:
            queries.append(v.strip())
    await ctx.emit("rewritten", {"original": original, "standalone": standalone, "variants": queries[1:]})
    return NodeResult(outputs={"query": standalone, "queries": queries}, usage=usage)


# ---- Context Compressor -------------------------------------------------------------------------
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


def split_sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            out.extend(s.strip() for s in _SENTENCE.split(line) if s.strip())
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


class CompressorConfig(_LLMFields):
    method: Literal["extractive", "llm"] = Field(
        default="extractive",
        description="extractive: keep the sentences that share terms with the query (fast, no model). llm: a model "
        "picks the relevant sentences; only sentences that really appear in the source are kept.",
    )
    max_sentences: int = Field(default=4, ge=1, le=50, description="Sentences kept per chunk.")
    neighbors: int = Field(default=1, ge=0, le=3, description="extractive: also keep this many adjacent sentences.")
    drop_irrelevant: bool = Field(default=True, description="Remove chunks with no relevant sentence at all.")

    def _uses_llm(self) -> bool:
        return self.method == "llm"


def extract(content: str, query: str, max_sentences: int, neighbors: int) -> str:
    sentences = split_sentences(content)
    if not sentences:
        return ""
    scores = keyword_scores(query, sentences)
    best = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: -scores[i])[:max_sentences]
    keep = sorted({j for i in best for j in range(i - neighbors, i + neighbors + 1) if 0 <= j < len(sentences)})
    parts: list[str] = []
    for n, i in enumerate(keep):
        if n and i != keep[n - 1] + 1:
            parts.append("…")
        parts.append(sentences[i])
    return " ".join(parts)


@define_node(
    type="agenticmind.rag.context_compressor",
    version="1.0.0",
    display_name="Context Compressor",
    category="rag",
    package=PACKAGE,
    description="Shrinks retrieved chunks to the sentences relevant to the query, keeping IDs and sources.",
    inputs=[
        InputPort(name="query", type=PortType.STRING),
        InputPort(name="documents", type=PortType.DOCUMENT_SET),
    ],
    outputs=[
        OutputPort(name="documents", type=PortType.DOCUMENT_SET),
        OutputPort(name="report", type=PortType.JSON, description="{documents_in/out, chars_in/out, ratio}"),
    ],
    config=CompressorConfig,
    permissions=Permissions(network=True, credentials=True),
    execution=ExecutionPolicy(timeout_seconds=300, max_attempts=2, idempotent=True),
    tags=["rag", "retrieval"],
)
async def context_compressor(ctx: NodeContext, inputs, config: CompressorConfig) -> NodeResult:
    query = inputs["query"].strip()
    docs = _parse_retrieved(inputs["documents"])
    usage_in = usage_out = 0

    async def llm_extract(content: str) -> str:
        nonlocal usage_in, usage_out
        sentences = split_sentences(content)
        tool = ToolSpec(
            name="submit_sentences",
            description="The sentences from the passage that help answer the query, copied exactly",
            parameters={
                "type": "object",
                "properties": {
                    "sentences": {"type": "array", "items": {"type": "string"}, "maxItems": config.max_sentences}
                },
                "required": ["sentences"],
            },
        )
        args, usage = await _forced(
            ctx,
            config,
            system="Copy, word for word, only the sentences of the passage that help answer the query. Do not "
            "paraphrase or add anything. Return an empty list if nothing is relevant.",
            user=f"Query: {query}\n\nPassage:\n{content}",
            tool=tool,
        )
        if usage is not None:
            usage_in += usage.input_tokens
            usage_out += usage.output_tokens
        chosen = {_norm(s) for s in (args or {}).get("sentences") or [] if isinstance(s, str)}
        # faithfulness guard: keep only sentences that exist in the source, in source order
        return " ".join(s for s in sentences if _norm(s) in chosen)

    if config.method == "llm":
        semaphore = asyncio.Semaphore(4)

        async def bounded(d: RetrievedDocument) -> str:
            async with semaphore:
                return await llm_extract(d.content)

        contents = await asyncio.gather(*(bounded(d) for d in docs))
    else:
        contents = [extract(d.content, query, config.max_sentences, config.neighbors) for d in docs]

    kept: list[RetrievedDocument] = []
    for d, content in zip(docs, contents, strict=True):
        if not content:
            if config.drop_irrelevant:
                continue
            content = d.content
        # offsets no longer describe the shortened text, so they are dropped; id/source stay for citations
        kept.append(d.model_copy(update={"content": content, "offsets": None}))
    chars_in = sum(len(d.content) for d in docs)
    chars_out = sum(len(d.content) for d in kept)
    report = {
        "method": config.method,
        "documents_in": len(docs),
        "documents_out": len(kept),
        "chars_in": chars_in,
        "chars_out": chars_out,
        "ratio": round(chars_out / chars_in, 3) if chars_in else 1.0,
    }
    await ctx.emit("compressed", report)
    usage = None
    if config.method == "llm" and (usage_in or usage_out):
        from agenticmind_node_sdk import ModelUsage

        usage = ModelUsage(
            provider=config.llm_provider, model=config.llm_model, input_tokens=usage_in, output_tokens=usage_out
        )
    return NodeResult(outputs={"documents": _out(kept), "report": report}, usage=usage)
