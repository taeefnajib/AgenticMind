"""Compiler pipeline (ADR-0016): IR → standalone FastAPI project.

1. Load the IR and resolve node package versions (registry).
2. Validate the graph and derive the API (one execution plan per endpoint).
3. Resolve requirements: secrets, services, local models, optional extras, limitations.
4. Generate source, configuration, tests, OpenAPI and deployment assets.
5. Vendor the first-party runtime/node packages and pin third-party versions.
6. Write the build manifest (IR hash, compiler and node versions).
7. Package deterministically (same IR → byte-identical ZIP).
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any

from agenticmind_ir import ProjectDocument, content_hash, iter_secret_refs, to_canonical_json
from agenticmind_registry import NodeRegistry

from . import render
from .checks import format_sources
from .spec import (
    CompileError,
    Endpoint,
    artifact_ids,
    build_endpoints,
    example_request,
    request_schema,
    response_schema,
    slug,
)
from .vendor import closure, pins, vendor_files

COMPILER_NAME = "agenticmind-compiler"
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
APP_PACKAGES = ("fastapi", "uvicorn[standard]", "python-dotenv")
DEV_PACKAGES = ("pytest", "httpx", "ruff")


def compiler_version() -> str:
    try:
        return metadata.version(COMPILER_NAME)
    except metadata.PackageNotFoundError:  # pragma: no cover - running from source
        return "0.0.0"


@dataclass
class CompileResult:
    root: str
    files: dict[str, bytes]
    manifest: dict[str, Any]
    endpoints: list[Endpoint]
    warnings: list[str] = field(default_factory=list)


def _pin(spec: str) -> str:
    name = re.split(r"[\[<>=!~ ]", spec, maxsplit=1)[0]
    try:
        return f"{spec}=={metadata.version(name)}"
    except metadata.PackageNotFoundError:
        return spec


def _yaml(value: Any, indent: int = 0) -> str:
    """Tiny YAML emitter for the manifest (scalars are JSON-quoted, which YAML accepts)."""
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = []
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:\n{_yaml(v, indent + 1)}")
            else:
                lines.append(f"{pad}{k}: {_yaml(v, 0)}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = []
        for item in value:
            if isinstance(item, dict) and item:
                inner = _yaml(item, indent + 1).lstrip()
                lines.append(f"{pad}- {inner}")
            else:
                lines.append(f"{pad}- {_yaml(item, 0)}")
        return "\n".join(lines)
    return json.dumps(value, ensure_ascii=False)


def analyze(doc: ProjectDocument, registry: NodeRegistry, endpoints: list[Endpoint]) -> dict[str, Any]:
    used = set().union(*(ep.node_ids for ep in endpoints))
    extras: dict[str, set[str]] = {}
    local_models: list[str] = []
    services: list[str] = []
    notes: list[str] = []
    network = False
    for node in doc.nodes:
        if node.id not in used:
            continue
        entry = registry.get(node.type, node.version)
        if entry is None:
            continue
        manifest = entry.definition.manifest
        cfg = entry.definition.parse_config(node.config).model_dump(mode="json")
        network = network or manifest.permissions.network
        if node.type == "agenticmind.rag.embedding_model" and cfg.get("provider") == "fastembed":
            extras.setdefault("agenticmind-providers", set()).add("local")
            local_models.append(f"embedding model {cfg['model']} (fastembed, downloaded on first use)")
        if node.type == "agenticmind.rag.reranker" and cfg.get("provider") == "fastembed":
            extras.setdefault("agenticmind-providers", set()).add("local")
            local_models.append(f"reranker {cfg['model']} (fastembed, downloaded on first use)")
        if node.type == "agenticmind.rag.vector_store":
            backend = cfg.get("backend", "qdrant")
            extra = "search" if backend in ("elasticsearch", "opensearch") else backend
            if backend != "qdrant":  # qdrant-client is a base dependency
                extras.setdefault("agenticmind-vectorstores", set()).add(extra)
            # the phrase "under DATA_DIR" marks embedded storage (deployment needs a persistent volume)
            where = "server at configured URL" if cfg.get("url") else "embedded, under DATA_DIR"
            services.append(
                {
                    "qdrant": f"Qdrant ({where})",
                    "chroma": f"Chroma ({where})",
                    "faiss": "FAISS (embedded, under DATA_DIR)",
                    "lancedb": f"LanceDB ({'LanceDB Cloud' if cfg.get('url') else 'embedded, under DATA_DIR'})",
                    "milvus": f"Milvus ({where if cfg.get('url') else 'Milvus Lite, under DATA_DIR'})",
                    "weaviate": "Weaviate (server at configured URL)",
                    "elasticsearch": "Elasticsearch (server at configured URL)",
                    "opensearch": "OpenSearch (server at configured URL)",
                    "pgvector": "PostgreSQL with pgvector (dsn secret)",
                    "mongodb": "MongoDB Atlas Vector Search (dsn secret)",
                    "redis": "Redis query engine (URL or dsn secret)",
                    "pinecone": "Pinecone serverless (api_key secret)",
                }.get(backend, backend)
            )
        if node.type == "agenticmind.agents.tool_web_fetch":
            notes.append("Web Fetch: set AGENTICMIND_HTTP_USER_AGENT with contact details for sites like Wikipedia.")
        # memory nodes, not the Agent (which only declares the permission so it *can* be wired to memory)
        if manifest.permissions.memory and node.type != "agenticmind.agents.agent":
            notes.append("Memory nodes keep conversations and memories in process memory (lost on restart).")
        if manifest.permissions.artifacts and not artifact_ids(node.config):
            notes.append(f"'{node.id}' reads project files but none are selected, so it cannot run until you add some.")
        if manifest.can_suspend:
            notes.append(
                f"'{node.id}' pauses for a human (e.g. Human Approval): the exported API answers 409 there; "
                "durable approvals need the AgenticMind server."
            )
    return {
        "extras": extras,
        "secrets": sorted({ref for n in doc.nodes for ref in iter_secret_refs(n.config)}),
        "network": network,
        "local_models": sorted(set(local_models)),
        "services": sorted(set(services)),
        "notes": sorted(set(notes)),
    }


def compile_project(
    doc: ProjectDocument, registry: NodeRegistry, *, artifacts: Any = None, version: str = "0.1.0"
) -> CompileResult:
    """``artifacts``: optional ``ArtifactStore`` (``scoped(project_id)`` → reader) to bundle uploaded files."""
    plugin_nodes = [
        {"node_id": n.id, "message": f"'{n.type}' comes from the plugin package '{e.source}'"}
        for n in doc.nodes
        if (e := registry.get(n.type, n.version)) is not None and e.isolated
    ]
    if plugin_nodes:  # plugins run in AgenticMind's isolated workers; the standalone app has none yet
        raise CompileError(
            "Graphs that use installed node packages (plugins) cannot be exported as standalone apps yet",
            plugin_nodes,
        )
    endpoints = build_endpoints(doc, registry)
    req = analyze(doc, registry, endpoints)
    warnings: list[str] = list(req["notes"])
    cversion = compiler_version()
    name = slug(doc.project.name).replace("_", "-") or "agenticmind-app"
    title = doc.project.name or name
    description = doc.project.description or f"API generated from the AgenticMind project '{title}'."

    node_dists = sorted({e.source for n in doc.nodes if (e := registry.get(n.type, n.version)) is not None})
    dists = closure(node_dists)
    pinned, missing = pins(dists, req["extras"])
    warnings += [f"Could not pin '{m}' (not installed with the compiler); using its version range." for m in missing]

    files: dict[str, bytes] = {}

    def text(path: str, content: str) -> None:
        files[path] = content.encode("utf-8")

    # architecture: canonical IR (re-importable) and per-endpoint schemas
    text("architecture/graph.json", to_canonical_json(doc))
    schemas = {ep.name: {"request": request_schema(ep), "response": response_schema(ep)} for ep in endpoints}
    for ep in endpoints:
        text(f"architecture/schemas/{ep.name}.request.json", json.dumps(schemas[ep.name]["request"], indent=2) + "\n")
        text(f"architecture/schemas/{ep.name}.response.json", json.dumps(schemas[ep.name]["response"], indent=2) + "\n")
        text(f"examples/{ep.name}.json", json.dumps(example_request(ep), indent=2, ensure_ascii=False) + "\n")
    text("openapi.json", json.dumps(render.openapi(title, description, version, endpoints, schemas), indent=2) + "\n")

    # application source
    node_modules = [(d.name, d.module) for d in dists if d.name in node_dists]
    text("app/__init__.py", render.header(title, cversion))
    text("app/api/__init__.py", '"""HTTP layer."""\n')
    text("app/settings.py", render.SETTINGS)
    text("app/endpoints.py", render.endpoints_py(endpoints))
    text("app/runtime.py", render.runtime_py(node_modules))
    text("app/main.py", render.main_py(title, description, version))
    text("app/api/models.py", render.models_py(endpoints))
    text("app/api/security.py", render.SECURITY)
    text("app/api/routes.py", render.routes_py(endpoints))
    text("tests/__init__.py", "")
    text("tests/test_api.py", render.tests_py())
    text("tests/cases.json", json.dumps(render.test_cases(endpoints), indent=2, ensure_ascii=False) + "\n")

    # bundled uploads referenced by the graph (e.g. Document Loader files)
    bundled: list[str] = []
    wanted = sorted(set().union(*(artifact_ids(n.config) for n in doc.nodes)) if doc.nodes else set())
    if wanted and artifacts is not None:
        reader = artifacts.scoped(doc.project.id)
        for art in wanted:
            try:
                info = reader.info(art)
                blob = reader.read_bytes(art)
            except Exception:  # noqa: BLE001 - missing files are reported, not fatal
                warnings.append(f"Referenced file {art} was not found and is not included.")
                continue
            base = f"data/artifacts/{doc.project.id}/{art}"
            files[f"{base}/blob"] = blob
            text(f"{base}/meta.json", info.model_dump_json())
            bundled.append(info.filename)
    elif wanted:
        warnings.append(f"{len(wanted)} referenced file(s) were not bundled (no file store given).")

    # dependencies
    vendored = []
    for d in dists:
        ex = sorted(req["extras"].get(d.name, set()))
        vendored.append((d, ex))
    app_deps = [_pin(p) for p in APP_PACKAGES]
    dev_deps = [_pin(p) for p in DEV_PACKAGES]
    first_party = [f"{d.name}{'[' + ','.join(ex) + ']' if ex else ''}=={d.version}" for d, ex in vendored]
    third_party = sorted(set(pinned.values()) | set(app_deps), key=str.lower)
    text("pyproject.toml", _pyproject(name, version, description, first_party, third_party, dev_deps, vendored))
    text(
        "requirements.txt",
        "# pip install -r requirements.txt  (paths are relative to the project root)\n"
        + "".join(f"./vendor/{d.name}{'[' + ','.join(ex) + ']' if ex else ''}\n" for d, ex in vendored)
        + "".join(f"{p}\n" for p in third_party),
    )
    text("requirements-dev.txt", "-r requirements.txt\n" + "".join(f"{p}\n" for p in dev_deps))
    files.update(vendor_files(dists))
    files, checks = format_sources(files)
    if checks["lint"] not in ("ok", "skipped"):
        warnings.append(f"Generated code has lint findings: {checks['lint'][:300]}")
    if checks["formatter"].startswith("skipped"):
        warnings.append("Generated code was not formatted (ruff is not installed).")

    # deployment and docs
    has_data = any(p.startswith("data/") for p in files)
    text("Dockerfile", _dockerfile(has_data))
    text("compose.yaml", _compose(name))
    text(".env.example", _env_example(req["secrets"], req["notes"]))
    text(".gitignore", ".env\n.venv/\n__pycache__/\n.pytest_cache/\ndata/vectors/\ndata/models/\ndata/keyword/\n")
    text(".dockerignore", ".env\n.venv/\n__pycache__/\n.pytest_cache/\ntests/\n")

    node_versions: dict[str, dict[str, Any]] = {}
    for n in doc.nodes:
        entry = registry.get(n.type, n.version)
        key = f"{n.type}@{n.version}"
        rec = node_versions.setdefault(key, {"package": entry.source if entry else "?", "count": 0})
        rec["count"] += 1
    manifest = {
        "format": 1,
        "project": {"id": doc.project.id, "name": title, "version": version},
        "ir": {"version": doc.ir_version, "content_hash": content_hash(doc)},
        "compiler": {"name": COMPILER_NAME, "version": cversion, "checks": checks},
        "runtime": {"python": ">=3.11", "entrypoint": "uvicorn app.main:app"},
        "nodes": [{"type": k, **v} for k, v in sorted(node_versions.items())],
        "packages": [{"name": d.name, "version": d.version, "extras": ex} for d, ex in vendored],
        "endpoints": [
            {
                "name": ep.name,
                "method": "POST",
                "path": ep.path,
                "outputs": [r.key for r in ep.response],
                "inputs": [f.key for f in ep.request],
                "auth": ep.auth,
                "rate_limit_per_minute": ep.rate_limit_per_minute,
                "offline_test": ep.offline,
            }
            for ep in endpoints
        ],
        "requirements": {
            "secrets": req["secrets"],
            "network": req["network"],
            "local_models": req["local_models"],
            "services": req["services"],
            "bundled_files": sorted(bundled),
        },
        "warnings": sorted(set(warnings)),
    }
    text(
        "agenticmind.yaml",
        "# Build manifest written by the AgenticMind compiler. Regenerate instead of editing.\n"
        + _yaml(manifest)
        + "\n",
    )
    text("README.md", _readme(title, description, name, endpoints, manifest))
    return CompileResult(
        root=name,
        files=dict(sorted(files.items())),
        manifest=manifest,
        endpoints=endpoints,
        warnings=manifest["warnings"],
    )


def to_zip(result: CompileResult) -> bytes:
    """Deterministic ZIP: sorted entries, fixed timestamps and permissions."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, data in sorted(result.files.items()):
            info = zipfile.ZipInfo(f"{result.root}/{path}", date_time=_ZIP_DATE)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buf.getvalue()


