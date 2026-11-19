"""Which distributions may run in the platform process (ADR-0004, ADR-0021).

Everything else is third-party: it is installed as a plugin and runs in an isolated worker process.
An operator can opt in to in-process execution for other trust levels with
``AGENTICMIND_IN_PROCESS_TRUST`` (e.g. ``core,community``), which also re-enables loading node and
tool entry points from non-core distributions installed in the host environment.
"""

from __future__ import annotations

import os

CORE_DISTRIBUTIONS = frozenset(
    {
        "agenticmind-nodes-core",
        "agenticmind-nodes-data",
        "agenticmind-nodes-rag",
        "agenticmind-nodes-agents",
        "agenticmind-nodes-semantic",
    }
)


def in_process_trust_from_env() -> frozenset[str]:
    raw = os.environ.get("AGENTICMIND_IN_PROCESS_TRUST", "core")
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


def may_load_in_process(distribution: str) -> bool:
    """Whether code from ``distribution``'s entry points may be imported into this process."""
    if distribution in CORE_DISTRIBUTIONS:
        return True
    return bool(in_process_trust_from_env() - {"core"})
