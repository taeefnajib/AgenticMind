# Deployment Center

**Deploy** in the toolbar opens the Deployment Center (ADR-0020). Pick a target and profile, and it checks
what the graph needs against what the target provides *before* anything runs. Nothing is deployed until you
tick "I reviewed this plan"; that confirmation is bound to the plan's hash, so a changed plan has to be
reviewed again.

## Targets

| Target | Mode | What you get |
|---|---|---|
| Local Docker | deploys here | Builds and runs the app on this machine (127.0.0.1 only), waits for health, then stops the previous version |
| Docker server | files | Compose file (restart policy, health check, log rotation, data volume) + DEPLOY.md |
| Kubernetes | files | Kustomize: Deployment, Service, PVC, Secret references, probes, NetworkPolicy (enterprise) |
| Google Cloud Run | files | Knative service YAML with Secret Manager references + gcloud commands |
| AWS ECS on Fargate | plan | Compatibility checks and a written plan |
| Azure Container Apps | plan | Compatibility checks and a written plan |
| Vercel | plan | Checks only: suitable for a frontend, not this long-running API |

Checks are **blockers** (cannot deploy as is), **warnings** or **notes**, each with a remedy. Examples:
- an index under DATA_DIR blocks Cloud Run and Vercel, which have no persistent disk;
- memory nodes pin one replica;
- a 600 s agent exceeds Azure's 240 s ingress timeout;
- approval pauses can't wait in an exported app;
- local models need about 2 GB of memory.

Vendor limits are cited with their source and verification date in the dialog.

## Profiles

- **development**: one replica, no resource limits.
- **production**: restart policy, health checks, memory limits, read-only root filesystem.
- **enterprise**: production plus a default-deny NetworkPolicy (HTTPS egress only when the graph needs
  network).

Every profile needs `APP_API_KEYS`: without keys the deployed API answers 503 (fail closed).

## Local Docker

Off by default, because building and running containers is a privilege (any signed-in member can deploy). To enable it on a trusted machine:

```
AGENTICMIND_LOCAL_DEPLOY=1        # in .env, then restart the server
APP_API_KEYS=<a long random key>  # what clients send as X-API-Key to the deployed app
```

The graph's own secrets (e.g. `OPENROUTER_API_KEY`) must be in the server's `.env` too. They are passed to
the container by name through the `docker compose` process environment and are never written to disk.
Each project keeps one data volume (`am-<project>-data`), shared across its deployments, so a rollback
keeps the index.

**History** lists every deployment with its status, endpoint, health and log. **Roll back to this** opens
the plan for that revision. Confirm it, and the earlier version is redeployed and replaces the current
one. **Stop** removes the container and keeps the volume.

## Guided targets

**Generate & download files** produces the app plus `deploy/<target>/` artifacts and a DEPLOY.md with the
exact commands. After you deploy, paste the URL under History → **Mark live**. AgenticMind then checks
`/healthz` and keeps the deployment in history.

## API

- `GET /api/deployment/targets`
- `POST /api/projects/{id}/deployments/plan`
- `POST /api/projects/{id}/deployments` (`plan_hash`, `confirm: true`, optional `rollback_of`)
- `GET /api/projects/{id}/deployments`
- `GET /api/deployments/{id}` (with the log)
- `GET /api/deployments/{id}/bundle`
- `POST /api/deployments/{id}/stop`, `/health`, `/url`
