"""``agenticmind-node``: create, validate, test and package a third-party node package (ADR-0021).

    agenticmind-node new acme-text-nodes --namespace acme
    agenticmind-node validate acme-text-nodes
    agenticmind-node test acme-text-nodes
    agenticmind-node pack acme-text-nodes      # → acme-text-nodes/dist/*.whl, install it in AgenticMind

Run it from an environment where ``agenticmind-node-sdk`` is installed. ``validate`` and ``test`` import
*your* package on *your* machine; the platform never imports it (it runs nodes in an isolated worker).
"""

from __future__ import annotations

import argparse
import importlib
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

ENTRY_POINT_GROUP = "agenticmind.nodes"
RESERVED = "agenticmind."
_DIST = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
_NS = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
#: Permissions available to isolated plugin nodes (mirrors agenticmind_plugins.ISOLATED_PERMISSIONS).
ISOLATED_PERMISSIONS = frozenset({"network", "credentials", "filesystem", "subprocess", "database"})


def _test_file(module: str) -> str:
    # import order as ruff/isort sorts it, so a fresh package passes `ruff check`
    froms = sorted(
        [
            f"from {module}.nodes import word_stats",
            "from agenticmind_node_sdk import NodeError",
            "from agenticmind_node_sdk.testing import invoke_node_sync",
        ],
        key=lambda line: line.split()[1],
    )
    body = (
        "\n\ndef test_word_stats_counts_and_ranks():\n"
        '    result = invoke_node_sync(word_stats, inputs={"text": "The cat sat. The cat ran! A dog?"}, '
        'config={"top_n": 2})\n'
        '    assert result.outputs["stats"]["sentences"] == 3\n'
        '    assert result.outputs["stats"]["top_words"][0] == {"word": "the", "count": 2}\n'
        '    assert result.outputs["summary"].startswith("7 words")  # "A" is shorter than min_length\n'
        "\n\ndef test_word_stats_rejects_empty_text():\n"
        '    with pytest.raises(NodeError, match="empty"):\n'
        '        invoke_node_sync(word_stats, inputs={"text": "  "})\n'
    )
    return "import pytest\n" + "\n".join(froms) + "\n" + body


# ---- new ----------------------------------------------------------------------------------------
def _scaffold(dist: str, ns: str) -> dict[str, str]:
    module = dist.replace("-", "_")
    node_type = f"{ns}.text.word_stats"
    return {
        "pyproject.toml": f'''[project]
name = "{dist}"
version = "0.1.0"
description = "AgenticMind nodes by {ns}"
requires-python = ">=3.11"
# agenticmind-* packages are provided by the AgenticMind host; list only your other dependencies here
dependencies = ["agenticmind-node-sdk"]

[project.entry-points."{ENTRY_POINT_GROUP}"]
{module} = "{module}:register"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/{module}"]
''',
        f"src/{module}/__init__.py": f'''"""AgenticMind nodes by {ns} (``{ENTRY_POINT_GROUP}`` entry point)."""

from __future__ import annotations

from typing import Any

from .nodes import word_stats

ALL_NODES = (word_stats,)


def register(registrar: Any) -> None:
    for definition in ALL_NODES:
        registrar(definition)
''',
        f"src/{module}/nodes.py": f'''"""Example node: a typed contract and an async handler."""

from __future__ import annotations

import re
from collections import Counter

from agenticmind_node_sdk import (
    InputPort,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    PortType,
    define_node,
)
from pydantic import Field

PACKAGE = "{dist}"


class WordStatsConfig(NodeConfig):
    top_n: int = Field(default=5, ge=1, le=50, description="How many frequent words to report.")
    min_length: int = Field(default=3, ge=1, le=20, description="Ignore words shorter than this.")


@define_node(
    type="{node_type}",
    version="1.0.0",
    display_name="Word Statistics",
    category="transform",
    package=PACKAGE,
    description="Counts words and sentences and lists the most frequent words in a text.",
    inputs=[InputPort(name="text", type=PortType.STRING)],
    outputs=[
        OutputPort(name="stats", type=PortType.JSON, description="{{words, sentences, top_words}}"),
        OutputPort(name="summary", type=PortType.STRING),
    ],
    config=WordStatsConfig,
    # no permissions needed: it only computes. Declare network/credentials/... only if you use them.
    tags=["text", "{ns}"],
)
async def word_stats(ctx: NodeContext, inputs, config: WordStatsConfig) -> NodeResult:
    text = str(inputs.get("text") or "")
    if not text.strip():
        raise NodeError("TEXT_EMPTY", "The text is empty")
    words = [w.lower() for w in re.findall(r"[\\w']+", text) if len(w) >= config.min_length]
    sentences = max(1, len(re.findall(r"[.!?]+(?:\\s|$)", text)))
    top = Counter(words).most_common(config.top_n)
    await ctx.emit("word_stats", {{"words": len(words)}})
    stats = {{"words": len(words), "sentences": sentences, "top_words": [{{"word": w, "count": c}} for w, c in top]}}
    summary = f"{{len(words)}} words in {{sentences}} sentence(s); most frequent: " + ", ".join(w for w, _ in top)
    return NodeResult(outputs={{"stats": stats, "summary": summary}})
''',
        "tests/test_nodes.py": _test_file(module),
        "README.md": f"""# {dist}

AgenticMind nodes by `{ns}`.

```
agenticmind-node validate .    # check the node contracts
agenticmind-node test .        # run the tests
agenticmind-node pack .        # build dist/*.whl
```

Install the wheel in AgenticMind under **Node packages**, review the permissions it requests and approve
them. The nodes then appear in the node library and run in an isolated worker process.

Rules: node types live in your own namespace (`{ns}.…`), versions are semver, and every permission a node
uses must be declared (`network`, `credentials`, `filesystem`, `subprocess`, `database`). Secrets are
referenced in config (`{{"$secret": "NAME"}}`) and read with `ctx.secret(...)`.
""",
    }


