"""Prometheus metrics computed from finished runs (ADR-0037).

Counters live in the API process and start at zero on restart (Prometheus handles counter resets).
Exposed in the text exposition format at ``/api/metrics``.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

from agenticmind_runtime import TraceEvent

_BUCKETS = (0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, float("inf"))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(labels: tuple[tuple[str, str], ...]) -> str:
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels) + "}" if labels else ""


class RunMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = defaultdict(lambda: defaultdict(float))
        self.duration_buckets = [0] * len(_BUCKETS)
        self.duration_sum = 0.0
        self.duration_count = 0

    def record_run(self, events: list[TraceEvent], *, project_id: str = "") -> None:
        events = sorted(events, key=lambda e: e.seq)
        if not events:
            return
        with self._lock:
            terminal = next((e for e in reversed(events) if e.event_type.value.startswith("run_")
                             and e.event_type.value != "run_started"), None)  # fmt: skip
            status = terminal.event_type.value.removeprefix("run_") if terminal else "unknown"
            self._inc("agenticmind_runs_total", (("status", status),))
            seconds = (events[-1].timestamp - events[0].timestamp).total_seconds()
            self.duration_sum += seconds
            self.duration_count += 1
            for i, bound in enumerate(_BUCKETS):
                if seconds <= bound:
                    self.duration_buckets[i] += 1
            for e in events:
                et = e.event_type.value
                if et == "node_failed":
                    code = e.error.code if e.error else "unknown"
                    self._inc("agenticmind_node_failures_total", (("node_type", e.node_type or ""), ("code", code)))
                if et == "node_succeeded" and e.usage is not None and (e.usage.input_tokens or e.usage.output_tokens):
                    labels = (("provider", e.usage.provider), ("model", e.usage.model))
                    self._inc("agenticmind_model_tokens_total", (*labels, ("direction", "input")), e.usage.input_tokens)
                    self._inc(
                        "agenticmind_model_tokens_total", (*labels, ("direction", "output")), e.usage.output_tokens
                    )
                    self._inc("agenticmind_model_calls_total", labels)
                    if e.usage.estimated_cost_usd:
                        self._inc("agenticmind_model_cost_usd_total", labels, e.usage.estimated_cost_usd)
                if et == "node_event" and e.data.get("type") == "tool_call":
                    ok = "true" if e.data.get("ok") else "false"
                    self._inc("agenticmind_tool_calls_total", (("tool", str(e.data.get("tool", ""))), ("ok", ok)))

    def _inc(self, name: str, labels: tuple[tuple[str, str], ...], value: float = 1.0) -> None:
        self.counters[name][labels] += value

    def render(self, extra: dict[str, Any] | None = None) -> str:
        help_text = {
            "agenticmind_runs_total": "Finished runs by status.",
            "agenticmind_node_failures_total": "Failed node invocations by node type and error code.",
            "agenticmind_model_tokens_total": "Model tokens by provider, model and direction.",
            "agenticmind_model_calls_total": "Model-using node invocations by provider and model.",
            "agenticmind_model_cost_usd_total": "Estimated model cost in USD (where the provider reports it).",
            "agenticmind_tool_calls_total": "Agent tool calls by tool and outcome.",
        }
        lines: list[str] = []
        with self._lock:
            for name, desc in help_text.items():
                lines += [f"# HELP {name} {desc}", f"# TYPE {name} counter"]
                for labels, value in sorted(self.counters.get(name, {}).items()):
                    lines.append(f"{name}{_labels(labels)} {value:g}")
            lines += ["# HELP agenticmind_run_duration_seconds Run wall-clock duration.",
                      "# TYPE agenticmind_run_duration_seconds histogram"]  # fmt: skip
            for bound, count in zip(_BUCKETS, self.duration_buckets, strict=True):
                le = "+Inf" if bound == float("inf") else f"{bound:g}"
                lines.append(f'agenticmind_run_duration_seconds_bucket{{le="{le}"}} {count}')
            lines += [f"agenticmind_run_duration_seconds_sum {self.duration_sum:g}",
                      f"agenticmind_run_duration_seconds_count {self.duration_count}"]  # fmt: skip
        for name, value in (extra or {}).items():
            lines += [f"# TYPE {name} gauge", f"{name} {value:g}"]
        return "\n".join(lines) + "\n"
