import json

from agenticmind_runtime.cli import main

HELLO = {
    "ir_version": "1.0",
    "project": {"id": "cli", "name": "CLI"},
    "nodes": [
        {"id": "name", "type": "agenticmind.core.manual_input", "version": "1.0.0", "config": {"key": "name"}},
        {"id": "out", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "who"}},
    ],
    "edges": [
        {"id": "e1", "kind": "data", "source": {"node": "name", "port": "value"},
         "target": {"node": "out", "port": "value"}}
    ],
}


def test_cli_validate_and_run(tmp_path, capsys):
    path = tmp_path / "g.json"
    path.write_text(json.dumps(HELLO), encoding="utf-8")
    assert main(["validate", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True
    assert main(["run", str(path), "-i", "name=Ada"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "succeeded" and out["outputs"] == {"who": "Ada"}


def test_cli_invalid(tmp_path, capsys):
    bad = dict(HELLO, edges=[])
    path = tmp_path / "g.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    assert main(["validate", str(path)]) == 1
    assert main(["run", str(path)]) == 1


def test_cli_runs_ingestion_with_data_dir(tmp_path, capsys):
    from agenticmind_runtime import LocalArtifactStore

    info = LocalArtifactStore(tmp_path / "data" / "artifacts").put_bytes("cli", "faq.md", b"# FAQ\n\nAnswers here.")
    graph = {
        "ir_version": "1.0",
        "project": {"id": "cli", "name": "CLI"},
        "nodes": [
            {"id": "load", "type": "agenticmind.data.document_loader", "version": "1.0.0",
             "config": {"files": [info.id]}},
            {"id": "out", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "docs"}},
        ],
        "edges": [
            {"id": "e1", "kind": "data", "source": {"node": "load", "port": "documents"},
             "target": {"node": "out", "port": "value"}}
        ],
    }
    path = tmp_path / "g.json"
    path.write_text(json.dumps(graph), encoding="utf-8")
    assert main(["run", str(path), "--data-dir", str(tmp_path / "data")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["outputs"]["docs"][0]["content"] == "# FAQ\n\nAnswers here."
