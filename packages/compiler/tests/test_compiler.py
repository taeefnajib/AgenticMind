"""Compiler: IR → standalone FastAPI project (layout, determinism, secrets, API spec, generated tests)."""

import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from agenticmind_compiler import CompileError, compile_project, to_zip, write_directory
from agenticmind_ir import content_hash, load_project
from agenticmind_node_sdk import ArtifactInfo
from agenticmind_registry import default_registry


def n(node_id, short, version="1.0.0", **config):
    t = short if short.count(".") >= 2 else f"agenticmind.core.{short}"
    return {"id": node_id, "type": t, "version": version, "config": config}


def d(eid, src, tgt):
    (a, b), (c, e) = src.split("."), tgt.split(".")
    return {"id": eid, "kind": "data", "source": {"node": a, "port": b}, "target": {"node": c, "port": e}}


def c(eid, src, tgt, branch):
    return {
        "id": eid,
        "kind": "control",
        "source": {"node": src, "port": branch},
        "target": {"node": tgt, "port": "trigger"},
    }


def hello(metadata=None):
    return load_project(
        {
            "ir_version": "1.0",
            "project": {"id": "prj_hello", "name": "Hello API", "description": "Greets people."},
            "nodes": [
                n("name", "manual_input", key="name", default="world", required=True),
                n("shout", "manual_input", key="shout", value_type="Boolean", default=False, required=False),
                n("tpl", "text_template", template="Hello, {{ text }}!"),
                n("out", "output", key="greeting"),
                n("count", "output", key="name_echo"),
            ],
            "edges": [
                d("e1", "name.value", "tpl.text"),
                d("e2", "tpl.text", "out.value"),
                d("e3", "name.value", "count.value"),
            ],
            "metadata": metadata or {},
        }
    )


@pytest.fixture(scope="module")
def registry():
    return default_registry()


def test_layout_manifest_and_endpoints(registry):
    r = compile_project(hello(), registry)
    files = r.files
    for path in (
        "agenticmind.yaml",
        "architecture/graph.json",
        "openapi.json",
        "app/main.py",
        "app/api/routes.py",
        "app/api/models.py",
        "app/runtime.py",
        "tests/test_api.py",
        "pyproject.toml",
        "requirements.txt",
        "Dockerfile",
        "compose.yaml",
        ".env.example",
        "README.md",
        "vendor/agenticmind-runtime/pyproject.toml",
    ):
        assert path in files, path
    assert content_hash(load_project(files["architecture/graph.json"].decode())) == content_hash(hello())
    assert r.manifest["ir"]["content_hash"] == content_hash(hello())
    assert {e.path: [f.key for f in e.request] for e in r.endpoints} == {
        "/greeting": ["name"],  # 'shout' is not needed by this output
        "/name-echo": ["name"],
        "/run": ["name"],
    }
    assert r.manifest["compiler"]["checks"]["lint"] == "ok"
    spec = json.loads(files["openapi.json"])
    assert spec["paths"]["/greeting"]["post"]["security"] == [{"ApiKey": []}]
    vendored = {p.split("/")[1] for p in files if p.startswith("vendor/")}
    assert "agenticmind-nodes-rag" not in vendored and {"agenticmind-nodes-core", "agenticmind-runtime"} <= vendored
    assert "agenticmind-api" not in vendored and "agenticmind-compiler" not in vendored


def test_zip_is_deterministic(registry):
    a = to_zip(compile_project(hello(), registry))
    b = to_zip(compile_project(hello(), registry))
    assert a == b
    names = zipfile.ZipFile(io.BytesIO(a)).namelist()
    assert names == sorted(names) and all(x.startswith("hello-api/") for x in names)


