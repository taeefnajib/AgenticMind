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
