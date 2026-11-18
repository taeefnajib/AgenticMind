"""LLM-as-judge. An estimate, not objective truth: every grade carries judge model, prompt version and rubric."""

from __future__ import annotations

from typing import Any

from agenticmind_providers import ChatMessage, ChatRequest, ProviderError, ToolSpec, get_provider

from .models import JudgeConfig, TestCase

RUBRIC_CORRECTNESS = (
    "5 = says the same as the reference (wording may differ); 4 = correct but misses a minor detail; "
    "3 = partly correct or incomplete; 2 = mostly wrong; 1 = wrong, contradicts the reference, or no answer."
)
RUBRIC_GROUNDEDNESS = (
    "5 = every factual claim is supported by the passages; 4 = one minor unsupported detail; "
    "3 = some claims unsupported; 2 = mostly unsupported; 1 = contradicts the passages or is unrelated."
)
PROMPTS = {
    "correctness": (
        "correctness-v1",
        "You grade answers against a reference answer. Judge meaning, not wording. Ignore citation markers like [1].",
    ),
    "groundedness": (
        "groundedness-v1",
        "You check whether an answer is supported by the given passages only. Do not use outside knowledge; "
        "a claim that is true but not in the passages is unsupported.",
    ),
}
GRADE_TOOL = ToolSpec(
    name="submit_grade",
    description="Submit the grade",
    parameters={
        "type": "object",
        "properties": {
            "score": {"type": "integer", "minimum": 1, "maximum": 5},
            "reasoning": {"type": "string", "description": "One or two sentences"},
        },
        "required": ["score", "reasoning"],
    },
)


class JudgeError(Exception):
    pass


def _question(case: TestCase) -> str:
    if len(case.inputs) == 1:
        return str(next(iter(case.inputs.values())))
    return "\n".join(f"{k}: {v}" for k, v in case.inputs.items())


class Judge:
    def __init__(self, config: JudgeConfig, api_key: str | None) -> None:
        self.config = config
        self.api_key = api_key
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.config.provider,
            "model": self.config.model,
            "prompts": {k: v[0] for k, v in PROMPTS.items()},
            "rubrics": {"correctness": RUBRIC_CORRECTNESS, "groundedness": RUBRIC_GROUNDEDNESS},
            "note": "LLM-as-judge scores are estimates, not ground truth.",
        }

    async def _grade(self, kind: str, rubric: str, user: str) -> dict[str, Any]:
        version, system = PROMPTS[kind]
        request = ChatRequest(
            model=self.config.model,
            system=f"{system}\n\nRubric: {rubric}\nCall submit_grade exactly once.",
            messages=[ChatMessage(role="user", content=user)],
            temperature=0,
            max_tokens=self.config.max_tokens,
            tools=[GRADE_TOOL],
            tool_choice="required",
        )
        try:
            resp = await get_provider(self.config.provider).chat(
                request, api_key=self.api_key, base_url=self.config.base_url
            )
        except ProviderError as exc:
            raise JudgeError(f"{exc.code}: {exc}") from exc
        self.input_tokens += resp.usage.input_tokens
        self.output_tokens += resp.usage.output_tokens
        self.cost_usd += resp.usage.estimated_cost_usd or 0.0
        call = next((c for c in resp.tool_calls if c.name == GRADE_TOOL.name), None)
        if call is None:
            raise JudgeError(f"The judge returned no grade (finish reason: {resp.finish_reason})")
        try:
            score = min(5, max(1, int(call.arguments.get("score", 1))))
        except (TypeError, ValueError) as exc:
            raise JudgeError("The judge returned a non-numeric score") from exc
        return {
            "score": score,
            "reasoning": str(call.arguments.get("reasoning", ""))[:1000],
            "judge": {"model": self.config.model, "prompt": version, "rubric": rubric},
        }

    async def correctness(self, case: TestCase, answer: str) -> dict[str, Any]:
        user = f"Question:\n{_question(case)}\n\nReference answer:\n{case.reference}\n\nAnswer to grade:\n{answer}"
        return await self._grade("correctness", RUBRIC_CORRECTNESS, user)

    async def groundedness(self, case: TestCase, answer: str, passages: list[dict[str, Any]]) -> dict[str, Any]:
        listing = "\n\n".join(f"[{i + 1}] {p.get('content', '')[:2000]}" for i, p in enumerate(passages))
        user = f"Question:\n{_question(case)}\n\nPassages:\n{listing}\n\nAnswer to check:\n{answer}"
        return await self._grade("groundedness", RUBRIC_GROUNDEDNESS, user)
