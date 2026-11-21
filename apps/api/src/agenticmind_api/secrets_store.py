"""Workspace secrets (ADR-0026): credentials added in Settings → Secrets, encrypted at rest.

- Values are encrypted with AES-256-GCM; the secret's name is bound in as associated data, so a ciphertext
  cannot be moved to another name. Values are never returned by the API: they are write-only.
- The key comes from ``AGENTICMIND_SECRET_KEY`` (32 bytes, URL-safe base64) or is generated once into
  ``<data dir>/secret.key``. Losing the key makes stored values unreadable (they are then treated as unset).
- Resolution order: the environment first (operators can always override), then the workspace store. Graphs
  keep referencing secrets by name only (``{"$secret": "NAME"}``), so nothing changes in projects.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import secrets as _secrets
from pathlib import Path
from typing import Any

from agenticmind_runtime import SecretResolver
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .db import SecretRow, utcnow

log = logging.getLogger("agenticmind.secrets")
SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_SECRET_BYTES = 64 * 1024  # PEM keys and service-account JSON fit comfortably


class SecretStoreError(ValueError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def load_key(data_dir: Path) -> bytes:
    raw = os.environ.get("AGENTICMIND_SECRET_KEY", "").strip()
    if raw:
        try:
            key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except ValueError as exc:
            raise ValueError("AGENTICMIND_SECRET_KEY must be URL-safe base64") from exc
        if len(key) != 32:
            raise ValueError("AGENTICMIND_SECRET_KEY must decode to 32 bytes")
        return key
    path = data_dir / "secret.key"
    if path.exists():
        key = base64.urlsafe_b64decode(path.read_text().strip())
        if len(key) == 32:
            return key
        raise ValueError(f"{path} does not hold a 32-byte key")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = _secrets.token_bytes(32)
    path.write_text(base64.urlsafe_b64encode(key).decode())
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - e.g. some Windows filesystems
        pass
    log.info("created a new secrets key at %s (back it up with the database)", path)
    return key


class SecretVault:
    def __init__(self, session_factory: sessionmaker, key: bytes) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self._sf = session_factory
        self._aead = AESGCM(key)

    def _encrypt(self, name: str, value: str) -> str:
        nonce = _secrets.token_bytes(12)
        sealed = self._aead.encrypt(nonce, value.encode(), name.encode())
        return base64.b64encode(nonce + sealed).decode()

    def _decrypt(self, name: str, token: str) -> str | None:
        from cryptography.exceptions import InvalidTag

        raw = base64.b64decode(token)
        try:
            return self._aead.decrypt(raw[:12], raw[12:], name.encode()).decode()
        except InvalidTag:
            log.warning("secret %s cannot be decrypted with the current key; treating it as unset", name)
            return None

    def get(self, name: str) -> str | None:
        with self._sf() as s:
            row = s.get(SecretRow, name)
        return self._decrypt(name, row.ciphertext) if row else None

    def has(self, name: str) -> bool:
        with self._sf() as s:
            return s.get(SecretRow, name) is not None

    def list(self) -> list[SecretRow]:
        with self._sf() as s:
            return list(s.scalars(select(SecretRow).order_by(SecretRow.name)))

    def set(self, name: str, value: str, *, user: str | None) -> SecretRow:
        if not SECRET_NAME_RE.match(name):
            raise SecretStoreError(
                422, "SECRET_NAME_INVALID", "Use letters, digits and _ (not starting with a digit), e.g. HF_TOKEN."
            )
        if not value:
            raise SecretStoreError(422, "SECRET_VALUE_EMPTY", "Enter a value.")
        if len(value.encode()) > MAX_SECRET_BYTES:
            raise SecretStoreError(422, "SECRET_VALUE_TOO_LARGE", "Secret values are limited to 64 KB.")
        with self._sf.begin() as s:
            row = s.get(SecretRow, name)
            if row is None:
                row = SecretRow(name=name, ciphertext=self._encrypt(name, value), updated_by=user)
                s.add(row)
            else:
                row.ciphertext = self._encrypt(name, value)
                row.updated_at = utcnow()
                row.updated_by = user
        return row

    def delete(self, name: str) -> bool:
        with self._sf.begin() as s:
            row = s.get(SecretRow, name)
            if row is None:
                return False
            s.delete(row)
            return True


class ChainedSecrets:
    """The environment (or a test mapping) first, then the workspace vault."""

    def __init__(self, primary: SecretResolver, vault: SecretVault) -> None:
        self.primary = primary
        self.vault = vault

    def get(self, name: str) -> str | None:
        return self.primary.get(name) or self.vault.get(name)

    def source(self, name: str) -> str | None:
        """Where a name resolves from: "environment", "workspace" or None (never the value)."""
        if self.primary.get(name):
            return "environment"
        return "workspace" if self.vault.get(name) is not None else None


def secret_json(row: SecretRow, chain: ChainedSecrets) -> dict[str, Any]:
    return {
        "name": row.name,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "updated_by": row.updated_by,
        "overridden_by_environment": bool(chain.primary.get(row.name)),
    }
