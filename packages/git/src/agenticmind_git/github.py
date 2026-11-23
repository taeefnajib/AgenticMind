"""A small GitHub REST client for atomic multi-file commits (ADR-0038).

Verified against GitHub's REST docs (Nov 2026): ``Authorization: Bearer <token>``,
``Accept: application/vnd.github+json``, ``X-GitHub-Api-Version: 2022-11-28`` (supported until 2028 and
understood by GitHub Enterprise Server), and a ``User-Agent`` on every request (GitHub returns 403 without
one). A commit is: head ref → its tree → one ``POST git/trees`` with ``base_tree`` and inline file contents
(``sha: null`` deletes) → ``POST git/commits`` → fast-forward ``PATCH git/refs`` (never forced, so history is
never rewritten). An empty repository has no branch to build on, so its first file goes through the
Contents API, which creates the branch.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import httpx

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "AgenticMind-git/0.1"


class GitError(Exception):
    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def blob_sha(content: bytes) -> str:
    """Git's blob id for content (lets us compare with remote files without downloading them)."""
    return hashlib.sha1(b"blob %d\0" % len(content) + content, usedforsecurity=False).hexdigest()


class GitHubClient:
    def __init__(self, token: str, repo: str, *, api_base: str = API_BASE, transport: Any = None) -> None:
        if "/" not in repo:
            raise GitError("GIT_REPO_INVALID", "Repository must be owner/name")
        self.owner, self.name = repo.split("/", 1)
        self.api_base = api_base.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.api_base,
            transport=transport,
            timeout=httpx.Timeout(60, connect=15),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
        )

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()

    @property
    def _repo(self) -> str:
        return f"/repos/{self.owner}/{self.name}"

    async def _call(self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201), **kw: Any) -> Any:
        try:
            resp = await self._client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise GitError("GIT_UNAVAILABLE", f"Could not reach GitHub: {type(exc).__name__}") from exc
        if resp.status_code in ok:
            return resp.json() if resp.content else None
        message = ""
        try:
            message = resp.json().get("message", "")
        except ValueError:
            message = resp.text[:200]
        status = resp.status_code
        if status == 401:
            raise GitError("GIT_AUTH_FAILED", "GitHub rejected the token", status)
        if status == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
            raise GitError("GIT_RATE_LIMITED", "GitHub rate limit reached; try again later", status)
        if status in (403, 404) and path.startswith(self._repo):
            needed = resp.headers.get("x-accepted-github-permissions", "")
            hint = f" (needs: {needed})" if needed else " (or the token cannot see it)"
            code = "GIT_FORBIDDEN" if status == 403 else "GIT_NOT_FOUND"
            raise GitError(code, f"{message or 'Not found'}{hint}", status)
        raise GitError("GIT_REQUEST_FAILED", f"GitHub returned {status}: {message}", status)

    # ---- repository -----------------------------------------------------------------------------
    async def repo(self) -> dict[str, Any]:
        return await self._call("GET", self._repo)

    async def create_repo(self, *, private: bool = True, description: str = "") -> dict[str, Any]:
        """Create ``name`` under the token's user (auto_init gives it a first commit and a branch)."""
        body = {"name": self.name, "private": private, "auto_init": True, "description": description[:350]}
        return await self._call("POST", "/user/repos", json=body)

    async def head(self, branch: str) -> str | None:
        try:
            ref = await self._call("GET", f"{self._repo}/git/ref/heads/{branch}")
        except GitError as exc:
            if exc.status in (404, 409):  # missing branch / empty repository
                return None
            raise
        return ref["object"]["sha"]

    async def create_branch(self, branch: str, from_sha: str) -> None:
        await self._call("POST", f"{self._repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": from_sha})

    # ---- files and history -----------------------------------------------------------------------
    async def get_file(self, path: str, ref: str) -> tuple[bytes, str] | None:
        """(content, blob sha), or None when the file (or the branch) does not exist."""
        try:
            data = await self._call("GET", f"{self._repo}/contents/{path}", params={"ref": ref})
        except GitError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(data, dict) or data.get("type") != "file":
            raise GitError("GIT_NOT_A_FILE", f"{path} is not a file")
        if data.get("encoding") != "base64":
            raise GitError("GIT_FILE_TOO_LARGE", f"{path} is too large to read through the API (over 1 MB)")
        return base64.b64decode(data.get("content") or ""), data["sha"]

    async def list_commits(self, path: str, branch: str, per_page: int = 30) -> list[dict[str, Any]]:
        try:
            items = await self._call(
                "GET", f"{self._repo}/commits", params={"sha": branch, "path": path, "per_page": per_page}
            )
        except GitError as exc:
            if exc.status in (404, 409):
                return []
            raise
        return [
            {
                "sha": c["sha"],
                "message": (c.get("commit") or {}).get("message", ""),
                "author": ((c.get("commit") or {}).get("author") or {}).get("name"),
                "date": ((c.get("commit") or {}).get("author") or {}).get("date"),
                "url": c.get("html_url"),
            }
            for c in items or []
        ]

    # ---- commits ---------------------------------------------------------------------------------
    async def _seed_empty(self, branch: str, path: str, content: bytes, message: str) -> str:
        body = {"message": message, "content": base64.b64encode(content).decode(), "branch": branch}
        data = await self._call("PUT", f"{self._repo}/contents/{path}", json=body)
        return data["commit"]["sha"]

    async def commit_files(
        self,
        branch: str,
        files: dict[str, bytes],
        message: str,
        *,
        delete: list[str] | None = None,
        default_branch: str | None = None,
    ) -> dict[str, Any]:
        """One commit writing ``files`` (and deleting ``delete``) on ``branch``; returns {sha, url, created}."""
        head = await self.head(branch)
        if head is None:
            base = await self.head(default_branch) if default_branch and default_branch != branch else None
            if base is not None:
                await self.create_branch(branch, base)
                head = base
            else:  # empty repository: the Contents API creates the first commit and the branch
                first = sorted(files)[0]
                head = await self._seed_empty(branch, first, files[first], message)
                files = {p: c for p, c in files.items() if p != first}
                if not files and not delete:
                    return {"sha": head, "url": self._commit_url(head), "created": True}
        commit = await self._call("GET", f"{self._repo}/git/commits/{head}")
        entries: list[dict[str, Any]] = []
        for path, content in sorted(files.items()):
            try:
                entries.append({"path": path, "mode": "100644", "type": "blob", "content": content.decode("utf-8")})
            except UnicodeDecodeError:
                blob = await self._call(
                    "POST",
                    f"{self._repo}/git/blobs",
                    json={"content": base64.b64encode(content).decode(), "encoding": "base64"},
                )
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        entries += [{"path": p, "mode": "100644", "type": "blob", "sha": None} for p in delete or []]
        tree = await self._call(
            "POST", f"{self._repo}/git/trees", json={"base_tree": commit["tree"]["sha"], "tree": entries}
        )
        if tree["sha"] == commit["tree"]["sha"]:
            return {"sha": head, "url": self._commit_url(head), "created": False}  # nothing changed
        new = await self._call(
            "POST", f"{self._repo}/git/commits", json={"message": message, "tree": tree["sha"], "parents": [head]}
        )
        try:
            await self._call("PATCH", f"{self._repo}/git/refs/heads/{branch}", json={"sha": new["sha"], "force": False})
        except GitError as exc:
            if exc.status == 422:
                raise GitError("GIT_BRANCH_MOVED", f"{branch} changed on GitHub while pushing; try again", 422) from exc
            raise
        return {"sha": new["sha"], "url": new.get("html_url") or self._commit_url(new["sha"]), "created": True}

    def _commit_url(self, sha: str) -> str:
        web = "https://github.com" if self.api_base == API_BASE else self.api_base.replace("/api/v3", "")
        return f"{web}/{self.owner}/{self.name}/commit/{sha}"
