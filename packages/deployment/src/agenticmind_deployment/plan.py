"""Deployment plans (ADR-0020): one pure function from (project revision, target, profile, settings) to
everything a person needs to confirm: compatibility checks, requirements, secrets, artifacts, permissions
and steps. The plan hash binds a confirmation to exactly what was shown."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from agenticmind_compiler import CompileResult, compile_project, to_zip
from agenticmind_ir import ProjectDocument
from agenticmind_registry import NodeRegistry

from .artifacts import API_KEYS, ArtifactOptions, dns_name, generate, secret_names
from .requirements import Requirements, derive_requirements
from .targets import PROFILES, TARGETS, Check, check

_ENV_KEY = __import__("re").compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class PlanError(ValueError):
    """Invalid plan input (unknown target/profile, bad env name)."""


@dataclass
class Plan:
    target: str
    target_name: str
    mode: str
    profile: str
    revision: int
    content_hash: str
    name: str
    requirements: Requirements
    checks: list[Check]
    secrets: list[dict[str, Any]]
    env: dict[str, str]
    files: dict[str, str]  # deploy/... artifacts (text), shown for review
    permissions: list[str]
    steps: list[str]
    plan_hash: str = ""
    compiled: CompileResult | None = field(default=None, repr=False)

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if c.severity == "blocker"]

    @property
    def deployable(self) -> bool:
        return not self.blockers

    def to_json(self, *, include_files: bool = True) -> dict[str, Any]:
        out = {
            "target": self.target,
            "target_name": self.target_name,
            "mode": self.mode,
            "profile": self.profile,
            "revision": self.revision,
            "content_hash": self.content_hash,
            "name": self.name,
            "requirements": self.requirements.to_json(),
            "checks": [c.to_json() for c in self.checks],
            "compatible": self.deployable,
            "secrets": self.secrets,
            "env": self.env,
            "artifacts": [{"path": p, "bytes": len(t.encode())} for p, t in self.files.items()],
            "permissions": self.permissions,
            "steps": self.steps,
            "plan_hash": self.plan_hash,
        }
        if include_files:
            out["files"] = self.files
        return out

    def bundle(self) -> bytes:
        """The compiled app plus ``deploy/`` artifacts as one deterministic ZIP."""
        assert self.compiled is not None
        files = {**self.compiled.files, **{p: t.encode("utf-8") for p, t in self.files.items()}}
        return to_zip(replace(self.compiled, files=dict(sorted(files.items()))))


def build_plan(
    doc: ProjectDocument,
    registry: NodeRegistry,
    *,
    revision: int,
    content_hash: str,
    target: str,
    profile: str = "development",
    env: dict[str, str] | None = None,
    image: str = "",
    artifacts: Any = None,
    secret_available: Callable[[str], bool] | None = None,
    local_status: tuple[bool, str] | None = None,
) -> Plan:
    """``secret_available`` answers whether this server can supply a secret (direct targets need it);
    ``local_status`` is ``(available, reason)`` for the local Docker deployer."""
    spec = TARGETS.get(target)
    if spec is None:
        raise PlanError(f"Unknown target {target!r}; choose one of {', '.join(TARGETS)}")
    if profile not in PROFILES:
        raise PlanError(f"Unknown profile {profile!r}; choose one of {', '.join(PROFILES)}")
    env = dict(env or {})
    for key in env:
        if not _ENV_KEY.match(key):
            raise PlanError(f"Environment variable names must look like MY_SETTING (got {key!r})")
    compiled = compile_project(doc, registry, artifacts=artifacts)
    req = derive_requirements(doc, registry, compiled)
    clash = sorted(set(env) & set(secret_names(req)))
    if clash:
        raise PlanError(f"{', '.join(clash)} are secrets: set them on the server, not as plain settings")
    name = dns_name(compiled.root)
    opts = ArtifactOptions(
        name=name, project_id=doc.project.id, revision=revision, profile=profile, image=image, env=env,
    )  # fmt: skip
    files = generate(target, spec.name, req, opts)
    checks = check(spec, req, profile)
    if not compiled.endpoints:
        checks.insert(0, Check("no-endpoints", "blocker", "No API endpoints", "Add an Output node to expose."))

    secrets: list[dict[str, Any]] = []
    for s in secret_names(req):
        provided = secret_available(s) if (secret_available and spec.mode == "direct") else None
        secrets.append({"name": s, "provided": provided, "purpose": "client API keys" if s == API_KEYS else "graph"})
    if spec.mode == "direct":
        missing = [s["name"] for s in secrets if s["provided"] is False]
        if missing:
            checks.insert(
                0,
                Check(
                    "secrets-missing",
                    "blocker",
                    "Secrets missing on this server",
                    f"{', '.join(missing)} must be set in the AgenticMind server's environment (.env); the local "
                    "deployment passes them to the container by name.",
                    f"Add {', '.join(missing)} to .env and restart the server."
                    + (" APP_API_KEYS is the key clients send as X-API-Key." if API_KEYS in missing else ""),
                ),
            )
        available, reason = local_status if local_status is not None else (False, "not checked")
        if not available:
            checks.insert(0, Check("local-docker", "blocker", "Local Docker deployment unavailable", reason))

    permissions, steps = _permissions_and_steps(spec.mode, target, spec.name, req, opts)
    plan = Plan(
        target=target, target_name=spec.name, mode=spec.mode, profile=profile, revision=revision,
        content_hash=content_hash, name=name, requirements=req, checks=checks, secrets=secrets, env=env,
        files=files, permissions=permissions, steps=steps, compiled=compiled,
    )  # fmt: skip
    plan.plan_hash = _hash(plan)
    return plan


def _hash(plan: Plan) -> str:
    material = {
        "target": plan.target,
        "profile": plan.profile,
        "revision": plan.revision,
        "content_hash": plan.content_hash,
        "env": plan.env,
        "files": {p: hashlib.sha256(t.encode()).hexdigest() for p, t in plan.files.items()},
        "checks": [(c.id, c.severity) for c in plan.checks],
    }
    return "plan_" + hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:24]


def _permissions_and_steps(
    mode: str, target: str, name: str, req: Requirements, o: ArtifactOptions
) -> tuple[list[str], list[str]]:
    if mode == "direct":
        return (
            [
                "Run Docker on this machine (build an image, start a container)",
                "Publish the API on a free port 8100-8199 of 127.0.0.1 only (not reachable from other machines)",
                f"Create or reuse the Docker volume am-{dns_name(o.project_id)}-data",
                "Pass these secrets to the container by name from the server environment: "
                + ", ".join(secret_names(req)),
                "Outbound network from the container" if req.network_egress else "No outbound network needed",
            ],
            [
                f"Compile revision {o.revision} and write the app with deploy/local/compose.yaml",
                "docker compose up --build (waits for the health check)",
                "Check /healthz from this machine",
                "Stop the previously running deployment of this project (kept for rollback)",
            ],
        )
    if mode == "guided":
        return (
            [
                "None on your infrastructure from AgenticMind: it only generates files.",
                f"You will need rights to push images and deploy to {name}, and to create secrets "
                f"({', '.join(secret_names(req))}).",
            ],
            [
                "Download the bundle (app + deploy/ artifacts)",
                f"Follow deploy/{_folder(target)}/DEPLOY.md: build and push the image, create secrets, apply",
                "Mark the deployment live here with the URL, so health checks and rollback appear in history",
            ],
        )
    return (
        ["None: plan only."],
        ["Download the bundle", f"Follow deploy/{_folder(target)}/PLAN.md"],
    )


def _folder(target: str) -> str:
    return {"docker_server": "docker-server", "cloud_run": "cloud-run"}.get(target, target.replace("_", "-"))
