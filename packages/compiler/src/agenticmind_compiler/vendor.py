"""Vendor the AgenticMind packages an exported project needs, and pin its third-party dependencies.

The export must run without any hosted AgenticMind service and without unpublished packages, so the
first-party runtime and the node packages the graph uses are copied into ``vendor/<distribution>/`` with a
``pyproject.toml`` synthesised from the installed metadata (works for editable and wheel installs alike).
Third-party requirements are pinned to the versions installed alongside the compiler, i.e. the versions
the platform itself was tested with.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

FIRST_PARTY = "agenticmind-"
#: Always needed to load, validate and execute a graph.
BASE_DISTRIBUTIONS = ("agenticmind-ir", "agenticmind-node-sdk", "agenticmind-registry", "agenticmind-runtime")
_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_EXTRA = re.compile(r"""extra\s*==\s*["']([^"']+)["']""")
_SKIP_DIRS = {"__pycache__", "tests"}


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class Distribution:
    name: str
    version: str
    summary: str
    module: str
    source_dir: Path
    requires: list[str] = field(default_factory=list)
    extras: dict[str, list[str]] = field(default_factory=dict)
    entry_points: list[tuple[str, str, str]] = field(default_factory=list)


def inspect_distribution(name: str) -> Distribution:
    dist = metadata.distribution(name)
    module_name = canonical(name).replace("-", "_")
    module = importlib.import_module(module_name)
    requires: list[str] = []
    extras: dict[str, list[str]] = {}
    for req in dist.requires or []:
        spec, _, marker = req.partition(";")
        found = _EXTRA.search(marker)
        if found:
            extras.setdefault(found.group(1), []).append(spec.strip())
        else:
            requires.append(req.strip())
    return Distribution(
        name=canonical(name),
        version=dist.version,
        summary=dist.metadata.get("Summary") or "",
        module=module_name,
        source_dir=Path(module.__file__).resolve().parent,
        requires=requires,
        extras=extras,
        entry_points=[(e.group, e.name, e.value) for e in dist.entry_points],
    )


def requirement_name(req: str) -> str:
    m = _NAME.match(req)
    return canonical(m.group(1)) if m else canonical(req)


def closure(roots: list[str]) -> list[Distribution]:
    """First-party distributions needed by ``roots`` (dependencies first, deterministic order)."""
    seen: dict[str, Distribution] = {}

    def visit(name: str) -> None:
        name = canonical(name)
        if name in seen:
            return
        dist = inspect_distribution(name)
        seen[name] = dist
        for req in dist.requires:
            dep = requirement_name(req)
            if dep.startswith(FIRST_PARTY):
                visit(dep)

    for root in [*BASE_DISTRIBUTIONS, *roots]:
        visit(root)
    return [seen[k] for k in sorted(seen)]


def pins(dists: list[Distribution], extras: dict[str, set[str]]) -> tuple[dict[str, str], list[str]]:
    """Exact versions for every third-party requirement (base + selected extras) of the vendored packages."""
    pinned: dict[str, str] = {}
    missing: list[str] = []
    for dist in dists:
        reqs = list(dist.requires)
        for extra in sorted(extras.get(dist.name, set())):
            reqs += dist.extras.get(extra, [])
        for req in reqs:
            name = requirement_name(req)
            if name.startswith(FIRST_PARTY) or name in pinned:
                continue
            bracket = re.match(r"^[^\[;<>=!~ ]+(\[[^\]]+\])", req)
            try:
                pinned[name] = f"{name}{bracket.group(1) if bracket else ''}=={metadata.version(name)}"
            except metadata.PackageNotFoundError:
                pinned[name] = req
                missing.append(name)
    return dict(sorted(pinned.items())), missing


def _toml_list(items: list[str]) -> str:
    if not items:
        return "[]"
    return "[\n" + "".join(f"    {_q(i)},\n" for i in items) + "]"


def _q(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pyproject_for(dist: Distribution) -> str:
    lines = [
        "# Vendored by the AgenticMind compiler from the installed distribution; do not edit.",
        "[project]",
        f"name = {_q(dist.name)}",
        f"version = {_q(dist.version)}",
        f"description = {_q(dist.summary)}",
        'requires-python = ">=3.11"',
        f"dependencies = {_toml_list(dist.requires)}",
    ]
    if dist.extras:
        lines += ["", "[project.optional-dependencies]"]
        lines += [f"{extra} = {_toml_list(reqs)}" for extra, reqs in sorted(dist.extras.items())]
    groups: dict[str, list[tuple[str, str]]] = {}
    for group, name, value in dist.entry_points:
        groups.setdefault(group, []).append((name, value))
    for group, items in sorted(groups.items()):
        if group == "console_scripts":
            lines += ["", "[project.scripts]"]
        else:
            lines += ["", f'[project.entry-points."{group}"]']
        lines += [f"{name} = {_q(value)}" for name, value in sorted(items)]
    lines += [
        "",
        "[build-system]",
        'requires = ["hatchling"]',
        'build-backend = "hatchling.build"',
        "",
        "[tool.hatch.build.targets.wheel]",
        f'packages = ["src/{dist.module}"]',
        "",
    ]
    return "\n".join(lines)


def vendor_files(dists: list[Distribution]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for dist in dists:
        root = f"vendor/{dist.name}"
        files[f"{root}/pyproject.toml"] = pyproject_for(dist).encode()
        for path in sorted(dist.source_dir.rglob("*")):
            rel = path.relative_to(dist.source_dir)
            if not path.is_file() or _SKIP_DIRS & set(rel.parts) or path.suffix in (".pyc", ".pyo"):
                continue
            files[f"{root}/src/{dist.module}/{rel.as_posix()}"] = path.read_bytes()
    return files
