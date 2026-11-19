"""Plugin isolation (ADR-0021): a third-party package runs in a worker process with only what it was given."""

from __future__ import annotations

import asyncio
import os
import sys
import zipfile
from pathlib import Path

import pytest
from agenticmind_ir import load_project
from agenticmind_node_sdk import cli as sdk_cli
from agenticmind_plugins import PluginError, describe, inspect_wheel, install, requested_permissions
from agenticmind_plugins.testing import build_wheel, install_isolated
from agenticmind_registry import TrustLevel, default_registry
from agenticmind_runtime import (
    Engine,
    InMemoryObserver,
    MappingSecretResolver,
    RunStatus,
    compile_plan,
    validate_document,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    root = tmp_path_factory.mktemp("probe")
    wheel = build_wheel(FIXTURES / "probe-nodes", root / "dist")
    return {d.manifest.type: d for d in install_isolated(wheel, root)}, root, wheel


def registry_with(defs, *, permitted=True):
    reg = default_registry()
    for d in defs:
        reg.register(d, trust=TrustLevel.COMMUNITY, source="probe-nodes", isolated=True, permitted=permitted)
    return reg


def doc_with(node, config=None, output=None, wire_input=False):
    nodes = [{"id": "n", "type": node, "version": "1.0.0", "config": config or {}}]
    edges = []
    if wire_input:
        nodes.append(
            {
                "id": "q",
                "type": "agenticmind.core.manual_input",
                "version": "1.0.0",
                "config": {"key": "v", "value_type": "JSON"},
            }
        )
        edges.append(
            {
                "id": "e1",
                "kind": "data",
                "source": {"node": "q", "port": "value"},
                "target": {"node": "n", "port": "value"},
            }
        )
    if output:
        nodes.append({"id": "o", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "out"}})
        edges.append(
            {
                "id": "e2",
                "kind": "data",
                "source": {"node": "n", "port": output},
                "target": {"node": "o", "port": "value"},
            }
        )
    return load_project({"ir_version": "1.0", "project": {"id": "p", "name": "x"}, "nodes": nodes, "edges": edges})


def run(defs, doc, inputs=None, secrets=None):
    sec = MappingSecretResolver(secrets or {})
    obs = InMemoryObserver()
    result = asyncio.run(
        Engine(observer=obs, secrets=sec, retry_backoff_seconds=0.01).run(
            compile_plan(doc, registry_with(defs.values()), secrets=sec), inputs=inputs or {}
        )
    )
    return result, obs


# ---- the boundary -------------------------------------------------------------------------------
def test_worker_sees_no_host_secrets_and_runs_in_another_process(probe, monkeypatch):
    defs, _, _ = probe
    monkeypatch.setenv("AM_PROBE_HOST_SECRET", "fixture-host-secret-123")
    result, obs = run(defs, doc_with("probe.env.inspect", output="report"))
    assert result.status == RunStatus.SUCCEEDED, result.error
    report = result.outputs["out"]
    assert report["pid"] != os.getpid()
    assert report["host_secret"] is None and "AM_PROBE_HOST_SECRET" not in report["env_keys"]
    assert not report["core_loaded"]
    assert "probe_nodes" not in sys.modules  # never imported by the host
    assert any(getattr(e, "data", None) and e.data.get("type") == "probe" for e in obs.events)  # events replayed


def test_network_is_refused_without_the_permission(probe):
    defs, _, _ = probe
    result, _ = run(defs, doc_with("probe.net.connect"))
    assert result.status == RunStatus.FAILED and result.error.code == "PLUGIN_NETWORK_DENIED"


def test_only_the_nodes_own_secret_reaches_the_worker(probe):
    defs, _, _ = probe
    result, obs = run(
        defs,
        doc_with("probe.secret.read", config={"api_key": {"$secret": "PROBE_KEY"}}, output="report"),
        secrets={"PROBE_KEY": "fixture-key-12345", "OTHER_SECRET": "fixture-other"},
    )
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert result.outputs["out"] == {"own_length": 17, "other": "SecretAccessError"}
    dumped = str([e.model_dump(mode="json") for e in obs.events])
    assert "fixture-key-12345" not in dumped and "fixture-other" not in dumped


def test_filesystem_permission_gives_a_package_storage_dir(probe):
    defs, root, _ = probe
    result, _ = run(defs, doc_with("probe.fs.write"))
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert (root / "storage" / "notes" / "n.txt").read_text() == "hello"


@pytest.mark.parametrize(
    ("how", "code"),
    [("node_error", "PROBE_FAILED"), ("crash", "PLUGIN_CRASHED"), ("not_json", "PLUGIN_OUTPUT_NOT_JSON"),
     ("exit", "PLUGIN_CRASHED")],
)  # fmt: skip
def test_crashes_and_errors_are_contained(probe, how, code):
    defs, _, _ = probe
    result, _ = run(defs, doc_with("probe.x.fail", config={"how": how}))
    assert result.status == RunStatus.FAILED and result.error.code == code, result.error


def test_timeout_kills_the_worker(probe):
    defs, _, _ = probe
    result, _ = run(defs, doc_with("probe.slow.sleep"))
    assert result.status == RunStatus.FAILED and result.error.code in ("PLUGIN_TIMEOUT", "NODE_TIMEOUT")


def test_inputs_and_outputs_cross_the_boundary_as_json(probe):
    defs, _, _ = probe
    value = {"nested": [1, 2.5, "three", None, True]}
    result, _ = run(defs, doc_with("probe.x.echo", output="value", wire_input=True), inputs={"v": value})
    assert result.status == RunStatus.SUCCEEDED and result.outputs["out"] == value


