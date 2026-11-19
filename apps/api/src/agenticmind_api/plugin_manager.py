"""Node packages (ADR-0021): install a wheel, review its permissions, approve, disable, remove.

Install → the wheel goes into its own directory and its manifests are read by an isolated worker; its nodes
are registered as *isolated* and *not permitted*, so graphs that use them load but are refused by validation
(``NODE_NOT_PERMITTED``). Approve → an operator grants exactly the permissions the package requests; then
its nodes run, each invocation in a fresh worker process. A new version needs a new approval.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path
from typing import Any

from agenticmind_plugins import (
    PluginError,
    describe,
    inspect_wheel,
    install,
    make_definition,
    requested_permissions,
    unsupported_permissions,
)
from agenticmind_registry import NodeRegistry, TrustLevel
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .db import PluginRow, utcnow
from .store import iso

PLUGIN_TRUST = {"community": TrustLevel.COMMUNITY, "private": TrustLevel.PRIVATE}
MAX_WHEEL_BYTES = 50 * 1024 * 1024


class PluginManagerError(Exception):
    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status, self.detail = status, detail


def to_json(row: PluginRow) -> dict[str, Any]:
    return {
        "name": row.name,
        "version": row.version,
        "sha256": row.sha256,
        "summary": row.summary,
        "trust": row.trust,
        "status": row.status,
        "requested": row.requested,
        "granted": row.granted,
        "unsupported": row.unsupported,
        "nodes": [
            {
                "type": n["manifest"]["type"],
                "version": n["manifest"]["version"],
                "display_name": n["manifest"].get("display_name", ""),
                "description": n["manifest"].get("description", ""),
                "permissions": sorted(k for k, v in n["manifest"].get("permissions", {}).items() if v is True),
            }
            for n in row.nodes
        ],
        "installed_at": iso(row.installed_at),
        "approved_at": iso(row.approved_at),
    }


class PluginManager:
    def __init__(self, session_factory: sessionmaker, registry: NodeRegistry, *, data_dir: Path) -> None:
        self._sf = session_factory
        self.registry = registry
        self.root = (data_dir / "plugins").resolve()
        self._lock = asyncio.Lock()

    # ---- persistence ---------------------------------------------------------------------------
    def list(self) -> list[PluginRow]:
        with self._sf() as s:
            return list(s.scalars(select(PluginRow).order_by(PluginRow.name)))

    def get(self, name: str) -> PluginRow:
        with self._sf() as s:
            row = s.get(PluginRow, name)
        if row is None:
            raise PluginManagerError(404, f"No node package named {name!r}")
        return row

    def _save(self, row: PluginRow) -> None:
        with self._sf.begin() as s:
            s.merge(row)

    # ---- registry ------------------------------------------------------------------------------
    def _register(self, row: PluginRow) -> None:
        self.registry.unregister_source(row.name)
        if row.status == "disabled":
            return
        storage = self.root / "storage" / row.name
        work = self.root / "work"
        for n in row.nodes:
            definition = make_definition(n["manifest"], plugin_dir=Path(row.path), work_root=work, storage_dir=storage)
            existing = self.registry.get(*definition.key)
            if existing is not None:  # never shadow a core node or another package's node
                continue
            self.registry.register(
                definition,
                trust=PLUGIN_TRUST.get(row.trust, TrustLevel.COMMUNITY),
                source=row.name,
                isolated=True,
                permitted=row.status == "approved",
            )

    def load_all(self) -> int:
        """Register installed packages at startup from their stored manifests (no plugin code runs)."""
        count = 0
        for row in self.list():
            if Path(row.path).is_dir():
                self._register(row)
                count += 1
        return count

    # ---- actions -------------------------------------------------------------------------------
    async def install(self, filename: str, data: bytes, *, trust: str = "community") -> PluginRow:
        if trust not in PLUGIN_TRUST:
            raise PluginManagerError(422, "trust must be 'community' or 'private'")
        if len(data) > MAX_WHEEL_BYTES:
            raise PluginManagerError(413, "The wheel is larger than 50 MB")
        async with self._lock:
            return await asyncio.to_thread(self._install, Path(filename).name, data, trust)

    def _install(self, filename: str, data: bytes, trust: str) -> PluginRow:
        sha = hashlib.sha256(data).hexdigest()
        # keep the original file name (installers parse it); the folder makes it unique
        folder = self.root / "wheels" / sha[:16]
        folder.mkdir(parents=True, exist_ok=True)
        wheel = folder / (filename if filename.endswith(".whl") else f"{filename}.whl")
        wheel.write_bytes(data)
        try:
            info = inspect_wheel(wheel)
            for core in self.registry.entries():  # a package may not reuse a core distribution name
                if core.source == info.name and not core.isolated:
                    raise PluginError("PLUGIN_RESERVED_NAME", f"'{info.name}' is a built-in package")
            target = install(wheel, info, self.root / "packages")
            nodes = describe(target, workdir=self.root / "work" / f"describe-{sha[:8]}")
        except PluginError as exc:
            shutil.rmtree(folder, ignore_errors=True)
            raise PluginManagerError(422, {"code": exc.code, "message": exc.message}) from exc
        for n in nodes:
            existing = self.registry.get(n["manifest"]["type"], n["manifest"]["version"])
            if existing is not None and existing.source != info.name:
                shutil.rmtree(target, ignore_errors=True)
                raise PluginManagerError(
                    409,
                    {
                        "code": "PLUGIN_NODE_CONFLICT",
                        "message": f"{n['manifest']['type']}@{n['manifest']['version']} is already provided by "
                        f"{existing.source}",
                    },
                )
        requested = requested_permissions(nodes)
        previous = None
        try:
            previous = self.get(info.name)
        except PluginManagerError:
            pass
        row = PluginRow(
            name=info.name,
            version=info.version,
            sha256=sha,
            summary=info.summary[:500],
            trust=trust,
            status="pending",  # every install, including upgrades, needs an approval
            requested=requested,
            granted=[],
            unsupported=unsupported_permissions(requested),
            nodes=nodes,
            path=str(target),
            wheel_path=str(wheel),
            installed_at=utcnow(),
            approved_at=None,
        )
        self._save(row)
        self._register(row)
        if previous is not None and previous.path != row.path:
            shutil.rmtree(previous.path, ignore_errors=True)
            if previous.wheel_path != row.wheel_path:
                shutil.rmtree(Path(previous.wheel_path).parent, ignore_errors=True)
        return self.get(info.name)

    def approve(self, name: str, permissions: list[str], *, sha256: str, confirm: bool) -> PluginRow:
        row = self.get(name)
        if not confirm:
            raise PluginManagerError(400, "Review the permissions and confirm (confirm: true)")
        if sha256 != row.sha256:
            raise PluginManagerError(409, "The package changed since you reviewed it; review it again")
        if row.unsupported:
            raise PluginManagerError(
                422,
                f"This package needs {', '.join(row.unsupported)}, which isolated nodes cannot have yet "
                "(project files and memory are not available to plugin workers).",
            )
        if sorted(set(permissions)) != sorted(row.requested):
            raise PluginManagerError(
                422,
                "Approve exactly the permissions the package requests: " + (", ".join(row.requested) or "none"),
            )
        row.status, row.granted, row.approved_at = "approved", list(row.requested), utcnow()
        self._save(row)
        self._register(row)
        return self.get(name)

    def set_enabled(self, name: str, enabled: bool) -> PluginRow:
        row = self.get(name)
        if enabled:
            row.status = "approved" if row.approved_at and row.granted == row.requested else "pending"
        else:
            row.status = "disabled"
        self._save(row)
        self._register(row)
        return self.get(name)

    def remove(self, name: str) -> None:
        row = self.get(name)
        self.registry.unregister_source(row.name)
        shutil.rmtree(row.path, ignore_errors=True)
        shutil.rmtree(Path(row.wheel_path).parent, ignore_errors=True)
        with self._sf.begin() as s:
            obj = s.get(PluginRow, name)
            if obj is not None:
                s.delete(obj)

    def wheel(self, name: str) -> tuple[str, bytes]:
        row = self.get(name)
        path = Path(row.wheel_path)
        if not path.exists():
            raise PluginManagerError(410, "The wheel is no longer on disk")
        return path.name, path.read_bytes()