def write_directory(result: CompileResult, target: Any) -> None:
    from pathlib import Path

    root = Path(target)
    for path, data in result.files.items():
        out = root / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)


# ---- static files -------------------------------------------------------------------------------
def _toml_list(items: list[str]) -> str:
    return "[\n" + "".join(f"    {json.dumps(i)},\n" for i in items) + "]" if items else "[]"


def _pyproject(name, version, description, first_party, third_party, dev_deps, vendored) -> str:
    sources = "".join(f'{d.name} = {{ path = "vendor/{d.name}" }}\n' for d, _ in vendored)
    return f"""[project]
name = {json.dumps(name)}
version = {json.dumps(version)}
description = {json.dumps(description)}
requires-python = ">=3.11"
dependencies = {_toml_list(first_party + third_party)}

[dependency-groups]
dev = {_toml_list(dev_deps)}

[tool.uv]
package = false

[tool.uv.sources]
{sources}
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]

[tool.ruff]
line-length = 120
target-version = "py311"
extend-exclude = ["vendor"]

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP"]
ignore = ["E501", "B008"]
"""


def _dockerfile(has_data: bool) -> str:
    data = "COPY data ./data\n" if has_data else ""
    return f"""# syntax=docker/dockerfile:1
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data
WORKDIR /app

COPY requirements.txt ./
COPY vendor ./vendor
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY architecture ./architecture
{data}RUN useradd --create-home --uid 10001 app && mkdir -p /app/data && chown -R app /app/data
USER app

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
"""


