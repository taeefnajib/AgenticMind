"""Extract the subset of NVIDIA Auto Ontology's gateway OpenAPI spec that the adapter uses (ADR-0019).

Usage: uv run python scripts/extract_auto_ontology_contract.py <auto-ontology checkout> [commit]

Reads ``docs/openapi/auto-ontology-api.json`` from a checkout of the pinned commit and writes
``nodes/semantic/src/agenticmind_nodes_semantic/contract/auto-ontology-api.subset.json``: the used operations
plus every schema they reference. Contract tests check the adapter's requests and parsing against it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

USED = {
    "/api/health": ["get"],
    "/api/semantic-compilation/status": ["get"],
    "/api/terms": ["get"],
    "/api/terms/{term_id}": ["get"],
    "/api/terms/{term_id}/sql-attributes": ["get"],
    "/api/chat/completions": ["post"],
}
OUT = Path(__file__).resolve().parents[1] / "nodes/semantic/src/agenticmind_nodes_semantic/contract"


def refs(node: object, found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[1])
        for v in node.values():
            refs(v, found)
    elif isinstance(node, list):
        for v in node:
            refs(v, found)


def main() -> None:
    checkout = Path(sys.argv[1])
    commit = sys.argv[2] if len(sys.argv) > 2 else "778ccdef75536bee5653306f8ded655924e4ada8"
    spec = json.loads((checkout / "docs/openapi/auto-ontology-api.json").read_text(encoding="utf-8"))
    paths = {p: {m: spec["paths"][p][m] for m in methods} for p, methods in USED.items()}
    schemas = spec.get("components", {}).get("schemas", {})
    needed: set[str] = set()
    refs(paths, needed)
    while True:
        more: set[str] = set()
        for name in needed:
            refs(schemas[name], more)
        if more <= needed:
            break
        needed |= more
    subset = {
        "openapi": spec["openapi"],
        "info": {**spec["info"], "x-agenticmind-source": {"repo": "NVIDIA/auto-ontology", "commit": commit}},
        "paths": paths,
        "components": {"schemas": {n: schemas[n] for n in sorted(needed)}},
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "auto-ontology-api.subset.json").write_text(
        json.dumps(subset, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"{len(paths)} paths, {len(needed)} schemas, API {spec['info']['version']}")


if __name__ == "__main__":
    main()