def cmd_new(args: argparse.Namespace) -> int:
    root = Path(args.path)
    dist = args.name or root.name
    ns = args.namespace or dist.split("-")[0]
    if not _DIST.match(dist) or dist.startswith("agenticmind"):
        return _fail(f"'{dist}': use lowercase letters, digits and dashes; 'agenticmind' names are reserved")
    if not _NS.match(ns) or ns == "agenticmind":
        return _fail(f"namespace '{ns}': lowercase letters/digits/underscores; 'agenticmind' is reserved")
    if root.exists() and any(root.iterdir()):
        return _fail(f"{root} is not empty")
    for rel, text in _scaffold(dist, ns).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    print(f"Created {dist} in {root}. Next: agenticmind-node validate {root}")
    return 0


# ---- validate -----------------------------------------------------------------------------------
def load_package(root: Path) -> tuple[dict[str, Any], list[Any]]:
    """Import the package from ``root/src`` via its entry point and collect its node definitions."""
    meta = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    eps = meta.get("project", {}).get("entry-points", {}).get(ENTRY_POINT_GROUP, {})
    if not eps:
        raise ValueError(f'pyproject.toml has no [project.entry-points."{ENTRY_POINT_GROUP}"]')
    src = str((root / "src").resolve())
    if src not in sys.path:
        sys.path.insert(0, src)
    found: list[Any] = []
    for target in eps.values():
        mod_name, _, attr = target.partition(":")
        mod = importlib.import_module(mod_name)
        getattr(mod, attr or "register")(found.append)
    return meta, found


def validate(root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        meta, nodes = load_package(root)
    except Exception as exc:  # noqa: BLE001 - reported to the author
        return [f"Could not load the package: {type(exc).__name__}: {exc}"], []
    dist = meta.get("project", {}).get("name", "")
    if not _DIST.match(dist or "") or dist.startswith("agenticmind"):
        errors.append(f"project.name '{dist}': lowercase/digits/dashes; 'agenticmind' names are reserved")
    if not nodes:
        errors.append("register() registered no nodes")
    seen: set[tuple[str, str]] = set()
    tests = (
        "\n".join(p.read_text(encoding="utf-8") for p in (root / "tests").glob("test_*.py"))
        if (root / "tests").is_dir()
        else ""
    )
    if not tests:
        errors.append("No tests: add tests/test_*.py using agenticmind_node_sdk.testing.invoke_node")
    for d in nodes:
        m = d.manifest
        label = f"{m.type}@{m.version}"
        if d.key in seen:
            errors.append(f"{label} is registered twice")
        seen.add(d.key)
        if m.type.startswith(RESERVED):
            errors.append(f"{label}: the 'agenticmind.' namespace is reserved for core nodes")
        if m.package != dist:
            warnings.append(f"{label}: package='{m.package}' differs from project.name '{dist}'")
        if len(m.description) < 15:
            errors.append(f"{label}: write a description (shown in the node library)")
        if not m.outputs and not m.branches:
            warnings.append(f"{label}: no outputs")
        perms = {k for k, v in m.permissions.model_dump().items() if v is True}
        unsupported = perms - ISOLATED_PERMISSIONS
        if unsupported:
            errors.append(
                f"{label}: {', '.join(sorted(unsupported))} cannot be granted to plugin nodes yet "
                "(they run in an isolated worker without project files or memory)"
            )
        fn_name = getattr(d.handler, "__name__", "")
        if tests and fn_name and fn_name not in tests and m.type not in tests:
            warnings.append(f"{label}: no test mentions '{fn_name}'")
    return errors, warnings


def cmd_validate(args: argparse.Namespace) -> int:
    errors, warnings = validate(Path(args.path))
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    if errors:
        return 1
    print("ok: the package is valid")
    return 0


# ---- test / pack --------------------------------------------------------------------------------
def cmd_test(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    env_path = str(root / "src")
    import os

    env = {**os.environ, "PYTHONPATH": env_path + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return subprocess.call([sys.executable, "-m", "pytest", "-q", str(root / "tests")], cwd=root, env=env)


def cmd_pack(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    errors, _ = validate(root)
    if errors:
        for e in errors:
            print(f"error: {e}")
        return 1
    out = root / "dist"
    uv = shutil.which("uv")
    cmd = [uv, "build", "--wheel", "--out-dir", str(out), str(root)] if uv else [
        sys.executable, "-m", "build", "--wheel", "--outdir", str(out), str(root)
    ]  # fmt: skip
    code = subprocess.call(cmd)
    if code == 0:
        wheels = sorted(out.glob("*.whl"), key=lambda p: p.stat().st_mtime)
        if wheels:
            print(f"Built {wheels[-1]}")
    return code


def _fail(message: str) -> int:
    print(f"error: {message}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agenticmind-node", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new", help="create a node package")
    p.add_argument("path")
    p.add_argument("--name", help="distribution name (default: the folder name)")
    p.add_argument("--namespace", help="node type namespace (default: first part of the name)")
    p.set_defaults(fn=cmd_new)
    for name, fn, text in (
        ("validate", cmd_validate, "check the node contracts"),
        ("test", cmd_test, "run the package's tests"),
        ("pack", cmd_pack, "validate and build a wheel"),
    ):
        q = sub.add_parser(name, help=text)
        q.add_argument("path")
        q.set_defaults(fn=fn)
    args = parser.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
