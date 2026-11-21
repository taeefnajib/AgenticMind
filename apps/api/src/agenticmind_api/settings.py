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
    #: Root for local data (uploaded files etc.).
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("AGENTICMIND_DATA_DIR", ".agenticmind")))
    max_upload_bytes: int = field(
        default_factory=lambda: int(os.environ.get("AGENTICMIND_MAX_UPLOAD_MB", "50")) * 1024 * 1024
    )
    database_url: str = field(
        default_factory=lambda: os.environ.get("AGENTICMIND_DATABASE_URL", "sqlite:///./.agenticmind/agenticmind.db")
    )
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip()
            for o in os.environ.get("AGENTICMIND_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(
                ","
            )
            if o.strip()
        )
    )
    in_process_trust: frozenset[TrustLevel] = field(default_factory=_trust_from_env)
    #: Prefix for environment variables holding secret values (``{prefix}NAME``).
    secret_env_prefix: str = field(default_factory=lambda: os.environ.get("AGENTICMIND_SECRET_PREFIX", ""))
    max_concurrency: int = field(default_factory=lambda: int(os.environ.get("AGENTICMIND_MAX_CONCURRENCY", "8")))
    #: Let the API run Docker to deploy projects on this machine (ADR-0020). Off by default: the API has no
    #: authentication yet, and building/running containers is a privilege.
    local_deploy: bool = field(
        default_factory=lambda: os.environ.get("AGENTICMIND_LOCAL_DEPLOY", "0").lower() in ("1", "true", "yes")
    )
    #: AI workflow designer (ADR-0022): provider, model and the secret holding its API key.
    assistant_provider: str = field(
        default_factory=lambda: os.environ.get("AGENTICMIND_ASSISTANT_PROVIDER", "openrouter")
    )
    assistant_model: str = field(
        default_factory=lambda: os.environ.get("AGENTICMIND_ASSISTANT_MODEL", "google/gemini-3.8-flash")
    )
    assistant_api_key_secret: str = field(
        default_factory=lambda: os.environ.get("AGENTICMIND_ASSISTANT_API_KEY_SECRET", "OPENROUTER_API_KEY")
    )
    assistant_base_url: str | None = field(
        default_factory=lambda: os.environ.get("AGENTICMIND_ASSISTANT_BASE_URL") or None
    )
    #: Sign-in (ADR-0023): "required" (default) or "off" (single-user tinkering; every request is anonymous).
    auth_mode: str = field(default_factory=lambda: os.environ.get("AGENTICMIND_AUTH", "off").strip().lower())
    session_days: int = field(default_factory=lambda: int(os.environ.get("AGENTICMIND_SESSION_DAYS", "14")))
    #: Mark the session cookie ``Secure`` (set when the app is served over HTTPS).
    cookie_secure: bool = field(
        default_factory=lambda: os.environ.get("AGENTICMIND_COOKIE_SECURE", "0").lower() in ("1", "true", "yes")
    )
    #: Optional directory with the built web app, served at ``/`` (single-container deployments).
    web_dist: Path | None = field(
        default_factory=lambda: Path(p) if (p := os.environ.get("AGENTICMIND_WEB_DIST")) else None
    )
