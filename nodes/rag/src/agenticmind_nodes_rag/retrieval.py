"""Embedding Model, Vector Store, Index Documents and Retriever nodes.

Embedding Model and Vector Store produce *handles*. Index Documents (ingestion graph) and Retriever
(query graph) consume the same handles, so both always use the same model and collection.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
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
    secret_field,
    ui,
)
from agenticmind_node_sdk.documents import parse_documents, to_value
from agenticmind_node_sdk.handles import handle_value
from agenticmind_providers import ProviderError
from agenticmind_providers.embeddings import (
    DEFAULT_FASTEMBED_MODEL,
    DEFAULT_OPENROUTER_URL,
    EmbeddingResult,
    get_embedding_provider,
)
from agenticmind_vectorstores import (
    StoreRecord,
    StoreSpec,
    VectorStore,
    VectorStoreError,
    filter_fields,
    open_store,
)
from pydantic import Field, JsonValue, model_validator

PACKAGE = "agenticmind-nodes-rag"


# -- helpers ----------------------------------------------------------------------------------------
def _secret(ctx: NodeContext, ref: SecretRef | None) -> str | None:
    return ctx.secret(ref) if ref is not None else None


async def embed_texts(
    ctx: NodeContext, model: EmbeddingModelHandle, texts: list[str], kind: Literal["document", "query"]
) -> EmbeddingResult:
    provider = get_embedding_provider(model.provider)
    cache_dir = ctx.storage_path("models") if model.provider == "fastembed" else None
    if cache_dir is not None and not any(cache_dir.glob("models--*")):
        await ctx.emit(
            "model_download",
            {"model": model.model, "note": "First use: downloading the local embedding model (one time, ~65 MB)"},
        )
    try:
        return await provider.embed(
            texts,
            model=model.model,
            kind=kind,
            dimensions=model.dimensions,
            batch_size=model.batch_size,
            api_key=_secret(ctx, model.api_key),
            base_url=model.base_url,
            cache_dir=cache_dir,
        )
    except ProviderError as exc:
        if exc.retryable:
            raise TransientNodeError(exc.code, str(exc)) from exc
        raise NodeError(exc.code, str(exc)) from exc


def open_vector_store(ctx: NodeContext, handle: VectorStoreHandle) -> VectorStore:
    local_path = None
    if handle.backend in ("qdrant", "chroma") and not handle.url:
        local_path = ctx.storage_path("vectors", handle.backend)
    spec = StoreSpec(
        backend=handle.backend,
        collection=handle.collection,
        distance=handle.distance,
        url=handle.url,
        api_key=_secret(ctx, handle.api_key),
        dsn=_secret(ctx, handle.dsn),
        cloud=handle.cloud,
        region=handle.region,
        namespace=handle.namespace,
        local_path=local_path,
    )
    try:
        return open_store(spec)
    except VectorStoreError as exc:
        raise NodeError(exc.code, str(exc)) from exc


async def store_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except VectorStoreError as exc:
        if exc.retryable:
            raise TransientNodeError(exc.code, str(exc)) from exc
        raise NodeError(exc.code, str(exc)) from exc


def content_hash(content: str, model_id: str) -> str:
    return hashlib.sha256(f"{model_id}\n{content}".encode()).hexdigest()


# -- Embedding Model --------------------------------------------------------------------------------
class EmbeddingModelConfig(NodeConfig):
    provider: Literal["fastembed", "openai_compatible", "hash"] = Field(
        default="fastembed",
        description="fastembed: local open-source model (default, no key; first use downloads it once). "
        "openai_compatible: an embedding API such as OpenRouter or OpenAI. hash: tiny offline test embedder "
        "(low quality).",
    )
    model: str = Field(
        default=DEFAULT_FASTEMBED_MODEL,
        min_length=1,
        description="fastembed e.g. BAAI/bge-small-en-v1.5 · OpenRouter e.g. google/gemini-embedding-001",
    )
    base_url: str | None = Field(
        default=None,
        description=f"API base URL for openai_compatible (default {DEFAULT_OPENROUTER_URL}).",
        json_schema_extra=ui(advanced=True, placeholder=DEFAULT_OPENROUTER_URL),
    )
    api_key: SecretRef | None = secret_field("API key for openai_compatible, e.g. OPENROUTER_API_KEY")
    dimensions: int | None = Field(
        default=None,
        ge=8,
        le=8192,
        description="Output size (hash embedder, or APIs that support shortening). Leave empty for the model default.",
        json_schema_extra=ui(advanced=True),
    )
    batch_size: int = Field(default=32, ge=1, le=2048, json_schema_extra=ui(advanced=True))

    @model_validator(mode="after")
    def _check(self) -> EmbeddingModelConfig:
        if self.provider == "openai_compatible" and self.api_key is None and not self.base_url:
            raise ValueError("openai_compatible against OpenRouter needs an api_key secret reference")
        return self


@define_node(
    type="agenticmind.rag.embedding_model",
    version="1.0.0",
    display_name="Embedding Model",
    category="rag",
    package=PACKAGE,
    description="Chooses the embedding model. Connect it to both Index Documents and Retriever.",
    outputs=[OutputPort(name="model", type=PortType.EMBEDDING_MODEL)],
    config=EmbeddingModelConfig,
    permissions=Permissions(credentials=True),
    tags=["rag", "models"],
)
async def embedding_model(ctx: NodeContext, inputs, config: EmbeddingModelConfig) -> NodeResult:
    model = config.model
    if config.provider == "hash":
        model = "hash"
    handle = EmbeddingModelHandle(
        provider=config.provider,
        model=model,
        dimensions=config.dimensions if config.provider != "fastembed" else None,
        base_url=config.base_url if config.provider == "openai_compatible" else None,
        api_key=config.api_key,
        batch_size=config.batch_size,
    )
    return NodeResult(outputs={"model": handle_value(handle)})


# -- Vector Store ---------------------------------------------------------------------------------
class VectorStoreConfig(NodeConfig):
    backend: Literal["qdrant", "chroma", "pgvector", "pinecone"] = Field(
        default="qdrant",
        description="qdrant / chroma run embedded locally when no URL is set. pgvector needs a dsn secret, "
        "pinecone an api_key secret.",
    )
    collection: str = Field(
        default="documents",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$",
        description="Collection (index/table) name.",
    )
    namespace_by_project: bool = Field(
        default=True,
        description="Prefix the collection with the project ID so projects never share data.",
        json_schema_extra=ui(advanced=True),
    )
    distance: Literal["cosine", "dot"] = Field(default="cosine", json_schema_extra=ui(advanced=True))
    url: str | None = Field(
        default=None,
        description="Server URL (Qdrant/Chroma server or cloud). Empty = embedded local storage.",
        json_schema_extra=ui(advanced=True, placeholder="http://localhost:6333"),
    )
    api_key: SecretRef | None = secret_field("API key (Pinecone, Qdrant Cloud, Chroma Cloud)")
    dsn: SecretRef | None = secret_field("PostgreSQL connection string for pgvector")
    cloud: str | None = Field(
        default=None, description="Pinecone cloud (default aws)", json_schema_extra=ui(advanced=True)
    )
    region: str | None = Field(
        default=None, description="Pinecone region (default us-east-1)", json_schema_extra=ui(advanced=True)
    )
    namespace: str | None = Field(default=None, description="Pinecone namespace", json_schema_extra=ui(advanced=True))

    @model_validator(mode="after")
    def _check(self) -> VectorStoreConfig:
        if self.backend == "pgvector" and self.dsn is None:
            raise ValueError("pgvector needs a dsn secret reference")
        if self.backend == "pinecone" and self.api_key is None:
            raise ValueError("pinecone needs an api_key secret reference")
        return self


@define_node(
    type="agenticmind.rag.vector_store",
    version="1.0.0",
    display_name="Vector Store",
    category="rag",
    package=PACKAGE,
    description="Selects a vector database collection. Connect it to both Index Documents and Retriever.",
    outputs=[OutputPort(name="store", type=PortType.VECTOR_STORE_HANDLE)],
    config=VectorStoreConfig,
    permissions=Permissions(credentials=True),
    tags=["rag"],
)
async def vector_store(ctx: NodeContext, inputs, config: VectorStoreConfig) -> NodeResult:
    collection = f"{ctx.project_id}__{config.collection}" if config.namespace_by_project else config.collection
    handle = VectorStoreHandle(
        backend=config.backend,
        collection=collection,
        distance=config.distance,
        url=config.url,
        api_key=config.api_key,
        dsn=config.dsn,
        cloud=config.cloud,
        region=config.region,
        namespace=config.namespace,
    )
    return NodeResult(outputs={"store": handle_value(handle)})


# -- Index Documents ------------------------------------------------------------------------------
_STORE_PERMISSIONS = Permissions(network=True, filesystem=True, credentials=True, database=True)


class IndexConfig(NodeConfig):
    skip_unchanged: bool = Field(
        default=True, description="Only embed chunks that are new or changed since the last indexing run."
    )
    recreate: bool = Field(
        default=False,
        description="Delete the collection first (needed after switching to a model with other dimensions).",
        json_schema_extra=ui(advanced=True),
    )
    upsert_batch_size: int = Field(default=128, ge=1, le=5000, json_schema_extra=ui(advanced=True))


@define_node(
    type="agenticmind.rag.index_documents",
    version="1.0.0",
    display_name="Index Documents",
    category="rag",
    package=PACKAGE,
    description="Embeds chunks and upserts them into the vector store (unchanged chunks are skipped).",
    inputs=[
        InputPort(name="documents", type=PortType.DOCUMENT_SET),
        InputPort(name="model", type=PortType.EMBEDDING_MODEL),
        InputPort(name="store", type=PortType.VECTOR_STORE_HANDLE),
    ],
    outputs=[OutputPort(name="report", type=PortType.JSON)],
    config=IndexConfig,
    permissions=_STORE_PERMISSIONS,
    execution=ExecutionPolicy(timeout_seconds=1800, max_attempts=2, idempotent=True),
    tags=["rag", "ingestion"],
)
async def index_documents(ctx: NodeContext, inputs, config: IndexConfig) -> NodeResult:
    t0 = time.perf_counter()
    docs = parse_documents(inputs["documents"])
    model = EmbeddingModelHandle.model_validate(inputs["model"])
    handle = VectorStoreHandle.model_validate(inputs["store"])
    store = open_vector_store(ctx, handle)
    model_id = model.model_id

    if config.recreate:
        await store_call(store.drop)
    if len({d.id for d in docs}) != len(docs):
        raise NodeError("DUPLICATE_DOCUMENT_IDS", "Documents must have unique IDs to be indexed")

    hashes = {d.id: content_hash(d.content, model_id) for d in docs}
    todo = docs
    if config.skip_unchanged and docs and await store_call(store.exists):
        existing = await store_call(store.existing, [d.id for d in docs])
        todo = [d for d in docs if existing.get(d.id) != (hashes[d.id], model_id)]

    embedded = 0
    dimensions = None
    tokens = 0
    for start in range(0, len(todo), config.upsert_batch_size):
        batch = todo[start : start + config.upsert_batch_size]
        result = await embed_texts(ctx, model, [d.content for d in batch], "document")
        dimensions = result.dimensions
        if result.usage:
            tokens += result.usage.input_tokens
        await store_call(store.ensure_collection, result.dimensions)
        records = []
        for d, vec in zip(batch, result.vectors, strict=True):
            value = to_value(d)
            records.append(
                StoreRecord(
                    id=d.id,
                    vector=vec,
                    document=value,
                    content_hash=hashes[d.id],
                    embedding_model=model_id,
                    fields=filter_fields(value),
                )
            )
        await store_call(store.upsert, records)
        embedded += len(batch)
        await ctx.emit("index_progress", {"embedded": embedded, "total": len(todo)})

    report = {
        "backend": handle.backend,
        "collection": handle.collection,
        "embedding_model": model_id,
        "dimensions": dimensions,
        "documents": len(docs),
        "embedded": embedded,
        "skipped_unchanged": len(docs) - len(todo),
        "collection_size": await store_call(store.count) if docs else 0,
        "seconds": round(time.perf_counter() - t0, 3),
    }
    usage = None
    if embedded:
        from agenticmind_node_sdk import ModelUsage

        usage = ModelUsage(provider=model.provider, model=model.model, input_tokens=tokens)
    return NodeResult(outputs={"report": report}, usage=usage)


# -- Retriever ------------------------------------------------------------------------------------
class RetrieverConfig(NodeConfig):
    top_k: int = Field(default=5, ge=1, le=100, description="Number of chunks to return.")
    score_threshold: float | None = Field(
        default=None, ge=-1, le=1, description="Drop results scoring below this (cosine similarity)."
    )
    filters: dict[str, JsonValue] = Field(
        default_factory=dict,
        description='Exact-match filters, e.g. {"filename": "handbook.pdf", "page": 3, "metadata.lang": "en"}.',
        json_schema_extra=ui(advanced=True),
    )


@define_node(
    type="agenticmind.rag.retriever",
    version="1.0.0",
    display_name="Retriever",
    category="rag",
    package=PACKAGE,
    description="Embeds the query and returns the most similar chunks with scores.",
    inputs=[
        InputPort(name="query", type=PortType.STRING),
        InputPort(name="model", type=PortType.EMBEDDING_MODEL),
        InputPort(name="store", type=PortType.VECTOR_STORE_HANDLE),
    ],
    outputs=[OutputPort(name="documents", type=PortType.DOCUMENT_SET)],
    config=RetrieverConfig,
    permissions=_STORE_PERMISSIONS,
    execution=ExecutionPolicy(timeout_seconds=120, max_attempts=2, idempotent=True),
    tags=["rag"],
)
async def retriever(ctx: NodeContext, inputs, config: RetrieverConfig) -> NodeResult:
    query = inputs["query"].strip()
    if not query:
        raise NodeError("QUERY_EMPTY", "The query is empty")
    model = EmbeddingModelHandle.model_validate(inputs["model"])
    handle = VectorStoreHandle.model_validate(inputs["store"])
    store = open_vector_store(ctx, handle)
    if not await store_call(store.exists):
        raise NodeError(
            "COLLECTION_NOT_FOUND",
            f"Collection '{handle.collection}' is empty or missing. Run the ingestion part (Index Documents) first.",
        )
    result = await embed_texts(ctx, model, [query], "query")
    hits = await store_call(store.query, result.vectors[0], config.top_k, config.filters or None)
    mismatched = {h.embedding_model for h in hits if h.embedding_model and h.embedding_model != model.model_id}
    if mismatched:
        raise NodeError(
            "EMBEDDING_MODEL_MISMATCH",
            f"The collection was indexed with {', '.join(sorted(mismatched))} but the query uses {model.model_id}. "
            "Re-index with the same Embedding Model.",
        )
    docs = []
    for h in hits:
        if config.score_threshold is not None and h.score < config.score_threshold:
            continue
        base = {k: v for k, v in h.document.items() if k in RetrievedDocument.model_fields}
        docs.append(
            RetrievedDocument.model_validate({**base, "retrieval_method": "vector", "score": round(h.score, 6)})
        )
    await ctx.emit("retrieved", {"query": query, "hits": len(docs), "top_score": docs[0].score if docs else None})
    usage = result.usage.model_copy(update={"output_tokens": 0}) if result.usage else None
    return NodeResult(outputs={"documents": [d.model_dump(mode="json", exclude_none=True) for d in docs]}, usage=usage)
