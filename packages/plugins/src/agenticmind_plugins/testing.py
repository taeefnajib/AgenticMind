"""Test a node package the way the platform runs it: built as a wheel, installed into its own directory,
described and invoked by the isolated worker (ADR-0021)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .package import describe, inspect_wheel, install
from .remote import RemoteNodeDefinition, make_definition


def build_wheel(project: Path, out: Path) -> Path:
    """Build ``project`` (a folder with pyproject.toml) into ``out``; returns the wheel."""
    out.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv")
    cmd = [uv, "build", "--wheel", "--out-dir", str(out), str(project)] if uv else [
        sys.executable, "-m", "build", "--wheel", "--outdir", str(out), str(project)
    ]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"building {project} failed: {proc.stderr[-2000:]}")
    return max(out.glob("*.whl"), key=lambda p: p.stat().st_mtime)


def install_isolated(wheel: Path, root: Path) -> list[RemoteNodeDefinition]:
    """Install ``wheel`` under ``root`` and return its nodes as isolated definitions."""
    info = inspect_wheel(wheel)
    target = install(wheel, info, root / "packages")
    nodes = describe(target, workdir=root / "work")
    return [
        make_definition(n["manifest"], plugin_dir=target, work_root=root / "work", storage_dir=root / "storage")
        for n in nodes
    ]
