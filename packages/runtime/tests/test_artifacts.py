import io

import pytest
from agenticmind_ir import load_project
from agenticmind_node_sdk import ArtifactNotFoundError
from agenticmind_runtime import ArtifactError, Engine, InMemoryObserver, LocalArtifactStore, RunStatus, compile_plan
from agenticmind_runtime.artifacts import sanitize_filename


@pytest.fixture
def store(tmp_path):
    return LocalArtifactStore(tmp_path / "artifacts", max_bytes=1024)


def test_put_list_read_delete(store):
    info = store.put_bytes("p1", "notes.md", b"# hello")
    assert info.id.startswith("art_") and info.media_type == "text/markdown" and info.size == 7
    assert store.read_bytes("p1", info.id) == b"# hello"
    assert [a.id for a in store.list("p1")] == [info.id]
    assert store.list("other") == []
    with pytest.raises(ArtifactNotFoundError):
        store.info("other", info.id)  # project-scoped
    assert store.delete("p1", info.id) and not store.delete("p1", info.id)
    assert store.list("p1") == []


@pytest.mark.parametrize(
    "name,data,code",
    [
        ("evil.exe", b"MZ", "ARTIFACT_TYPE_NOT_ALLOWED"),
        ("empty.txt", b"", "ARTIFACT_EMPTY"),
        ("big.txt", b"x" * 2000, "ARTIFACT_TOO_LARGE"),
    ],
)
def test_rejections_leave_nothing_behind(store, name, data, code):
    with pytest.raises(ArtifactError) as exc:
        store.put_stream("p1", name, io.BytesIO(data))
    assert exc.value.code == code
    assert store.list("p1") == []


def test_ids_cannot_escape_the_store(store):
    with pytest.raises(ArtifactNotFoundError):
        store.read_bytes("p1", "../../etc/passwd")
    with pytest.raises(ArtifactError):
        store.list("../outside")
    assert sanitize_filename("..\\..\\secret/../report.pdf") == "report.pdf"
    assert sanitize_filename("\x00") == "file"


async def test_engine_gives_store_only_to_permitted_nodes(store, registry):
    info = store.put_bytes("p", "faq.txt", b"Q: refunds? A: within 30 days.")
    doc = load_project(
        {
            "ir_version": "1.0",
            "project": {"id": "p", "name": "P"},
            "nodes": [
                {
                    "id": "load",
                    "type": "agenticmind.data.document_loader",
                    "version": "1.0.0",
                    "config": {"files": [info.id]},
                },
                {
                    "id": "chunk",
                    "type": "agenticmind.rag.chunker",
                    "version": "1.0.0",
                    "config": {"chunk_size": 6, "chunk_overlap": 0},
                },
                {"id": "out", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "chunks"}},
            ],
            "edges": [
                {
                    "id": "e1",
                    "kind": "data",
                    "source": {"node": "load", "port": "documents"},
                    "target": {"node": "chunk", "port": "documents"},
                },
                {
                    "id": "e2",
                    "kind": "data",
                    "source": {"node": "chunk", "port": "chunks"},
                    "target": {"node": "out", "port": "value"},
                },
            ],
        }
    )
    obs = InMemoryObserver()
    result = await Engine(observer=obs, artifacts=store).run(compile_plan(doc, registry))
    assert result.status == RunStatus.SUCCEEDED, result.error
    chunks = result.outputs["chunks"]
    assert [c["content"] for c in chunks] == ["Q: refunds?", "A: within 30 days."]
    assert any(e.data.get("type") == "document_loaded" for e in obs.events)

    # same graph without a store configured fails cleanly
    result = await Engine().run(compile_plan(doc, registry))
    assert result.status == RunStatus.FAILED and result.error.code == "ARTIFACT_ACCESS_DENIED"
