"""Project version control on GitHub (ADR-0038): link, push, pull, history, restore and auto-push.

The link (repository, branch, folder, token secret name, options) and the sync state (last commit and the IR
blob id we last pushed or pulled) are kept per project in the settings table (``git:<project id>``). The
GitHub token itself stays in the secret store and is resolved per request.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agenticmind_git import IR_FILE, GitError, GitHubClient, GitLink, blob_sha, check_push, project_files
from agenticmind_ir import load_project, to_canonical_json

if TYPE_CHECKING:
    from .deps import Services

log = logging.getLogger("agenticmind.api.git")


def _key(project_id: str) -> str:
    return f"git:{project_id}"


class GitSyncService:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.transport: Any = None  # replaceable in tests
        self._tasks: set[asyncio.Task[Any]] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    # ---- stored link and state ------------------------------------------------------------------
    def load(self, project_id: str) -> tuple[GitLink | None, dict[str, Any]]:
        row = self.svc.auth.get_setting(_key(project_id))
        if row is None:
            return None, {}
        value = dict(row.value)
        try:
            return GitLink.validate(value.get("link") or {}), dict(value.get("state") or {})
        except ValueError:
            return None, dict(value.get("state") or {})

    def save(self, project_id: str, link: GitLink | None, state: dict[str, Any]) -> None:
        value = None if link is None else {"link": link.__dict__, "state": state}
        self.svc.auth.set_setting(_key(project_id), value, user_id=None)

    def _client(self, link: GitLink) -> GitHubClient:
        token = self.svc.secrets.get(link.token_secret)
        if not token:
            raise GitError(
                "GIT_TOKEN_MISSING",
                f"Add the secret {link.token_secret} (a GitHub token with Contents: "
                "read and write on the repository) in Settings → Secrets or .env",
            )
        return GitHubClient(token, link.repo, api_base=link.api_base, transport=self.transport)

    def _lock(self, project_id: str) -> asyncio.Lock:
        return self._locks.setdefault(project_id, asyncio.Lock())

    # ---- operations -------------------------------------------------------------------------------
    async def check(self, link: GitLink) -> dict[str, Any]:
        async with self._client(link) as gh:
            try:
                repo = await gh.repo()
            except GitError as exc:
                if exc.status == 404 and link.create_repo:
                    return {"ok": True, "exists": False, "will_create": True}
                raise
        perms = repo.get("permissions") or {}
        return {
            "ok": bool(perms.get("push", True)),
            "exists": True,
            "private": repo.get("private"),
            "default_branch": repo.get("default_branch"),
            "url": repo.get("html_url"),
            "can_push": perms.get("push"),
        }

    async def push(self, project_id: str, *, message: str | None = None, force: bool = False) -> dict[str, Any]:
        link, state = self.load(project_id)
        if link is None:
            raise GitError("GIT_NOT_LINKED", "Link this project to a GitHub repository first")
        rev = self.svc.store.get_revision(project_id)
        if rev is None:
            raise GitError("GIT_NOTHING_TO_PUSH", "Save the project first")
        doc = load_project(rev.document)
        app_files = await asyncio.to_thread(self._app_files, doc) if link.include_app else None
        files = project_files(doc, link, app_files)
        ir_path = link.path(IR_FILE)
        local = blob_sha(files[ir_path])
        async with self._lock(project_id), self._client(link) as gh:
            try:
                repo = await gh.repo()
            except GitError as exc:
                if exc.status == 404 and link.create_repo:
                    repo = await gh.create_repo(private=link.private, description=doc.project.description or "")
                else:
                    raise
            remote = await gh.get_file(ir_path, link.branch)
            remote_sha = remote[1] if remote else None
            if remote is not None and remote_sha != local and self._same_project(remote[0], doc):
                remote_sha = local  # same content, other formatting (e.g. edited by hand): nothing to push
            decision = check_push(remote_sha, local, state.get("ir_sha"), force)
            if decision == "up_to_date":
                state.update(ir_sha=local, revision=rev.revision, synced_at=datetime.now(UTC).isoformat(), error=None)
                self.save(project_id, link, state)
                return {"status": "up_to_date", "commit": state.get("commit"), "url": state.get("url")}
            msg = (message or "").strip() or f"AgenticMind: {doc.project.name} revision {rev.revision}"
            result = await gh.commit_files(link.branch, files, msg[:5000], default_branch=repo.get("default_branch"))
        state.update(
            commit=result["sha"],
            url=result["url"],
            ir_sha=local,
            revision=rev.revision,
            synced_at=datetime.now(UTC).isoformat(),
            error=None,
        )
        self.save(project_id, link, state)
        return {"status": "pushed", "commit": result["sha"], "url": result["url"], "files": len(files)}

    @staticmethod
    def _same_project(content: bytes, doc: Any) -> bool:
        try:
            remote = load_project(content.decode("utf-8"))
        except Exception:  # noqa: BLE001 - an invalid remote file is simply "different"
            return False
        remote.project.id = doc.project.id
        return to_canonical_json(remote) == to_canonical_json(doc)

    def _app_files(self, doc: Any) -> dict[str, bytes]:
        from agenticmind_compiler import compile_project

        result = compile_project(doc, self.svc.registry, artifacts=self.svc.artifacts)
        root = result.root.rstrip("/") + "/"
        return {p.removeprefix(root): c for p, c in result.files.items()}

    async def history(self, project_id: str, limit: int = 30) -> list[dict[str, Any]]:
        link, _state = self.load(project_id)
        if link is None:
            return []
        async with self._client(link) as gh:
            return await gh.list_commits(link.path(IR_FILE), link.branch, per_page=min(limit, 100))

    async def pull(self, project_id: str, ref: str | None = None) -> dict[str, Any]:
        """Save the project file from GitHub (branch head, or a commit for restore) as a new revision."""
        link, state = self.load(project_id)
        if link is None:
            raise GitError("GIT_NOT_LINKED", "Link this project to a GitHub repository first")
        async with self._lock(project_id), self._client(link) as gh:
            remote = await gh.get_file(link.path(IR_FILE), ref or link.branch)
        if remote is None:
            raise GitError("GIT_NOT_FOUND", f"{link.path(IR_FILE)} does not exist on {ref or link.branch}", 404)
        content, sha = remote
        try:
            doc = load_project(content.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - shown to the user
            raise GitError(
                "GIT_INVALID_PROJECT", f"The file on GitHub is not a valid AgenticMind project: {exc}"
            ) from exc
        doc.project.id = project_id
        current = self.svc.store.get_revision(project_id)
        if current is not None and to_canonical_json(load_project(current.document)) == to_canonical_json(doc):
            return {"status": "up_to_date", "revision": current.revision}
        _row, rev, _created = self.svc.store.save_revision(project_id, doc, None)
        if ref is None:  # a pull of the branch head is a sync point; a restore of an old commit is not
            state.update(ir_sha=sha, revision=rev.revision, synced_at=datetime.now(UTC).isoformat(), error=None)
            self.save(project_id, link, state)
        if self.svc.triggers is not None:
            self.svc.triggers.request_refresh()
        return {"status": "pulled", "revision": rev.revision, "ref": ref or link.branch}

    def schedule_auto_push(self, project_id: str) -> None:
        """After a save: push in the background when the link has auto-push on (failures land in the state)."""
        link, _state = self.load(project_id)
        if link is None or not link.auto_push:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # called from a sync route (threadpool): hand over to the event loop thread
            import anyio.from_thread

            try:
                anyio.from_thread.run_sync(self._spawn_auto_push, project_id)
            except RuntimeError:
                log.warning("auto-push for %s skipped: no event loop", project_id)
            return
        self._spawn_auto_push(project_id)

    def _spawn_auto_push(self, project_id: str) -> None:
        task = asyncio.get_running_loop().create_task(self._auto_push(project_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _auto_push(self, project_id: str) -> None:
        try:
            await self.push(project_id)
        except GitError as exc:
            link, state = self.load(project_id)
            if link is not None:
                state["error"] = f"auto-push: {exc.message}"
                self.save(project_id, link, state)
            log.warning("auto-push for %s failed: %s", project_id, exc.message)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
