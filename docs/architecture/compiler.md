# Compiler and app export

`agenticmind-compiler` turns a saved project revision into a standalone FastAPI project (ADR-0016).

```python
from agenticmind_compiler import compile_project, to_zip, write_directory
result = compile_project(document, registry, artifacts=artifact_store)   # CompileError on invalid graphs/API
open("app.zip", "wb").write(to_zip(result))                                # deterministic bytes
```

HTTP: `GET /api/projects/{id}/app/preview` (endpoints, requirements, warnings, checks, layout) and
`GET /api/projects/{id}/app` (ZIP). In the IDE: **Export app**.

## Exported layout

```
<project>/
  agenticmind.yaml            build manifest (IR hash, compiler + checks, nodes, packages, endpoints, requirements)
  architecture/graph.json     canonical IR (re-import into AgenticMind)
  architecture/schemas/       <endpoint>.request.json / .response.json
  openapi.json                OpenAPI 3.1 contract
  app/main.py                 FastAPI app factory (+ /healthz, /docs)
  app/api/routes.py           one function per endpoint
  app/api/models.py           request/response models generated from Manual Input / Output nodes
  app/api/security.py         API-key auth and rate limit
  app/runtime.py              loads graph.json and runs it with the vendored AgenticMind runtime
  app/endpoints.py            run targets per endpoint
  tests/                      contract tests (+ cases.json)
  examples/                   example request bodies
  data/                       bundled uploaded files (when the graph references any)
  vendor/                     pinned AgenticMind runtime and node packages
  pyproject.toml              uv project (path sources for vendor/), pinned dependencies
  requirements.txt            the same for pip / Docker
  Dockerfile, compose.yaml, .env.example, README.md
```

## Customising endpoints

Add to the IR's `metadata` (export the IR, edit, re-import):

```json
{"api": {"endpoints": [
  {"name": "ask", "path": "/v1/ask", "outputs": ["answer"], "auth": "api_key", "rate_limit_per_minute": 30,
   "summary": "Answer a question about the handbook"}
]}}
```

`outputs` takes Output node IDs or output keys; `auth` is `api_key` or `none`.

## Diagnostic codes

| Code | Meaning |
|---|---|
| `API_ENDPOINT_INVALID` | Bad or duplicate endpoint name/path (or a reserved path like `/healthz`), invalid auth or rate limit |
| `API_UNKNOWN_OUTPUT` | An endpoint references an Output node or key that does not exist |

Graph diagnostics (from validation) are returned unchanged when an endpoint's plan cannot be compiled.