def test_unapproved_package_is_refused_by_validation(probe):
    defs, _, _ = probe
    report = validate_document(doc_with("probe.x.echo"), registry_with(defs.values(), permitted=False))
    assert "NODE_NOT_PERMITTED" in [d.code.value for d in report.diagnostics]


def test_plugin_config_is_validated_against_its_schema(probe):
    defs, _, _ = probe
    report = validate_document(doc_with("probe.x.fail", config={"how": 3}), registry_with(defs.values()))
    bad = [d for d in report.diagnostics if d.code.value == "CONFIG_INVALID"]
    assert bad and bad[0].path == "config.how"


def test_requested_permissions(probe):
    _, root, _ = probe
    target = next((root / "packages").iterdir())
    assert requested_permissions(describe(target, workdir=root / "w2")) == ["credentials", "filesystem"]


# ---- packages -----------------------------------------------------------------------------------
def _fake_wheel(path: Path, name: str) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            f"{name.replace('-', '_')}-1.0.dist-info/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
        )
    return path


def test_wheel_inspection_rejects_what_cannot_be_a_node_package(tmp_path, probe):
    _, _, wheel = probe
    info = inspect_wheel(wheel)
    assert info.name == "probe-nodes" and info.requires == ["agenticmind-node-sdk"]
    bad = tmp_path / "x.zip"
    bad.write_bytes(b"nope")
    cases = [
        (bad, "PLUGIN_NOT_A_WHEEL"),
        (_fake_wheel(tmp_path / "plain-1.0-py3-none-any.whl", "plain"), "PLUGIN_NO_NODES"),
        (_fake_wheel(tmp_path / "agenticmind_evil-1.0-py3-none-any.whl", "agenticmind-evil"), "PLUGIN_RESERVED_NAME"),
    ]
    for path, code in cases:
        with pytest.raises(PluginError) as exc:
            inspect_wheel(path)
        assert exc.value.code == code


def test_reserved_node_namespace_is_refused(tmp_path):
    pkg = tmp_path / "sneaky-nodes"
    assert sdk_cli.main(["new", str(pkg), "--namespace", "sneaky"]) == 0
    nodes_py = pkg / "src" / "sneaky_nodes" / "nodes.py"
    nodes_py.write_text(nodes_py.read_text().replace('"sneaky.text.word_stats"', '"agenticmind.text.word_stats2"'))
    errors, _ = sdk_cli.validate(pkg)
    assert any("reserved" in e for e in errors)
    wheel = build_wheel(pkg, tmp_path / "dist")
    target = install(wheel, inspect_wheel(wheel), tmp_path / "plugins")
    with pytest.raises(PluginError) as exc:
        describe(target, workdir=tmp_path / "w")
    assert exc.value.code == "PLUGIN_RESERVED_NAMESPACE"


def edge(i, src, dst):
    (a, b), (c, d) = src.split("."), dst.split(".")
    return {"id": i, "kind": "data", "source": {"node": a, "port": b}, "target": {"node": c, "port": d}}


# ---- the SDK workflow end to end ----------------------------------------------------------------
def test_sdk_new_validate_test_pack_install_run(tmp_path):
    pkg = tmp_path / "acme-text-nodes"
    assert sdk_cli.main(["new", str(pkg), "--namespace", "acme"]) == 0
    assert sdk_cli.validate(pkg) == ([], [])
    assert sdk_cli.main(["test", str(pkg)]) == 0  # the scaffold ships passing tests
    import subprocess

    for name in ("acme-text-nodes", "zeta-nodes"):  # module names sorting before/after agenticmind_*
        target = pkg if name == "acme-text-nodes" else tmp_path / name
        if name != "acme-text-nodes":
            assert sdk_cli.main(["new", str(target)]) == 0
        lint = subprocess.run([sys.executable, "-m", "ruff", "check", "--no-cache", str(target)], capture_output=True)
        assert lint.returncode == 0, lint.stdout.decode()  # a fresh package is lint-clean
    wheel = build_wheel(pkg, tmp_path / "dist")
    defs = {d.manifest.type: d for d in install_isolated(wheel, tmp_path / "root")}
    assert set(defs) == {"acme.text.word_stats"}
    doc = load_project(
        {
            "ir_version": "1.0",
            "project": {"id": "p", "name": "x"},
            "nodes": [
                {"id": "q", "type": "agenticmind.core.manual_input", "version": "1.0.0", "config": {"key": "text"}},
                {"id": "n", "type": "acme.text.word_stats", "version": "1.0.0", "config": {"top_n": 1}},
                {"id": "o", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "summary"}},
            ],
            "edges": [
                edge("e1", "q.value", "n.text"),
                edge("e2", "n.summary", "o.value"),
            ],
        }
    )  # fmt: skip
    result, _ = run(defs, doc, inputs={"text": "Graphs run. Graphs evaluate. Graphs deploy!"})
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert result.outputs["summary"] == "6 words in 3 sentence(s); most frequent: graphs"


def test_relative_plugin_paths_work(tmp_path, monkeypatch, probe):
    # found live: the server's data dir is relative, while the worker runs in its own directory
    _, root, _ = probe
    target = next((root / "packages").iterdir())
    monkeypatch.chdir(target.parent)
    nodes = describe(Path(target.name), workdir=Path("rel-work"))
    assert any(n["manifest"]["type"] == "probe.x.echo" for n in nodes)
