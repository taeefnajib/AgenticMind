"""Workspace settings edited in the Settings page (ADR-0023).

Values saved here override the environment defaults (``AGENTICMIND_ASSISTANT_*``); resetting removes the
override. Only secret *names* are stored, never secret values (constraint 6): the key itself stays in the
environment / ``.env``.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

from agenticmind_providers import KEYED_CHAT_PROVIDERS, PROVIDERS, provider_names

from .auth import AuthStore
from .settings import Settings

ASSISTANT_KEY = "assistant"
SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MODEL_RE = re.compile(r"^\S{1,200}$")
#: Providers that call a remote API and therefore need a key (an OpenAI-compatible server usually does too).
KEYED_PROVIDERS = set(KEYED_CHAT_PROVIDERS) | {"openai_compatible"}


class SettingsError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class AssistantConfig:
    provider: str
    model: str
    api_key_secret: str
    base_url: str | None
    max_attempts: int = 3

    @property
    def needs_key(self) -> bool:
        return self.provider in KEYED_PROVIDERS


def env_assistant(settings: Settings) -> AssistantConfig:
    return AssistantConfig(
        provider=settings.assistant_provider,
        model=settings.assistant_model,
        api_key_secret=settings.assistant_api_key_secret,
        base_url=settings.assistant_base_url,
    )


def validate_assistant(raw: dict[str, Any]) -> AssistantConfig:
    provider = str(raw.get("provider", "")).strip()
    if provider not in provider_names():
        raise SettingsError("provider", f"Unknown provider {provider!r}. Choose one of: {', '.join(provider_names())}.")
    model = str(raw.get("model", "")).strip()
    if not MODEL_RE.match(model):
        raise SettingsError("model", "Enter a model id without spaces (up to 200 characters).")
    secret = str(raw.get("api_key_secret") or "").strip()
    if provider in KEYED_PROVIDERS and not SECRET_NAME_RE.match(secret):
        raise SettingsError(
            "api_key_secret", "Enter the name of the secret holding the key, e.g. OPENROUTER_API_KEY (A-Z, 0-9, _)."
        )
    base_url = str(raw.get("base_url") or "").strip() or None
    region_ok = provider == "bedrock" and base_url is not None and re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", base_url)
    if base_url is not None and not region_ok:
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or len(base_url) > 500:
            raise SettingsError("base_url", "The base URL must be an http(s) URL." if provider != "bedrock"
                                else "Enter the AWS region (e.g. us-east-1) or the Bedrock runtime URL.")  # fmt: skip
    info = PROVIDERS.get(provider)
    if info is not None and info.base_url_required and base_url is None:
        raise SettingsError("base_url", f"{info.label} needs {info.base_url_required}.")
    try:
        attempts = int(raw.get("max_attempts", 3))
    except (TypeError, ValueError):
        attempts = 0
    if not 1 <= attempts <= 5:
        raise SettingsError("max_attempts", "Repair attempts must be between 1 and 5.")
    default_secret = (info.key_secret if info else None) or "OPENROUTER_API_KEY"
    return AssistantConfig(provider, model, secret or default_secret, base_url, attempts)


def effective_assistant(settings: Settings, auth: AuthStore) -> tuple[AssistantConfig, str]:
    """The assistant configuration in force and where it comes from ("workspace" or "environment")."""
    row = auth.get_setting(ASSISTANT_KEY)
    if row is not None:
        try:
            return validate_assistant(row.value), "workspace"
        except SettingsError:
            pass  # e.g. a provider that no longer exists: fall back to the environment
    return env_assistant(settings), "environment"


def assistant_json(cfg: AssistantConfig) -> dict[str, Any]:
    return asdict(cfg)
