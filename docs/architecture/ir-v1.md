# IR v1

The canonical IR is the durable source of truth for architecture, validation, execution planning,
(future) code generation, diffing and migration. Models live in `packages/ir/src/agenticmind_ir/models.py`;
the published schema is `packages/ir/schema/ir-v1.0.schema.json` (also served at `GET /api/ir/schema` and
printed by `agenticmind schema`).

## Document shape

```json
{
  "ir_version": "1.0",
  "project": {"id": "prj_1a2b3c", "name": "Hello pipeline", "description": ""},
  "runtime": {"engine": "agenticmind", "profile": "development"},
  "nodes": [
    {"id": "name", "type": "agenticmind.core.manual_input", "version": "1.0.0",
     "config": {"key": "name", "value_type": "String", "default": "world"},
     "ui": {"position": {"x": 0, "y": 80}}},
    {"id": "greet", "type": "agenticmind.core.text_template", "version": "1.0.0",
     "config": {"template": "Hello, {{ text }}!"},
     "execution": {"timeout_seconds": 5}},
    {"id": "result", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "greeting"}}
  ],
  "edges": [
    {"id": "e1", "kind": "data",
     "source": {"node": "name", "port": "value"}, "target": {"node": "greet", "port": "text"}},
    {"id": "e2", "kind": "data",
     "source": {"node": "greet", "port": "text"}, "target": {"node": "result", "port": "value"}}
  ],
  "metadata": {}
}
```

## Fields

| Field | Rules |
|---|---|
| `ir_version` | `"1.0"`. Older versions are upgraded by registered migrations before validation. |
| `project.id` | `[A-Za-z0-9_-]{1,64}`. The API forces it to match the resource ID. |
| `nodes[].id` | Stable, `^[A-Za-z_][A-Za-z0-9_-]{0,63}$`, unique (validator: `GRAPH_DUPLICATE_NODE_ID`). |
| `nodes[].type` | Dotted, ≥3 segments, e.g. `agenticmind.core.output`. Resolved in the registry. |
| `nodes[].version` | Semantic version of the node contract. Exact match required. |
| `nodes[].config` | Free JSON at IR level; validated against the node's own config schema. Secret fields hold `{"$secret": "NAME"}` only. |
| `nodes[].execution` | Optional overrides: `timeout_seconds` (0–3600], `max_attempts` 1–10. |
| `nodes[].ui` | Presentation only (`position`, `label`); never affects execution. |
| `edges[].kind` | `data` (typed value, output port → input port) or `control` (branch → `trigger`). |
| `metadata` | Free JSON for tooling. |

Unknown fields are rejected everywhere (`extra="forbid"`), so typos fail loudly.

## Control edges

Every node has an implicit control input port `trigger` and one or more control output **branches**.
Ordinary nodes have a single branch `done`; branching nodes declare theirs (Conditional: `true`, `false`).

```json
{"id": "c1", "kind": "control", "source": {"node": "check", "port": "true"},
 "target": {"node": "notify", "port": "trigger"}}
```

## Deterministic serialization

`to_canonical_json` sorts nodes and edges by ID and keys alphabetically, omits unset optional structural
fields, and preserves user-authored config exactly (including explicit `null`). `content_hash` is SHA-256
of the compact canonical form; revisions and runs record it, so a run can always be traced to the exact
architecture it executed. Export (`GET /api/projects/{id}/export`) returns this canonical form and can be
re-imported (`POST /api/projects` with `document`).

## Migrations

```python
from agenticmind_ir import register_migration

@register_migration("1.0", "1.1")
def add_subgraphs(doc: dict) -> dict:
    doc.setdefault("subgraphs", [])
    return doc
```

Migrations operate on raw dicts before validation, chain until the current version, and must preserve all
user configuration. Add a compatibility test per migration and regenerate the schema.

## Planned additions (non-breaking 1.x)

Subgraphs, explicit graph-level input/output contracts, environment requirements, loop/parallel/join
control nodes. See the roadmap.