def test_secrets_are_referenced_never_embedded(registry, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-very-secret-value")
    doc = load_project(
        {
            "ir_version": "1.0",
            "project": {"id": "prj_llm", "name": "LLM"},
            "nodes": [
                n("q", "manual_input", key="question"),
                n(
                    "llm",
                    "agenticmind.models.llm",
                    "1.1.0",
                    provider="openrouter",
                    api_key={"$secret": "OPENROUTER_API_KEY"},
                ),
                n("out", "output", key="answer"),
            ],
            "edges": [d("e1", "q.value", "llm.prompt"), d("e2", "llm.text", "out.value")],
        }
    )
    r = compile_project(doc, registry)
    assert all(b"sk-or-very-secret-value" not in data for data in r.files.values())
    assert "OPENROUTER_API_KEY=" in r.files[".env.example"].decode()
    assert r.manifest["requirements"]["secrets"] == ["OPENROUTER_API_KEY"] and r.manifest["requirements"]["network"]
    assert r.endpoints[0].offline is False


def test_custom_api_definition_and_validation(registry):
    api = {
        "endpoints": [
            {
                "name": "greet",
                "path": "/v1/greet",
                "outputs": ["greeting"],
                "auth": "none",
                "rate_limit_per_minute": 5,
                "summary": "Say hello",
            }
        ]
    }
    r = compile_project(hello({"api": api}), registry)
    (ep,) = r.endpoints
    assert (ep.path, ep.auth, ep.rate_limit_per_minute, ep.summary) == ("/v1/greet", "none", 5, "Say hello")
    assert "security" not in json.loads(r.files["openapi.json"])["paths"]["/v1/greet"]["post"]
    bad = {"endpoints": [{"name": "x", "path": "/healthz", "outputs": ["nope"]}]}
    with pytest.raises(CompileError) as exc:
        compile_project(hello({"api": bad}), registry)
    assert {d["code"] for d in exc.value.diagnostics} == {"API_ENDPOINT_INVALID"}
    with pytest.raises(CompileError) as exc:
        compile_project(hello({"api": {"endpoints": [{"name": "x", "path": "/x", "outputs": ["nope"]}]}}), registry)
    assert "API_UNKNOWN_OUTPUT" in {d["code"] for d in exc.value.diagnostics}


def test_alternative_branches_share_an_endpoint(registry):
    doc = load_project(
        {
            "ir_version": "1.0",
            "project": {"id": "p", "name": "Branches"},
            "nodes": [
                n("x", "manual_input", key="x", value_type="Number"),
                n("big", "conditional", operator="greater_than", operand=5),
                n("yes", "output", key="verdict"),
                n("no", "output", key="verdict"),
            ],
            "edges": [
                d("e1", "x.value", "big.value"),
                d("e2", "x.value", "yes.value"),
                d("e3", "x.value", "no.value"),
                c("c1", "big", "yes", "true"),
                c("c2", "big", "no", "false"),
            ],
        }
    )
    (ep,) = compile_project(doc, registry).endpoints
    assert ep.path == "/verdict" and sorted(ep.targets) == ["no", "yes"] and [r.key for r in ep.response] == ["verdict"]


class FakeArtifacts:
    def scoped(self, project_id):
        assert project_id == "prj_files"
        return self

    def info(self, artifact_id):
        if artifact_id != "art_0123456789abcdef":
            raise LookupError(artifact_id)
        return ArtifactInfo(id=artifact_id, filename="guide.md", media_type="text/markdown", size=7, sha256="0" * 64)

    def read_bytes(self, artifact_id):
        return b"# Guide"


def test_referenced_files_are_bundled(registry):
    doc = load_project(
        {
            "ir_version": "1.0",
            "project": {"id": "prj_files", "name": "Files"},
            "nodes": [
                n("load", "agenticmind.data.document_loader", files=["art_0123456789abcdef", "art_ffffffffffffffff"]),
                n("out", "output", key="documents"),
            ],
            "edges": [d("e1", "load.documents", "out.value")],
        }
    )
    r = compile_project(doc, registry, artifacts=FakeArtifacts())
    assert r.files["data/artifacts/prj_files/art_0123456789abcdef/blob"] == b"# Guide"
    assert r.manifest["requirements"]["bundled_files"] == ["guide.md"]
    assert any("art_ffffffffffffffff" in w for w in r.warnings)
    assert r.endpoints[0].offline is True  # files are bundled, so the smoke test can run it


def test_generated_project_tests_pass(registry, tmp_path):
    """The export runs its own tests against its vendored packages only."""
    r = compile_project(hello(), registry)
    write_directory(r, tmp_path)
    vendor = [str(p) for p in sorted((tmp_path / "vendor").glob("*/src"))]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), *vendor])}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-W", "ignore::DeprecationWarning"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    assert " passed" in proc.stdout
    # and the vendored runtime really was the one imported
    check = subprocess.run(
        [sys.executable, "-c", "import agenticmind_runtime as m; print(m.__file__)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert Path(check.stdout.strip()).resolve().is_relative_to(tmp_path.resolve())
