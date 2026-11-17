"""Load a local ``.env`` file into the process environment (development convenience).

Real environment variables always win over values in the file. The file location is
``AGENTICMIND_ENV_FILE`` if set, otherwise ``.env`` in the current working directory (the repository
root when started with ``npm run dev:api``). Values are never logged; only the variable *names*
loaded are reported.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("agenticmind.env")


def env_file_path() -> Path:
    return Path(os.environ.get("AGENTICMIND_ENV_FILE", ".env"))


def load_env_file(path: Path | None = None) -> list[str]:
    """Load ``path`` without overriding existing variables. Returns the names newly set."""
    from dotenv import dotenv_values

    path = path or env_file_path()
    if not path.is_file():
        return []
    loaded = []
    for name, value in dotenv_values(path).items():
        if value is None or value == "" or name in os.environ:
            continue
        os.environ[name] = value
        loaded.append(name)
    if loaded:
        log.info("loaded %d variable(s) from %s: %s", len(loaded), path, ", ".join(sorted(loaded)))
    return loaded
