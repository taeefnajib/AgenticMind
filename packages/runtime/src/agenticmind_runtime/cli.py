"""``agenticmind`` command-line interface.

    agenticmind nodes                      list installed node types
    agenticmind schema                     print the IR JSON Schema
    agenticmind validate graph.json        validate an IR document
    agenticmind run graph.json -i name=Ada run a graph in the first-party runtime
    agenticmind canonicalize graph.json    print the canonical (diff-friendly) form
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from agenticmind_ir import ir_json_schema, to_canonical_json
from agenticmind_registry import default_registry

from .engine import Engine
from .events import InMemoryObserver, RunStatus
from .plan import PlanError, compile_plan
from .secrets import EnvSecretResolver
from .validation import validate_project


def _print(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")


def _parse_inputs(pairs: list[str], inputs_json: str | None) -> dict[str, Any]:
    inputs: dict[str, Any] = json.loads(inputs_json) if inputs_json else {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep:
            raise SystemExit(f"--input expects key=value, got {pair!r}")
        try:
            inputs[key] = json.loads(raw)
        except json.JSONDecodeError:
            inputs[key] = raw
    return inputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agenticmind", description="AgenticMind CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("nodes", help="List installed node types")
    sub.add_parser("schema", help="Print the IR JSON Schema")
    p_val = sub.add_parser("validate", help="Validate an IR document")
    p_val.add_argument("file", type=Path)
    p_can = sub.add_parser("canonicalize", help="Print canonical IR JSON")
    p_can.add_argument("file", type=Path)
    p_run = sub.add_parser("run", help="Execute an IR document")
    p_run.add_argument("file", type=Path)
    p_run.add_argument("-i", "--input", action="append", default=[], help="key=value (JSON values allowed)")
    p_run.add_argument("--inputs-json", help="Run inputs as a JSON object")
    p_run.add_argument("--events", action="store_true", help="Print trace events")
    args = parser.parse_args(argv)

    if args.cmd == "schema":
        _print(ir_json_schema())
        return 0

    registry = default_registry()
    if args.cmd == "nodes":
        _print(
            [
                {"type": e.manifest.type, "version": e.manifest.version, "category": e.manifest.category,
                 "trust": e.trust.value, "name": e.manifest.display_name}
                for e in registry.entries()
            ]
        )
        return 0

    source = args.file.read_text(encoding="utf-8")
    secrets = EnvSecretResolver()
    doc, report = validate_project(source, registry, secrets=secrets)

    if args.cmd == "validate":
        _print(report.to_public())
        return 0 if report.valid else 1
    if doc is None:
        _print(report.to_public())
        return 1
    if args.cmd == "canonicalize":
        sys.stdout.write(to_canonical_json(doc))
        return 0

    try:
        plan = compile_plan(doc, registry, secrets=secrets)
    except PlanError as exc:
        _print(exc.report.to_public())
        return 1
    observer = InMemoryObserver()
    result = asyncio.run(
        Engine(observer=observer, secrets=secrets).run(plan, inputs=_parse_inputs(args.input, args.inputs_json))
    )
    out: dict[str, Any] = {
        "run_id": result.run_id,
        "status": result.status.value,
        "outputs": result.outputs,
        "node_status": {k: v.value for k, v in result.node_status.items()},
        "error": result.error.model_dump() if result.error else None,
    }
    if args.events:
        out["events"] = [e.model_dump(mode="json", exclude_none=True) for e in observer.events]
    _print(out)
    return 0 if result.status == RunStatus.SUCCEEDED else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
