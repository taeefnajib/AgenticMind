"""Plugin packages (ADR-0021): inspect a wheel, install it into its own directory, read its manifests.

A plugin is installed into ``<plugins_root>/<name>-<version>/``, never into the platform's environment.
Its AgenticMind dependencies (node SDK, providers) are supplied by the host and are not installed again;
its other dependencies are installed next to it. Manifests are read by the isolated worker, so installing
a package never imports it into the platform process.
"""

from __future__ import annotations

import email.parser
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Permissions an isolated node may hold today. The others (artifacts, memory) need host services
#: (project files, conversation memory) that the worker protocol does not provide yet.
ISOLATED_PERMISSIONS = frozenset({"network", "credentials", "filesystem", "subprocess", "database"})
_HOST_PROVIDED = re.compile(r"^agenticmind[-_]", re.I)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
#: Node types are namespaced by their author; the ``agenticmind.`` namespace is reserved for core nodes.
RESERVED_NAMESPACE = "agenticmind."


class PluginError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class WheelInfo:
    name: str
    version: str
    sha256: str
    requires: list[str]
    summary: str = ""
    entry_points: list[str] = field(default_factory=list)


def inspect_wheel(path: Path) -> WheelInfo:
    """Read name, version, dependencies and entry points from a wheel without importing anything."""
    if path.suffix != ".whl":
        raise PluginError("PLUGIN_NOT_A_WHEEL", "Upload a Python wheel (.whl) built with `agenticmind-node pack`")
    data = path.read_bytes()
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise PluginError("PLUGIN_NOT_A_WHEEL", "The file is not a valid wheel") from exc
    with zf:
        names = zf.namelist()
        if any(n.startswith("/") or ".." in Path(n).parts for n in names):
            raise PluginError("PLUGIN_UNSAFE_ARCHIVE", "The wheel contains unsafe paths")
        meta = next((n for n in names if n.endswith(".dist-info/METADATA")), None)
        if meta is None:
            raise PluginError("PLUGIN_NOT_A_WHEEL", "The wheel has no METADATA")
        msg = email.parser.Parser().parsestr(zf.read(meta).decode("utf-8", "replace"))
        eps_name = meta.replace("METADATA", "entry_points.txt")
        eps = zf.read(eps_name).decode("utf-8", "replace") if eps_name in names else ""
    name, version = msg.get("Name", ""), msg.get("Version", "")
    if not _NAME.match(name) or not version:
        raise PluginError("PLUGIN_NOT_A_WHEEL", "The wheel has no valid name/version")
    if _HOST_PROVIDED.match(name):
        raise PluginError("PLUGIN_RESERVED_NAME", "Package names starting with 'agenticmind' are reserved")
    groups = re.findall(r"^\[([^\]]+)\]", eps, re.M)
    if "agenticmind.nodes" not in groups:
        raise PluginError(
            "PLUGIN_NO_NODES",
            'The wheel declares no [project.entry-points."agenticmind.nodes"] entry point',
        )
    requires = [r for r in msg.get_all("Requires-Dist") or [] if "extra ==" not in r]
    return WheelInfo(
        name=name,
        version=version,
        sha256=hashlib.sha256(data).hexdigest(),
        requires=requires,
        summary=msg.get("Summary", ""),
        entry_points=groups,
    )


def _installer() -> list[str]:
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", sys.executable]
    return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]


def install(wheel: Path, info: WheelInfo, plugins_root: Path, *, timeout: float = 600) -> Path:
    """Install into ``plugins_root/<name>-<version>``. Returns the directory."""
    target = (plugins_root / f"{info.name.lower()}-{info.version}").resolve()
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    base = _installer()
    steps = [[*base, "--target", str(target), "--no-deps", str(wheel)]]
    third_party = [r for r in info.requires if not _HOST_PROVIDED.match(r.strip())]
    if third_party:
        steps.append([*base, "--target", str(target), *third_party])
    for cmd in steps:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            shutil.rmtree(target, ignore_errors=True)
            raise PluginError("PLUGIN_INSTALL_FAILED", (proc.stderr or proc.stdout).strip()[-2000:])
    return target


def worker_env(workdir: Path) -> dict[str, str]:
    """A scrubbed environment: nothing from the host except what Python needs to start."""
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(workdir)}
    for key in ("SYSTEMROOT", "WINDIR", "PATH", "LANG", "LC_ALL", "TZ"):
        if key in os.environ:
            env[key] = os.environ[key]
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(workdir)
    return env


def run_worker(
    mode: str, plugin_dir: Path, *, workdir: Path, payload: dict[str, Any] | None = None, timeout: float = 60
) -> dict[str, Any]:
    # absolute paths: the worker runs in its own working directory, so relative ones would point elsewhere
    workdir = workdir.resolve()
    plugin_dir = plugin_dir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-I", "-m", "agenticmind_plugins.worker", mode, str(plugin_dir)]
    kwargs: dict[str, Any] = {}
    if os.name == "posix":  # resource limits where the OS supports them

        def limits() -> None:  # pragma: no cover - POSIX only
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (int(timeout) + 5, int(timeout) + 10))
            resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))

        kwargs["preexec_fn"] = limits
    try:
        proc = subprocess.run(
            cmd,
            input=json.dumps(payload) if payload is not None else "",
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            cwd=workdir,
            env=worker_env(workdir),
            **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        raise PluginError("PLUGIN_TIMEOUT", f"The node did not finish within {timeout:g}s") from exc
    try:
        return json.loads(proc.stdout or "")
    except json.JSONDecodeError as exc:
        tail = (proc.stderr or proc.stdout or "").strip()[-1500:]
        raise PluginError("PLUGIN_CRASHED", f"The worker exited with {proc.returncode}: {tail}") from exc


def describe(plugin_dir: Path, *, workdir: Path) -> list[dict[str, Any]]:
    """Manifests of the plugin's nodes, read in an isolated worker."""
    result = run_worker("describe", plugin_dir, workdir=workdir, timeout=120)
    if "error" in result:
        err = result["error"]
        raise PluginError("PLUGIN_DESCRIBE_FAILED", f"{err.get('code')}: {err.get('message')}")
    nodes = result.get("nodes") or []
    if not nodes:
        raise PluginError("PLUGIN_NO_NODES", "The package registered no nodes")
    for n in nodes:
        t = n["manifest"]["type"]
        if t.startswith(RESERVED_NAMESPACE):
            raise PluginError("PLUGIN_RESERVED_NAMESPACE", f"'{t}': the 'agenticmind.' namespace is reserved")
    return nodes


def requested_permissions(nodes: list[dict[str, Any]]) -> list[str]:
    perms: set[str] = set()
    for n in nodes:
        perms |= {k for k, v in n["manifest"].get("permissions", {}).items() if v is True and k != "idempotent"}
    return sorted(perms)


def unsupported_permissions(perms: list[str]) -> list[str]:
    return sorted(set(perms) - ISOLATED_PERMISSIONS)
