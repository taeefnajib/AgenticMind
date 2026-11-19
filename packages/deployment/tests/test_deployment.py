"""Deployment Center core (ADR-0020): requirements, target checks, artifacts, plans."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
import yaml
from agenticmind_api.templates import load_template
from agenticmind_deployment import TARGETS, PlanError, build_plan
from agenticmind_ir import load_project
from agenticmind_registry import default_registry

REG = default_registry()
SECRET_VALUE = "fixture-secret-value-123"


def plan(template: str, target: str, profile: str = "production", **kw):
    kw.setdefault("secret_available", lambda name: True)
    kw.setdefault("local_status", (True, "Docker test"))
    return build_plan(
        load_project(load_template(template)), REG, revision=4, content_hash="sha256:x", target=target,
        profile=profile, **kw,
    )  # fmt: skip


def ids(p, severity):
    return {c.id for c in p.checks if c.severity == severity}


# ---- requirements -------------------------------------------------------------------------------
def test_requirements_reflect_the_graph():
    rag = plan("advanced-rag", "local").requirements
    assert rag.persistent_data and rag.local_models and rag.memory_mb >= 2048
    assert rag.secrets == ["OPENROUTER_API_KEY"] and rag.network_egress
    memory = plan("assistant-with-memory", "local").requirements
    assert memory.in_process_state and memory.longest_request_seconds == 600
    # an Agent that merely *declares* the memory permission is not stateful
    assert not plan("research-team", "local").requirements.in_process_state
    hello = plan("hello-pipeline", "local").requirements
    assert not hello.persistent_data and not hello.in_process_state and not hello.secrets


# ---- compatibility ------------------------------------------------------------------------------
def test_targets_check_what_they_cannot_provide():
    assert "persistent-data" in ids(plan("advanced-rag", "cloud_run"), "blocker")
    assert not plan("advanced-rag", "cloud_run").deployable
    assert plan("advanced-rag", "kubernetes").deployable
    assert "vercel-bundle" in ids(plan("advanced-rag", "vercel"), "blocker")
    # 600 s agent vs Azure's 240 s ingress timeout
    assert "request-duration" in ids(plan("assistant-with-memory", "azure_container_apps"), "warning")
    assert "single-replica" in ids(plan("assistant-with-memory", "cloud_run"), "warning")
    assert "approvals" in ids(plan("content-approval", "docker_server"), "warning")
    for target in TARGETS:  # every target states the facts it was checked against
        assert TARGETS[target].to_json()["verified_on"]


def test_direct_target_requires_docker_and_secrets_on_this_server():
    off = plan("hello-pipeline", "local", local_status=(False, "Local deployment is off"))
    assert "local-docker" in ids(off, "blocker")
    missing = plan("rag-answers", "local", secret_available=lambda name: name != "APP_API_KEYS")
    blocker = next(c for c in missing.checks if c.id == "secrets-missing")
    assert "APP_API_KEYS" in blocker.detail and "X-API-Key" in blocker.remedy
    assert {s["name"]: s["provided"] for s in missing.secrets}["APP_API_KEYS"] is False
    # guided targets cannot know what the remote side has, so they don't claim to
    guided = plan("rag-answers", "kubernetes", secret_available=lambda name: False)
    assert all(s["provided"] is None for s in guided.secrets) and guided.deployable


# ---- artifacts ----------------------------------------------------------------------------------
def test_artifacts_reference_secrets_by_name_only():
    for target in ("local", "docker_server", "kubernetes", "cloud_run"):
        p = plan("rag-answers", target, env={"LOG_LEVEL": "info"})
        text = "\n".join(p.files.values())
        assert "OPENROUTER_API_KEY" in text and "APP_API_KEYS" in text
        assert SECRET_VALUE not in text and ("sk-" + "or-") not in text  # no OpenRouter-style key


def test_compose_is_local_only_and_hardened_in_production():
    dev = yaml.safe_load(plan("hello-pipeline", "local", "development").files["deploy/local/compose.yaml"])
    app = dev["services"]["app"]
    assert app["ports"][0].startswith("127.0.0.1:") and "read_only" not in app
    assert dev["volumes"]["data"]["name"].startswith("am-")  # shared per project, survives rollback
    prod = yaml.safe_load(plan("hello-pipeline", "local").files["deploy/local/compose.yaml"])["services"]["app"]
    assert prod["read_only"] is True and prod["restart"] == "unless-stopped" and prod["tmpfs"] == ["/tmp"]
    assert "APP_API_KEYS" in prod["environment"]  # name only: compose passes the value through


def test_kubernetes_manifests():
    files = plan("advanced-rag", "kubernetes", "enterprise", image="ghcr.io/acme/rag").files
    dep = yaml.safe_load(files["deploy/kubernetes/deployment.yaml"])
    spec = dep["spec"]
    assert spec["replicas"] == 1 and spec["strategy"]["type"] == "Recreate"  # RWO volume
    container = spec["template"]["spec"]["containers"][0]
    assert container["image"] == "ghcr.io/acme/rag:rev4"
    assert container["securityContext"]["readOnlyRootFilesystem"] and container["securityContext"]["runAsNonRoot"]
    refs = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"] if "valueFrom" in e}
    assert set(refs) == {"APP_API_KEYS", "OPENROUTER_API_KEY"}
    kust = yaml.safe_load(files["deploy/kubernetes/kustomization.yaml"])
    assert set(kust["resources"]) == {"deployment.yaml", "service.yaml", "pvc.yaml", "networkpolicy.yaml"}
    netpol = yaml.safe_load(files["deploy/kubernetes/networkpolicy.yaml"])
    assert {"port": 443, "protocol": "TCP"} in [p for rule in netpol["spec"]["egress"] for p in rule.get("ports", [])]
    stateless = yaml.safe_load(plan("ticket-triage", "kubernetes").files["deploy/kubernetes/deployment.yaml"])
    assert stateless["spec"]["replicas"] == 2 and stateless["spec"]["strategy"]["type"] == "RollingUpdate"


def test_cloud_run_service():
    svc = yaml.safe_load(plan("assistant-with-memory", "cloud_run").files["deploy/cloud-run/service.yaml"])
    tpl = svc["spec"]["template"]
    assert tpl["metadata"]["annotations"]["autoscaling.knative.dev/maxScale"] == "1"
    assert tpl["spec"]["timeoutSeconds"] == 630  # 600 s agent + margin, within 300..3600
    env = {e["name"]: e for e in tpl["spec"]["containers"][0]["env"]}
    assert env["OPENROUTER_API_KEY"]["valueFrom"]["secretKeyRef"] == {"name": "OPENROUTER_API_KEY", "key": "latest"}


def test_plan_only_targets_say_so():
    p = plan("rag-answers", "aws_ecs")
    assert p.mode == "plan" and list(p.files) == ["deploy/aws-ecs/PLAN.md"]
    assert "plan-only" in ids(p, "info")


# ---- plans --------------------------------------------------------------------------------------
def test_plan_hash_binds_what_was_reviewed():
    a, b = plan("rag-answers", "kubernetes"), plan("rag-answers", "kubernetes")
    assert a.plan_hash == b.plan_hash
    assert plan("rag-answers", "kubernetes", env={"X_MODE": "1"}).plan_hash != a.plan_hash
    assert plan("rag-answers", "kubernetes", "enterprise").plan_hash != a.plan_hash
    assert plan("rag-answers", "docker_server").plan_hash != a.plan_hash


def test_bundle_is_deterministic_and_complete():
    p = plan("rag-answers", "docker_server")
    data = p.bundle()
    assert data == plan("rag-answers", "docker_server").bundle()
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert any(n.endswith("deploy/docker-server/compose.yaml") for n in names)
    assert any(n.endswith("app/main.py") for n in names) and any(n.endswith("Dockerfile") for n in names)
    assert json.loads(json.dumps(p.to_json()))["plan_hash"] == p.plan_hash


@pytest.mark.parametrize(
    ("env", "message"),
    [({"lower": "x"}, "MY_SETTING"), ({"OPENROUTER_API_KEY": "x"}, "are secrets")],
)
def test_plan_rejects_bad_settings(env, message):
    with pytest.raises(PlanError, match=message):
        plan("rag-answers", "docker_server", env=env)
    with pytest.raises(PlanError, match="Unknown target"):
        plan("rag-answers", "mainframe")
