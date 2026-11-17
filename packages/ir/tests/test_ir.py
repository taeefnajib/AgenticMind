import json

import pytest
from agenticmind_ir import (
    IRLoadError,
    ProjectDocument,
    content_hash,
    ir_json_schema,
    iter_secret_refs,
    load_project,
    register_migration,
    secret_ref,
    to_canonical_json,
)
from agenticmind_ir.migrations import unregister_migration
from agenticmind_ir.schema import published_schema_path, render_schema

SAMPLE = {
    "ir_version": "1.0",
    "project": {"id": "p1", "name": "support-rag"},
    "runtime": {"engine": "agenticmind", "profile": "development"},
    "nodes": [
        {"id": "load", "type": "agenticmind.data.file_loader", "version": "1.0.0", "config": {"x": None}},
        {
            "id": "chunk",
            "type": "agenticmind.rag.chunker",
            "version": "1.0.0",
            "config": {"strategy": "recursive", "chunk_size": 700},
        },
    ],
    "edges": [
        {
            "id": "edge_1",
            "kind": "data",
            "source": {"node": "load", "port": "documents"},
            "target": {"node": "chunk", "port": "documents"},
        }
    ],
}


def test_roundtrip_preserves_content():
    doc = load_project(SAMPLE)
    again = load_project(json.loads(to_canonical_json(doc)))
    assert to_canonical_json(again) == to_canonical_json(doc)
    assert {n.id: n for n in again.nodes} == {n.id: n for n in doc.nodes}
    # explicit null in user config survives canonicalisation
    assert again.node("load").config == {"x": None}


def test_canonical_json_is_order_independent():
    a = load_project(SAMPLE)
    reordered = dict(SAMPLE, nodes=list(reversed(SAMPLE["nodes"])))
    b = load_project(reordered)
    assert to_canonical_json(a) == to_canonical_json(b)
    assert content_hash(a) == content_hash(b)


def test_unknown_fields_rejected():
    bad = dict(SAMPLE, surprise=True)
    with pytest.raises(IRLoadError) as exc:
        load_project(bad)
    assert exc.value.issues[0]["code"] == "IR_SCHEMA_VIOLATION"


def test_invalid_json_reports_code():
    with pytest.raises(IRLoadError) as exc:
        load_project("{not json")
    assert exc.value.issues[0]["code"] == "IR_INVALID_JSON"


def test_bad_node_type_rejected():
    bad = json.loads(json.dumps(SAMPLE))
    bad["nodes"][0]["type"] = "NotAType"
    with pytest.raises(IRLoadError) as exc:
        load_project(bad)
    assert any(i["path"].startswith("nodes.0.type") for i in exc.value.issues)


def test_unsupported_version():
    with pytest.raises(IRLoadError) as exc:
        load_project(dict(SAMPLE, ir_version="9.9"))
    assert exc.value.issues[0]["code"] == "IR_UNSUPPORTED_VERSION"


def test_migration_chain_preserves_config():
    @register_migration("0.9", "1.0")
    def _upgrade(doc):
        doc["project"] = {"id": doc.pop("project_id"), "name": doc.pop("project_name")}
        return doc

    try:
        old = {
            "ir_version": "0.9",
            "project_id": "legacy",
            "project_name": "Legacy",
            "nodes": SAMPLE["nodes"],
            "edges": SAMPLE["edges"],
        }
        doc = load_project(old)
        assert doc.project.id == "legacy"
        assert doc.node("chunk").config["chunk_size"] == 700
    finally:
        unregister_migration("0.9")


def test_secret_refs():
    ref = secret_ref("OPENAI_API_KEY")
    assert list(iter_secret_refs({"a": ref, "b": [ref, {"c": 1}]})) == ["OPENAI_API_KEY", "OPENAI_API_KEY"]
    with pytest.raises(ValueError):
        secret_ref("lowercase")


def test_published_schema_matches_models():
    """The committed JSON Schema must be regenerated when models change (scripts/generate_schemas.py)."""
    assert published_schema_path().read_text(encoding="utf-8") == render_schema()


def test_schema_has_identity():
    schema = ir_json_schema()
    assert schema["$id"].endswith("ir-v1.0.schema.json")
    assert "nodes" in schema["properties"]


def test_default_document():
    doc = ProjectDocument.model_validate({"project": {"id": "x", "name": "X"}})
    assert doc.ir_version == "1.0" and doc.nodes == []
