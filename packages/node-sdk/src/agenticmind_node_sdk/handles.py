"""Handles: small, serialisable descriptors of shared resources passed along data edges.

A handle carries configuration (and secret *references*, never values) so that several nodes use
exactly the same resource: e.g. the Index Documents and Retriever nodes share one embedding model and
one vector store collection. A node consuming a handle may resolve the secret references it carries,
provided it declares the ``credentials`` permission (the runtime delegates them along the edge).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import SecretRef

#: Port types whose values are handles (used by the runtime for secret delegation).
HANDLE_PORT_TYPES = frozenset({"EmbeddingModel", "VectorStoreHandle", "Tool", "Database", "Ontology"})


class _Handle(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class EmbeddingModelHandle(_Handle):
    kind: Literal["embedding_model"] = "embedding_model"
    provider: str
    model: str
    dimensions: int | None = Field(default=None, ge=1)
    base_url: str | None = None
    api_key: SecretRef | None = None
    batch_size: int = Field(default=32, ge=1, le=2048)

    @property
    def model_id(self) -> str:
        """Identity recorded with every stored vector, used to detect model mismatches."""
        suffix = f"@{self.dimensions}" if self.dimensions else ""
        return f"{self.provider}:{self.model}{suffix}"


class VectorStoreHandle(_Handle):
    kind: Literal["vector_store"] = "vector_store"
    backend: str
    collection: str
    distance: Literal["cosine", "dot"] = "cosine"
    url: str | None = None
    api_key: SecretRef | None = None
    dsn: SecretRef | None = None
    cloud: str | None = None
    region: str | None = None
    namespace: str | None = None


#: Database engines the connector supports (ADR-0018, ADR-0026).
DatabaseEngine = Literal[
    "sqlite", "postgresql", "mysql", "sqlserver", "snowflake", "databricks", "duckdb", "clickhouse", "bigquery"
]


class DatabaseHandle(_Handle):
    """A read-only connection description. Credentials are secret *references* only.

    ``engine`` picks the driver; connection details are either a ``dsn`` secret (PostgreSQL, MySQL, SQL
    Server, SQLite path) or structured fields (``host``/``port``/``database``/``user`` plus ``password`` or
    ``token`` secrets, and engine-specific ones such as Snowflake ``account``/``warehouse`` or Databricks
    ``warehouse_id``). ``schemas`` limits which schemas/tables queries may touch; ``max_rows`` and
    ``timeout_seconds`` bound every query. Writes are refused by the SQL policy and, where the engine offers
    it, by a read-only session as well.
    """

    kind: Literal["database"] = "database"
    engine: DatabaseEngine
    artifact_id: str | None = None
    dsn: SecretRef | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = None
    user: str | None = None
    password: SecretRef | None = None
    token: SecretRef | None = None
    private_key: SecretRef | None = None
    #: BigQuery: a service-account JSON key, kept in a secret.
    service_account: SecretRef | None = None
    ssl: Literal["require", "prefer", "disable"] = "require"
    account: str | None = None
    warehouse: str | None = None
    role: str | None = None
    catalog: str | None = None
    default_schema: str | None = None
    project: str | None = None
    location: str | None = None
    schemas: list[str] = Field(default_factory=list)
    max_rows: int = Field(default=500, ge=1, le=100_000)
    timeout_seconds: float = Field(default=15, gt=0, le=300)


TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


class OntologyHandle(_Handle):
    """A connection to an NVIDIA Auto Ontology deployment's public API (ADR-0019).

    ``api_token`` is a secret reference sent as ``x-api-key``; it never travels as a value.
    """

    kind: Literal["ontology"] = "ontology"
    base_url: str = Field(min_length=1, description="The gateway URL (APP_URL), e.g. http://localhost:3000")
    api_token: SecretRef | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=600, description="Per request (questions: see node)")
    target_db: str | None = None


class ToolHandle(_Handle):
    """A tool an agent may call.

    ``tool_type`` selects the implementation; ``parameters`` is the JSON Schema the model must follow
    (arguments are validated against it before execution); ``binding`` holds the tool's own config
    and any handles it needs (e.g. an embedding model and vector store). ``permissions`` lists what
    executing it requires; the calling node must declare them all.
    """

    kind: Literal["tool"] = "tool"
    tool_type: str
    name: str = Field(pattern=TOOL_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=2000)
    parameters: dict[str, Any]
    binding: dict[str, Any] = Field(default_factory=dict)
    permissions: list[str] = Field(default_factory=list)


def handle_value(handle: _Handle) -> dict[str, Any]:
    return handle.model_dump(mode="json", by_alias=True, exclude_none=True)


def is_handle(value: Any, kind: str) -> bool:
    return isinstance(value, dict) and value.get("kind") == kind
