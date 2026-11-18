"""Pipeline step 6: format and lint the generated sources with ruff, using the export's own config."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def _ruff() -> str | None:
    try:
        from ruff.__main__ import find_ruff_bin

        return str(find_ruff_bin())
    except Exception:  # noqa: BLE001 - ruff missing or broken: formatting is skipped with a warning
        return None


def format_sources(files: dict[str, bytes]) -> tuple[dict[str, bytes], dict[str, str]]:
    """Return (files with formatted app/ and tests/ sources, check report)."""
    ruff = _ruff()
    if ruff is None:
        return files, {"formatter": "skipped (ruff not installed)", "lint": "skipped"}
    sources = {p: d for p, d in files.items() if p.endswith(".py") and p.startswith(("app/", "tests/"))}
    with tempfile.TemporaryDirectory(prefix="agenticmind-export-") as tmp:
        root = Path(tmp)
        (root / "pyproject.toml").write_bytes(files["pyproject.toml"])
        for path, data in sources.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        flags = {"cwd": root, "capture_output": True, "text": True, "timeout": 120, "check": False}
        subprocess.run([ruff, "check", "--fix", "--quiet", "app", "tests"], **flags)
        fmt = subprocess.run([ruff, "format", "--quiet", "app", "tests"], **flags)
        lint = subprocess.run([ruff, "check", "--quiet", "app", "tests"], **flags)
        out = dict(files)
        for path in sources:
            out[path] = (root / path).read_bytes()
    version = subprocess.run([ruff, "--version"], capture_output=True, text=True, check=False).stdout.strip()
    return out, {
        "formatter": f"{version}: {'ok' if fmt.returncode == 0 else 'failed'}",
        "lint": "ok" if lint.returncode == 0 else (lint.stdout or lint.stderr).strip()[:2000],
    }
