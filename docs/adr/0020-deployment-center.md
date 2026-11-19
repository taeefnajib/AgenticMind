# ADR-0020: Deployment Center (M8)

**Status:** Accepted

## Context
Blueprint §13 asks for a Deployment Center that "must model target capabilities and report
incompatibilities before deployment". Its nine-step workflow ends with explicit confirmation before
provisioning, then health, endpoints and rollback. The M8 exit criterion: "Compatibility checked; user
confirms; deployment/rollback visible". The open-decisions table says to start with generated artifacts
and guided deployment, add direct provisioning later, and go Docker first, with cloud adapters later.
Export app (ADR-0016) already compiles a project into a standalone FastAPI app with a Dockerfile and
declared requirements.

## Decision
- **New package `agenticmind-deployment`** (no cloud SDKs), layered on the compiler:
  - `requirements.py`: facts derived from the compiled app and graph:
    - persistent data: Index Documents, or an embedded vector store under DATA_DIR;
    - in-process state: actual memory nodes, not an Agent's declared permission;
    - the longest request, i.e. the slowest node's execution limit;
    - local models and memory; secrets; egress; human-approval pauses.
  - `targets.py`: seven targets and their checks. Every vendor limit cites its source page and was
    verified on 2026-11-19:
    - Cloud Run: 300 s default and 3600 s max request timeout; in-memory, non-persistent filesystem.
    - Azure Container Apps: 240 s ingress request timeout.
    - Vercel Functions (Python): 300 s or 800 s max duration; 500 MB bundle.
    - AWS ALB: 60 s default idle timeout, 1–4000 s.
    - Checks are blockers, warnings or notes, each with a remedy.
  - `artifacts.py`, one generator per target:
    - Docker Compose for Local and Docker server: secrets passed through by name; one data volume per
      project; production adds a read-only root, tmpfs, restart policy, log rotation and a memory
      limit.
    - Kubernetes (Kustomize): non-root, read-only root, probes, a PVC with `Recreate` when data is
      persistent, one replica when stateful, a default-deny NetworkPolicy in enterprise.
    - Cloud Run: Knative YAML with Secret Manager references, a computed timeout, `maxScale: 1` when
      stateful.
    - AWS, Azure and Vercel: a written plan only, clearly labelled.
  - `plan.py`: one pure function producing checks, requirements, secret status, artifacts,
    permissions, steps and a **plan hash** over the inputs and artifact digests.
  - `local.py`: the Local Docker deployer.
- **Modes:**
  - `direct`: only Local Docker, which AgenticMind runs itself.
  - `guided`: files and exact commands; you run them, then record the URL so health and history
    cover it.
  - `plan`: checks and plan only.
- **Confirmation is bound to what was shown.** `POST /deployments` needs `confirm: true` and the
  plan hash. The server re-plans; a different hash gives 409 with the new plan, and blockers give 422.
- **Local deploys are opt-in and local-only.** The API has no auth yet, so running Docker is off unless
  `AGENTICMIND_LOCAL_DEPLOY=1`. Containers publish only on 127.0.0.1, on a free port 8100–8199 set at
  deploy time (so the reviewed file is the deployed file). Secret values go only into the
  `docker compose` process environment, never to disk or into records. The local target also requires
  each secret, including `APP_API_KEYS`, to exist on this server.
- **Switch-over and rollback.** A new deployment must be healthy (`--wait` plus `/healthz`) before the
  previous one is stopped and marked superseded, so history never shows two running. Rollback means
  reviewing and confirming the plan of an earlier deployment's revision, recorded with `rollback_of`.
  The data volume is shared per project, so the index survives.
- **Records:** a `deployments` table holds the plan (names only), status, endpoint, health, the
  rollback link and errors. The build log is a file per deployment; the bundle ZIP is kept for
  download and audit.

## Verification
Live on this machine with Docker 29.6:
- The *Ticket triage* project deployed from the browser with the production profile (read-only root
  filesystem) and became healthy at 127.0.0.1:8100.
- The deployed API refused requests with no key or a wrong key (401) and answered with the right key.
- Neither secret value appears anywhere in the deployment files.
- Revision 2 deployed to :8101; revision 1 was stopped only afterwards.
- "Roll back to this" re-planned revision 1 with the same plan hash and replaced revision 2. Its reply
  text matched revision 1.
- Stop removed the container and kept the data volume.
- Generated Compose files pass `docker compose config`; the Kubernetes output passes
  `kubectl kustomize`. Server-side `kubectl --dry-run` needs a cluster and was not run.

Live testing found and fixed:
- the plan hash never matched for local deploys, because the port was baked into the file;
- history briefly showed two running deployments during switch-over;
- a stopped deployment kept showing a stale "healthy";
- the selected tab was unreadable in dark mode.

## Consequences
- Cloud targets are honest about what is and isn't automated: nothing is provisioned in anyone's cloud
  account yet. Image scanning is not built in. The Kubernetes and Cloud Run manifests are validated
  structurally, not against a live cluster or project.
- Each local deployment builds an image (`agenticmind/<name>:rev<N>`); old images are not pruned
  automatically.

## Revisit when
Authentication lands (then local deploy can default on for owners), direct cloud provisioning is wanted
(adapters behind the same plan and confirmation contract), or durable workers make stateful graphs
horizontally scalable.
