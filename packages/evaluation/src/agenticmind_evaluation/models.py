"""Evaluation entities (blueprint §10): datasets, test cases, observations, results and summaries."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

_ID = r"^[A-Za-z0-9_.:-]{1,64}$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpectedSource(_Model):
    """A source a good answer should retrieve/cite: a chunk ``id``, or a ``filename`` (+ optional ``section`` text)."""

    id: str | None = None
    filename: str | None = None
    section: str | None = Field(default=None, description="Case-insensitive substring of the section path")

    @classmethod
    def parse(cls, value: Any) -> ExpectedSource:
        """Accepts ``{"filename": ..., "section": ...}``, ``"file.md#Section"``, ``"file.md"`` or a chunk id."""
        if isinstance(value, dict):
            return cls.model_validate(value)
        text = str(value).strip()
        if text.startswith("art_"):  # chunk ids look like art_<file>#c<n>
            return cls(id=text)
        if "#" in text:
            filename, section = text.split("#", 1)
            return cls(filename=filename.strip() or None, section=section.strip() or None)
        if "." in text:
            return cls(filename=text)
        return cls(id=text)

    def matches(self, doc: dict[str, Any]) -> bool:
        if self.id and doc.get("id") != self.id:
            return False
        source = doc.get("source") if isinstance(doc.get("source"), dict) else doc
        if self.filename and (source.get("filename") or "").lower() != self.filename.lower():
            return False
        if self.section and self.section.lower() not in (source.get("section") or "").lower():
            return False
        return bool(self.id or self.filename or self.section)

    def label(self) -> str:
        if self.id:
            return self.id
        return f"{self.filename or '*'}{'#' + self.section if self.section else ''}"


class TestCase(_Model):
    __test__ = False  # not a pytest class

    id: str = Field(pattern=_ID)
    inputs: dict[str, JsonValue] = Field(default_factory=dict, description="Run inputs (Manual Input keys)")
    reference: str | None = Field(default=None, description="Reference answer (for correctness metrics)")
    expected_sources: list[ExpectedSource] = Field(default_factory=list)
    must_include: list[str] = Field(default_factory=list, description="Phrases a correct answer contains")
    must_not_include: list[str] = Field(default_factory=list, description="Phrases an answer must never contain")
    expected_tools: list[str] = Field(default_factory=list, description="Tools an agent should use")
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("expected_sources", mode="before")
    @classmethod
    def _sources(cls, value: Any) -> Any:
        return [ExpectedSource.parse(v) if not isinstance(v, ExpectedSource) else v for v in value or []]


class DatasetContent(_Model):
    cases: list[TestCase] = Field(default_factory=list, max_length=2000)

    @field_validator("cases")
    @classmethod
    def _unique(cls, cases: list[TestCase]) -> list[TestCase]:
        ids = [c.id for c in cases]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate case ids: {', '.join(dupes)}")
        return cases

    def content_hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json", exclude_defaults=True), sort_keys=True, ensure_ascii=False)
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


class JudgeConfig(_Model):
    provider: Literal["openrouter", "openai_compatible", "mock"] = "openrouter"
    model: str = "google/gemini-3.8-flash"
    api_key: dict[str, str] | None = Field(
        default=None, description='Secret reference, e.g. {"$secret": "OPENROUTER_API_KEY"}'
    )
    base_url: str | None = None
    max_tokens: int = Field(default=2000, ge=64, le=100_000)


class EvaluationConfig(_Model):
    evaluators: list[str] = Field(min_length=1, description="Evaluator names (see the catalogue)")
    answer_output: str | None = Field(default=None, description="Output node id or key holding the answer")
    retrieval_node: str | None = Field(default=None, description="Node whose 'documents' are scored (auto)")
    k: int = Field(default=5, ge=1, le=100)
    concurrency: int = Field(default=2, ge=1, le=16)
    judge: JudgeConfig | None = None


class Observation(_Model):
    """What evaluators see for one executed case (extracted from the run, nothing else)."""

    run_id: str
    status: str
    error: dict[str, Any] | None = None
    answer: str | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    invalid_citations: list[int] = Field(default_factory=list)
    retrieved: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_node: str | None = None
    tools_used: list[str] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    retries: int = 0


class MetricResult(_Model):
    name: str
    version: str
    value: float | None = Field(description="None = not applicable to this case (excluded from the mean)")
    passed: bool | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class CaseResult(_Model):
    case_id: str
    run_id: str
    status: str
    error: dict[str, Any] | None = None
    metrics: list[MetricResult] = Field(default_factory=list)
    observation: Observation

    def metric(self, name: str) -> MetricResult | None:
        return next((m for m in self.metrics if m.name == name), None)
