"""AgenticMind Evaluation Lab: datasets, versioned evaluators, LLM judges and run comparison (ADR-0017)."""

from .evaluators import REGISTRY, EvaluatorSpec, catalogue
from .judge import Judge, JudgeError
from .models import (
    CaseResult,
    DatasetContent,
    EvaluationConfig,
    ExpectedSource,
    JudgeConfig,
    MetricResult,
    Observation,
    TestCase,
)
from .runner import EvaluationError, answer_output, compare, run_evaluation, summarize

__all__ = [
    "REGISTRY",
    "CaseResult",
    "DatasetContent",
    "EvaluationConfig",
    "EvaluationError",
    "EvaluatorSpec",
    "ExpectedSource",
    "Judge",
    "JudgeConfig",
    "JudgeError",
    "MetricResult",
    "Observation",
    "TestCase",
    "answer_output",
    "catalogue",
    "compare",
    "run_evaluation",
    "summarize",
]
