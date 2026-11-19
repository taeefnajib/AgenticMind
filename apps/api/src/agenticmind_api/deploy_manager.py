"""Deployment Center service (ADR-0020): plans, confirmed deployments, local Docker runs, health, rollback.

Workflow (blueprint §13): pick revision + target → validation and compatibility checks → profile → env and
secret references → artifacts → plan with permissions → explicit confirmation (bound to the plan hash) →
deploy (local Docker) or hand over the bundle (guided) → health, endpoint and rollback in history.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from agenticmind_deployment import (
    LocalDocker,
    Plan,
    build_plan,
    compose_project,
    docker_status,
    free_port,
    health,
)
from agenticmind_ir import load_project
from agenticmind_registry import NodeRegistry
from agenticmind_runtime import SecretResolver
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from .db import DeploymentRow, utcnow
from .store import Store, iso

log = logging.getLogger("agenticmind.deploy")
ACTIVE = ("deploying", "running", "live")


class DeployError(Exception):
    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status, self.detail = status, detail


class DeploymentStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._sf = session_factory

    def add(self, row: DeploymentRow) -> DeploymentRow:
        with self._sf.begin() as s:
            s.add(row)
        return row

    def get(self, deployment_id: str) -> DeploymentRow | None:
        with self._sf() as s:
            return s.get(DeploymentRow, deployment_id)

    def list(self, project_id: str, limit: int = 50) -> list[DeploymentRow]:
        with self._sf() as s:
            stmt = (
                select(DeploymentRow)
                .where(DeploymentRow.project_id == project_id)
                .order_by(DeploymentRow.created_at.desc())
                .limit(limit)
            )
            return list(s.scalars(stmt))

    def set(self, deployment_id: str, **values: Any) -> None:
        with self._sf.begin() as s:
            s.execute(update(DeploymentRow).where(DeploymentRow.id == deployment_id).values(**values))

    def active(self, project_id: str, target: str) -> list[DeploymentRow]:
        with self._sf() as s:
            stmt = select(DeploymentRow).where(
                DeploymentRow.project_id == project_id,
                DeploymentRow.target == target,
                DeploymentRow.status.in_(("running", "live")),
            )
            return list(s.scalars(stmt))

    def fail_interrupted(self) -> int:
        with self._sf.begin() as s:
            res = s.execute(
                update(DeploymentRow)
                .where(DeploymentRow.status == "deploying")
                .values(status="failed", error="The server stopped while deploying", finished_at=utcnow())
            )
            return res.rowcount or 0


def to_json(row: DeploymentRow, *, log_tail: str | None = None) -> dict[str, Any]:
    out = {
        "id": row.id,
        "project_id": row.project_id,
        "revision": row.revision,
        "content_hash": row.content_hash,
        "target": row.target,
        "target_name": row.plan.get("target_name", row.target),
        "profile": row.profile,
        "mode": row.mode,
        "status": row.status,
        "plan_hash": row.plan_hash,
        "endpoint": row.endpoint,
        "health": row.health,
        "env": row.env,
        "image": row.image,
        "rollback_of": row.rollback_of,
        "error": row.error,
        "checks": row.plan.get("checks", []),
        "secrets": [s["name"] for s in row.plan.get("secrets", [])],
        "created_at": iso(row.created_at),
        "finished_at": iso(row.finished_at),
        "stopped_at": iso(row.stopped_at),
    }
    if log_tail is not None:
        out["log"] = log_tail
    return out


class DeploymentManager:
    def __init__(
        self,
        store: Store,
        deployments: DeploymentStore,
        registry: NodeRegistry,
        secrets: SecretResolver,
        *,
        artifacts: Any,
        data_dir: Path,
        local_enabled: bool,
    ) -> None:
        self.store, self.deployments, self.registry, self.secrets = store, deployments, registry, secrets
        self.artifacts, self.root = artifacts, data_dir / "deployments"
        self.local_enabled = local_enabled
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

    # ---- planning -------------------------------------------------------------------------------
    def local_status(self) -> tuple[bool, str]:
        if not self.local_enabled:
            return False, (
                "Local deployment is off. Set AGENTICMIND_LOCAL_DEPLOY=1 in the server's .env to let AgenticMind "
                "run Docker on this machine (the API has no authentication yet, so only do this on a trusted machine)."
            )
        return docker_status()

    def _has_secret(self, name: str) -> bool:
        try:
            return bool(self.secrets.get(name))
        except Exception:  # noqa: BLE001 - any resolver failure means "not available"
            return False

    def plan(
        self,
        project_id: str,
        *,
        revision: int | None,
        target: str,
        profile: str,
        env: dict[str, str],
        image: str = "",
    ) -> Plan:
        rev = self.store.get_revision(project_id, revision) if self.store.get_project(project_id) else None
        if rev is None:
            raise DeployError(404, "Project or revision not found")
        from agenticmind_compiler import CompileError
        from agenticmind_deployment import PlanError

        try:
            return build_plan(
                load_project(rev.document),
                self.registry,
                revision=rev.revision,
                content_hash=rev.content_hash,
                target=target,
                profile=profile,
                env=env,
                image=image,
                artifacts=self.artifacts,
                secret_available=self._has_secret,
                local_status=self.local_status() if target == "local" else None,
            )
        except PlanError as exc:
            raise DeployError(422, str(exc)) from exc
        except CompileError as exc:
            raise DeployError(422, {"message": str(exc), "diagnostics": exc.diagnostics}) from exc

    # ---- deploying ------------------------------------------------------------------------------
    async def create(
        self,
        project_id: str,
        *,
        revision: int | None,
        target: str,
        profile: str,
        env: dict[str, str],
        image: str,
        plan_hash: str,
        confirm: bool,
        rollback_of: str | None = None,
    ) -> DeploymentRow:
        if not confirm:
            raise DeployError(400, "Review the plan and confirm it (confirm: true) before deploying")
        async with self._lock:  # one deployment decision at a time (ports, superseding)
            taken = {r.host_port for r in self._all_active_local() if r.host_port}
            port = free_port(taken=taken) if target == "local" else 8080
            plan = await asyncio.to_thread(
                self.plan, project_id, revision=revision, target=target, profile=profile, env=env, image=image,
            )  # fmt: skip
            if plan.plan_hash != plan_hash:
                raise DeployError(
                    409,
                    {"message": "The plan changed since you reviewed it; review the new plan and confirm again",
                     "plan": plan.to_json()},
                )  # fmt: skip
            if not plan.deployable:
                raise DeployError(
                    422, {"message": "The plan has blockers", "blockers": [c.to_json() for c in plan.blockers]}
                )
            dep_id = f"dep_{uuid.uuid4().hex[:12]}"
            row = DeploymentRow(
                id=dep_id,
                project_id=project_id,
                revision=plan.revision,
                content_hash=plan.content_hash,
                target=target,
                profile=profile,
                mode=plan.mode,
                status="deploying" if plan.mode == "direct" else "artifacts_ready",
                plan_hash=plan.plan_hash,
                plan=plan.to_json(include_files=False),
                env=env,
                image=image,
                compose_project=compose_project(project_id, dep_id) if plan.mode == "direct" else "",
                host_port=port if plan.mode == "direct" else None,
                rollback_of=rollback_of,
            )
            self.deployments.add(row)
        workdir = self.root / dep_id
        await asyncio.to_thread(self._write_bundle, plan, workdir)
        if plan.mode == "direct":
            task = asyncio.create_task(self._deploy_local(row, plan, workdir))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            self.deployments.set(dep_id, finished_at=utcnow())
        return self.deployments.get(dep_id) or row

    def _all_active_local(self) -> list[DeploymentRow]:
        with self.deployments._sf() as s:  # noqa: SLF001
            stmt = select(DeploymentRow).where(
                DeploymentRow.target == "local", DeploymentRow.status.in_(("deploying", "running"))
            )
            return list(s.scalars(stmt))

    def _write_bundle(self, plan: Plan, workdir: Path) -> None:
        import io
        import zipfile

        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir(parents=True)
        (workdir / "bundle.zip").write_bytes(plan.bundle())
        with zipfile.ZipFile(io.BytesIO((workdir / "bundle.zip").read_bytes())) as z:
            z.extractall(workdir / "src")

    def app_dir(self, deployment_id: str) -> Path:
        src = self.root / deployment_id / "src"
        dirs = [p for p in src.iterdir() if p.is_dir()] if src.exists() else []
        if not dirs:
            raise DeployError(410, "This deployment's files are no longer on disk")
        return dirs[0]

    def log_path(self, deployment_id: str) -> Path:
        return self.root / deployment_id / "deploy.log"

    async def _deploy_local(self, row: DeploymentRow, plan: Plan, workdir: Path) -> None:
        docker = LocalDocker(self.log_path(row.id))
        names = [s["name"] for s in plan.secrets]
        try:
            # values live only in the subprocess environment; the compose file names them
            values = {n: v for n in names if (v := self.secrets.get(n))}
            app = self.app_dir(row.id)
            result = await asyncio.to_thread(
                docker.up, app, row.compose_project, secrets=values, port=row.host_port or 8080
            )
        except Exception as exc:  # noqa: BLE001 - recorded on the deployment
            log.exception("local deployment %s failed", row.id)
            result = None
            error = f"{type(exc).__name__}: {exc}"
        else:
            error = result.error
        if result is None or not result.ok:
            self.deployments.set(row.id, status="failed", error=error, finished_at=utcnow())
            try:
                await asyncio.to_thread(docker.down, self.app_dir(row.id), row.compose_project)
            except Exception:  # noqa: BLE001
                log.warning("cleanup of failed deployment %s failed", row.id)
            return
        # switch over: the new one is healthy, so stop the previous one(s) first (their files stay for
        # rollback), then mark the new one running; history never shows two running at once
        for old in self.deployments.active(row.project_id, row.target):
            if old.id == row.id:
                continue
            try:
                await asyncio.to_thread(docker.down, self.app_dir(old.id), old.compose_project)
            except DeployError:
                pass
            self.deployments.set(old.id, status="superseded", stopped_at=utcnow(), health=None)
            docker.log(f"superseded {old.id} (revision {old.revision})")
        now = utcnow()
        self.deployments.set(
            row.id, status="running", endpoint=result.endpoint, finished_at=now,
            health={"ok": True, "checked_at": now.isoformat()},
        )  # fmt: skip

    async def stop(self, deployment_id: str) -> DeploymentRow:
        row = self._get(deployment_id)
        if row.status not in ACTIVE and row.status != "artifacts_ready":
            raise DeployError(409, f"Deployment is {row.status}")
        if row.mode == "direct":
            docker = LocalDocker(self.log_path(row.id))
            ok = await asyncio.to_thread(docker.down, self.app_dir(row.id), row.compose_project)
            if not ok:
                raise DeployError(502, "docker compose down failed; see the deployment log")
        self.deployments.set(row.id, status="stopped", stopped_at=utcnow(), health=None)
        return self._get(deployment_id)

    async def check_health(self, deployment_id: str) -> DeploymentRow:
        row = self._get(deployment_id)
        if not row.endpoint:
            raise DeployError(409, "This deployment has no endpoint yet")
        ok = await asyncio.to_thread(health, row.endpoint)
        self.deployments.set(row.id, health={"ok": ok, "checked_at": utcnow().isoformat()})
        return self._get(deployment_id)

    async def register_url(self, deployment_id: str, url: str) -> DeploymentRow:
        """Guided targets: record where you deployed it, so health and history cover it too."""
        row = self._get(deployment_id)
        if row.mode == "direct":
            raise DeployError(409, "Local deployments get their endpoint automatically")
        if not url.startswith(("http://", "https://")):
            raise DeployError(422, "The URL must start with http:// or https://")
        ok = await asyncio.to_thread(health, url, attempts=2)
        for old in self.deployments.active(row.project_id, row.target):
            if old.id != row.id:
                self.deployments.set(old.id, status="superseded", stopped_at=utcnow(), health=None)
        self.deployments.set(
            row.id, status="live", endpoint=url.rstrip("/"), health={"ok": ok, "checked_at": utcnow().isoformat()}
        )
        return self._get(deployment_id)

    def bundle(self, deployment_id: str) -> tuple[DeploymentRow, bytes]:
        row = self._get(deployment_id)
        path = self.root / row.id / "bundle.zip"
        if not path.exists():
            raise DeployError(410, "This deployment's bundle is no longer on disk")
        return row, path.read_bytes()

    def log_tail(self, deployment_id: str, max_chars: int = 20000) -> str:
        path = self.log_path(deployment_id)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-max_chars:]

    def _get(self, deployment_id: str) -> DeploymentRow:
        row = self.deployments.get(deployment_id)
        if row is None:
            raise DeployError(404, "Deployment not found")
        return row

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
