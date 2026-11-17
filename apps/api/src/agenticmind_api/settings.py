from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from agenticmind_registry import TrustLevel


def _trust_from_env() -> frozenset[TrustLevel]:
    raw = os.environ.get("AGENTICMIND_IN_PROCESS_TRUST", "core")
    return frozenset(TrustLevel(x.strip()) for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "AGENTICMIND_DATABASE_URL", "sqlite:///./.agenticmind/agenticmind.db"
        )
    )
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip()
            for o in os.environ.get(
                "AGENTICMIND_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
            ).split(",")
            if o.strip()
        )
    )
    in_process_trust: frozenset[TrustLevel] = field(default_factory=_trust_from_env)
    #: Prefix for environment variables holding secret values (``{prefix}NAME``).
    secret_env_prefix: str = field(default_factory=lambda: os.environ.get("AGENTICMIND_SECRET_PREFIX", ""))
    max_concurrency: int = field(default_factory=lambda: int(os.environ.get("AGENTICMIND_MAX_CONCURRENCY", "8")))
    #: Optional directory with the built web app, served at ``/`` (single-container deployments).
    web_dist: Path | None = field(
        default_factory=lambda: Path(p) if (p := os.environ.get("AGENTICMIND_WEB_DIST")) else None
    )