def _compose(name: str) -> str:
    return f"""services:
  {name}:
    build: .
    ports:
      - "8080:8080"
    env_file:
      - path: .env
        required: false
    volumes:
      - app-data:/app/data
    restart: unless-stopped

volumes:
  app-data: {{}}
"""


def _env_example(secrets: list[str], notes: list[str]) -> str:
    lines = [
        "# Copy to .env and fill in. Never commit .env.",
        "",
        "# Keys accepted in the X-API-Key header (comma-separated). Required for protected endpoints.",
        "APP_API_KEYS=",
    ]
    if secrets:
        lines += ["", '# Secrets referenced by the graph ({"$secret": NAME}); values stay out of the code.']
        lines += [f"{s}=" for s in secrets]
    if any("AGENTICMIND_HTTP_USER_AGENT" in n for n in notes):
        lines += [
            "",
            "# Web Fetch User-Agent with contact details",
            "# AGENTICMIND_HTTP_USER_AGENT=MyApp/1.0 (https://example.org; you@example.org)",
        ]
    lines += ["", "# Optional", "# DATA_DIR=data", "# MAX_CONCURRENCY=8", ""]
    return "\n".join(lines)


def _readme(title: str, description: str, name: str, endpoints: list[Endpoint], manifest: dict) -> str:
    req = manifest["requirements"]
    rows = "\n".join(
        f"| `POST {ep.path}` | {', '.join(f'`{f.key}`' for f in ep.request) or '-'} | "
        f"{', '.join(f'`{r.key}`' for r in ep.response)} | {'API key' if ep.auth == 'api_key' else 'none'} |"
        for ep in endpoints
    )
    first = endpoints[0]
    curl = (
        f"curl -X POST http://localhost:8080{first.path} \\\n  -H 'Content-Type: application/json' \\\n"
        + ("  -H 'X-API-Key: <one of APP_API_KEYS>' \\\n" if first.auth == "api_key" else "")
        + f"  -d @examples/{first.name}.json"
    )

    def bullets(items: list[str], empty: str) -> str:
        return "\n".join(f"- {i}" for i in items) if items else f"- {empty}"

    secrets = bullets([f"`{s}`" for s in req["secrets"]], "none")
    return f"""# {title}

{description}

Generated by the AgenticMind compiler {manifest["compiler"]["version"]} from IR
`{manifest["ir"]["content_hash"][:19]}…`. It runs on its own: the AgenticMind runtime and node packages are
vendored in `vendor/`, and nothing calls a hosted AgenticMind service.

## Endpoints

| Endpoint | Request fields | Response outputs | Auth |
|---|---|---|---|
{rows}

Interactive docs at `/docs` once running; the contract is in `openapi.json` and `architecture/schemas/`.

```bash
{curl}
```

## Run locally

```bash
cp .env.example .env          # set APP_API_KEYS and the secrets below
uv sync                       # or: python -m venv .venv && pip install -r requirements-dev.txt
uv run uvicorn app.main:app --port 8080
uv run pytest                 # contract tests
```

## Run with Docker

```bash
docker compose up --build     # http://localhost:8080/docs
```

## Configuration

- `APP_API_KEYS`: comma-separated keys accepted in `X-API-Key` (protected endpoints refuse requests until set).
- `DATA_DIR` (default `data`): uploaded files, local vector databases and model caches.
- Secrets used by the graph, read from the environment by name:
{secrets}

## Requirements and limitations

- Network access: {"yes (model providers or web tools)" if req["network"] else "no"}
- Local models:
{bullets(req["local_models"], "none")}
- Services:
{bullets(req["services"], "none")}
- Bundled files:
{bullets(req["bundled_files"], "none")}
- Notes:
{bullets(manifest["warnings"], "none")}
- The rate limit is per process and in memory; put a gateway in front for multi-instance deployments.

## Files

- `architecture/graph.json`: canonical AgenticMind IR; re-import it into AgenticMind to keep editing visually.
- `agenticmind.yaml`: build manifest (IR hash, compiler, node and package versions, requirements).
- `app/`: FastAPI application (`main.py`, `api/routes.py`, `api/models.py`, `runtime.py`).
- `vendor/`: pinned copies of the AgenticMind runtime and node packages this graph uses.
"""
