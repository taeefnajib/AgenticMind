"""Deployment targets and their capability checks (ADR-0020).

Each target states what it can provide (persistent disk, request duration, single-replica pinning, ...) and
checks a project's ``Requirements`` against it. Vendor limits are cited with the page they come from and the
date they were verified; they are deliberately few, and conservative.

``mode``:
* ``direct``: AgenticMind deploys it itself (today only Local Docker, opt-in).
* ``guided``: AgenticMind generates the artifacts and the exact commands; you run them.
* ``plan``: compatibility checks and a written plan only; artifacts are not generated yet.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .requirements import Requirements

Severity = Literal["blocker", "warning", "info"]
VERIFIED_ON = "2026-11-19"


@dataclass
class Check:
    id: str
    severity: Severity
    title: str
    detail: str
    remedy: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Fact:
    text: str
    source: str


@dataclass
class Target:
    id: str
    name: str
    mode: Literal["direct", "guided", "plan"]
    summary: str
    persistent_disk: str  # "yes" | "volume" | "no"
    max_request_seconds: float | None  # None = no platform limit
    default_request_seconds: float | None
    facts: list[Fact] = field(default_factory=list)
    extra: Callable[[Requirements, str], list[Check]] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "mode": self.mode,
            "summary": self.summary,
            "persistent_disk": self.persistent_disk,
            "max_request_seconds": self.max_request_seconds,
            "facts": [asdict(f) for f in self.facts],
            "verified_on": VERIFIED_ON,
        }


def _fmt(seconds: float) -> str:
    return f"{seconds / 60:g} min" if seconds >= 120 else f"{seconds:g} s"


def check(target: Target, req: Requirements, profile: str) -> list[Check]:
    """Compatibility checks for one target and profile; no blockers means it can be deployed."""
    out: list[Check] = []
    # persistent data
    if req.persistent_data:
        reasons = "; ".join(req.persistent_data)
        if target.persistent_disk == "no":
            out.append(
                Check(
                    "persistent-data",
                    "blocker",
                    "Needs a persistent data directory",
                    f"{reasons}. {target.name} has no persistent local disk, so the index would be lost whenever "
                    "an instance stops, and each instance would have its own copy.",
                    "Use an external vector store (Qdrant server, pgvector or Pinecone) with a URL/dsn secret, "
                    "or choose a target with volumes.",
                )
            )
        elif target.persistent_disk == "volume":
            out.append(
                Check(
                    "persistent-data",
                    "info",
                    "Uses a persistent volume for DATA_DIR",
                    f"{reasons}. The generated artifacts mount a volume at /app/data.",
                )
            )
    # in-process state → single replica
    if req.in_process_state:
        out.append(
            Check(
                "single-replica",
                "warning",
                "Must run as one replica",
                "; ".join(req.in_process_state) + ". With several replicas each would have different state, "
                "and a restart loses it.",
                "The artifacts pin one replica. For durable memory, run the graph on the AgenticMind server.",
            )
        )
    # request duration
    if target.max_request_seconds is not None and req.longest_request_seconds > target.max_request_seconds:
        out.append(
            Check(
                "request-duration",
                "warning",
                f"Requests may outlast {target.name}'s {_fmt(target.max_request_seconds)} limit",
                f"{req.slowest_node} may run for up to {_fmt(req.longest_request_seconds)}; "
                f"{target.name} ends requests after {_fmt(target.max_request_seconds)}.",
                "Lower that node's limits (max turns, timeouts) or choose a target without a request limit.",
            )
        )
    elif target.default_request_seconds is not None and req.longest_request_seconds > target.default_request_seconds:
        out.append(
            Check(
                "request-duration",
                "info",
                "Request timeout raised",
                f"{req.slowest_node} may run for up to {_fmt(req.longest_request_seconds)}, above the "
                f"{_fmt(target.default_request_seconds)} default; the artifacts set a longer timeout.",
            )
        )
    # humans in the loop
    if req.pauses_for_humans:
        out.append(
            Check(
                "approvals",
                "warning",
                "Human approval steps cannot pause in an exported app",
                f"{', '.join(req.pauses_for_humans)} pause for a person; the exported API answers 409 there.",
                "Keep approval flows on the AgenticMind server, or remove the pause from the deployed graph.",
            )
        )
    # local models
    if req.local_models:
        out.append(
            Check(
                "local-models",
                "info",
                "Downloads local models on first use",
                f"{'; '.join(req.local_models)}. Needs about {req.memory_mb} MB of memory and egress to the "
                "model hub; the first request after a cold start is slow.",
            )
        )
    # secrets
    if req.secrets:
        out.append(
            Check(
                "secrets",
                "info",
                f"{len(req.secrets)} secret(s) to provide",
                ", ".join(req.secrets) + " are referenced by name and must be set on the target; their values "
                "are never written into the artifacts.",
            )
        )
    # the exported app refuses every request (503) until APP_API_KEYS is set: fail closed in every profile
    out.append(
        Check(
            "api-keys",
            "info",
            "Clients must send an API key",
            "APP_API_KEYS (comma-separated) must be set on the target; requests without a matching X-API-Key "
            "header are refused, and without any keys the API answers 503.",
        )
    )
    if target.extra is not None:
        out.extend(target.extra(req, profile))
    return out


def _vercel_extra(req: Requirements, profile: str) -> list[Check]:
    out = [
        Check(
            "vercel-functions",
            "warning",
            "Not a fit for the API as a whole",
            "The exported app is a long-running FastAPI service with an on-disk data directory; Vercel Functions "
            "are short-lived and have no persistent filesystem. Vercel suits a web frontend that calls the API "
            "hosted elsewhere.",
            "Host the API on Cloud Run, a Docker server or Kubernetes and point a Vercel frontend at it.",
        )
    ]
    if req.local_models:
        out.append(
            Check(
                "vercel-bundle",
                "blocker",
                "Local models do not fit a function bundle",
                "Python functions are limited to 500 MB uncompressed (and 4 GB memory on Pro); local embedding or "
                "reranking models are downloaded at runtime into a non-persistent filesystem on every cold start.",
                "Use an API embedding model (e.g. OpenRouter) or another target.",
            )
        )
    return out


def _azure_extra(req: Requirements, profile: str) -> list[Check]:
    return [
        Check(
            "plan-only",
            "info",
            "Artifacts not generated yet",
            "AgenticMind checks compatibility and writes the plan; Container Apps manifests are a later step. "
            "Use the generated Docker image with `az containerapp up` meanwhile.",
        )
    ]


def _aws_extra(req: Requirements, profile: str) -> list[Check]:
    return [
        Check(
            "plan-only",
            "info",
            "Artifacts not generated yet",
            "AgenticMind checks compatibility and writes the plan; ECS task definitions are a later step.",
        ),
        Check(
            "alb-idle-timeout",
            "info" if req.longest_request_seconds <= 60 else "warning",
            "Load balancer idle timeout",
            f"An Application Load Balancer closes idle connections after 60 s by default (configurable 1-4000 s); "
            f"requests here may take up to {_fmt(req.longest_request_seconds)}.",
            "Set idle_timeout.timeout_seconds above the longest request." if req.longest_request_seconds > 60 else "",
        ),
    ]


TARGETS: dict[str, Target] = {
    t.id: t
    for t in [
        Target(
            "local",
            "Local Docker",
            "direct",
            "Builds the app image and runs it on this machine with Docker Compose; health-checked, with rollback.",
            persistent_disk="volume",
            max_request_seconds=None,
            default_request_seconds=None,
        ),
        Target(
            "docker_server",
            "Docker server",
            "guided",
            "Docker Compose for your own server: restart policy, health check, log rotation, data volume.",
            persistent_disk="volume",
            max_request_seconds=None,
            default_request_seconds=None,
        ),
        Target(
            "kubernetes",
            "Kubernetes",
            "guided",
            "Kustomize manifests: Deployment, Service, PersistentVolumeClaim, Secret reference, probes.",
            persistent_disk="volume",
            max_request_seconds=None,
            default_request_seconds=None,
            facts=[
                Fact("Ingress controllers have their own read timeouts (often 60 s); raise them for long requests.", "")
            ],
        ),
        Target(
            "cloud_run",
            "Google Cloud Run",
            "guided",
            "A Cloud Run service (Knative YAML) with secrets from Secret Manager, plus the gcloud commands.",
            persistent_disk="no",
            max_request_seconds=3600,
            default_request_seconds=300,
            facts=[
                Fact(
                    "Request timeout: 5 min by default, at most 60 min.",
                    "https://docs.cloud.google.com/run/docs/configuring/request-timeout",
                ),
                Fact(
                    "The filesystem is in-memory and not persistent; idle instances can be shut down at any time.",
                    "https://docs.cloud.google.com/run/docs/container-contract",
                ),
            ],
        ),
        Target(
            "aws_ecs",
            "AWS ECS on Fargate",
            "plan",
            "Compatibility checks and a deployment plan; task definitions come later.",
            persistent_disk="volume",
            max_request_seconds=4000,
            default_request_seconds=None,
            facts=[
                Fact(
                    "ALB connection idle timeout: 60 s by default, 1-4000 s.",
                    "https://docs.aws.amazon.com/elasticloadbalancing/latest/application/edit-load-balancer-attributes.html",
                ),
                Fact("Persistent data needs an EFS volume mounted at /app/data.", ""),
            ],
            extra=_aws_extra,
        ),
        Target(
            "azure_container_apps",
            "Azure Container Apps",
            "plan",
            "Compatibility checks and a deployment plan; Container Apps manifests come later.",
            persistent_disk="volume",
            max_request_seconds=240,
            default_request_seconds=None,
            facts=[
                Fact(
                    "HTTP ingress request timeout is 240 seconds.",
                    "https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview",
                ),
                Fact("Persistent data needs an Azure Files volume mounted at /app/data.", ""),
            ],
            extra=_azure_extra,
        ),
        Target(
            "vercel",
            "Vercel",
            "plan",
            "Checks only: Vercel Functions suit a frontend or short endpoints, not this long-running API.",
            persistent_disk="no",
            max_request_seconds=800,
            default_request_seconds=300,
            facts=[
                Fact(
                    "Python functions: 300 s default; max 300 s (Hobby) or 800 s (Pro/Enterprise); 500 MB bundle; "
                    "4.5 MB request body.",
                    "https://vercel.com/docs/functions/limitations",
                ),
            ],
            extra=_vercel_extra,
        ),
    ]
}

PROFILES = {
    "development": "One replica, no resource limits, verbose logs.",
    "production": "API key required, restart policy, health checks, resource limits, read-only root filesystem.",
    "enterprise": "Production plus a default-deny network policy (Kubernetes) and image digest pinning.",
}
