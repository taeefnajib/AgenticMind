"""An in-memory fake of the GitHub REST endpoints the client uses, for tests (httpx ``MockTransport``).

It keeps real git semantics where they matter: commits point at full trees, ``base_tree`` merges, ``sha: null``
deletes, ``PATCH git/refs`` only fast-forwards, empty repositories have no branches (refs answer 409) until the
Contents API creates the first commit.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

import httpx

from .github import blob_sha


def _h(*parts: Any) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode(), usedforsecurity=False).hexdigest()


class FakeGitHub:
    def __init__(
        self,
        repo: str = "acme/agents",
        *,
        token: str = "fixture-gh-token",
        empty: bool = False,
        exists: bool = True,
        default_branch: str = "main",
    ) -> None:
        self.repo = repo
        self.token = token
        self.exists = exists
        self.default_branch = default_branch
        self.trees: dict[str, dict[str, bytes]] = {}
        self.commits: dict[str, dict[str, Any]] = {}
        self.refs: dict[str, str] = {}
        self.requests: list[httpx.Request] = []
        self.created_repo: dict[str, Any] | None = None
        if not empty and exists:
            self._commit(default_branch, {"README.md": b"# agents\n"}, "Initial commit")

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ---- model -----------------------------------------------------------------------------------
    def _tree(self, files: dict[str, bytes]) -> str:
        sha = _h("tree", sorted((p, blob_sha(c)) for p, c in files.items()))
        self.trees[sha] = dict(files)
        return sha

    def _commit(self, branch: str, files: dict[str, bytes], message: str) -> str:
        parent = self.refs.get(branch)
        base = dict(self.trees[self.commits[parent]["tree"]]) if parent else {}
        base.update(files)
        tree = self._tree(base)
        sha = _h("commit", tree, parent, message, len(self.commits))
        self.commits[sha] = {"tree": tree, "parents": [parent] if parent else [], "message": message}
        self.refs[branch] = sha
        return sha

    def edit_remote(self, branch: str, path: str, content: bytes, message: str = "Edited on GitHub") -> str:
        """Simulate someone else committing to the repository."""
        return self._commit(branch, {path: content}, message)

    def file(self, branch: str, path: str) -> bytes | None:
        head = self.refs.get(branch)
        return self.trees[self.commits[head]["tree"]].get(path) if head else None

    # ---- HTTP ------------------------------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        if not request.headers.get("user-agent"):
            return httpx.Response(403, json={"message": "User-Agent required"})
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        if method == "POST" and path == "/user/repos":
            self.exists = True
            self.repo = f"{self.repo.split('/')[0]}/{body['name']}"
            self.created_repo = body
            if body.get("auto_init"):
                self._commit(self.default_branch, {"README.md": b"# new\n"}, "Initial commit")
            return httpx.Response(
                201,
                json={"full_name": self.repo, "private": body.get("private"), "default_branch": self.default_branch},
            )
        prefix = f"/repos/{self.repo}"
        if not path.startswith(prefix) or not self.exists:
            return httpx.Response(404, json={"message": "Not Found"})
        rest = path[len(prefix) :]
        if rest == "" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "full_name": self.repo,
                    "private": True,
                    "default_branch": self.default_branch,
                    "html_url": f"https://github.com/{self.repo}",
                    "permissions": {"push": True},
                },
            )
        if m := re.fullmatch(r"/git/ref/heads/(.+)", rest):
            if not self.refs:
                return httpx.Response(409, json={"message": "Git Repository is empty."})
            sha = self.refs.get(m.group(1))
            return (
                httpx.Response(200, json={"object": {"sha": sha}})
                if sha
                else httpx.Response(404, json={"message": "Not Found"})
            )
        if rest == "/git/refs" and method == "POST":
            self.refs[body["ref"].removeprefix("refs/heads/")] = body["sha"]
            return httpx.Response(201, json={"ref": body["ref"], "object": {"sha": body["sha"]}})
        if m := re.fullmatch(r"/git/refs/heads/(.+)", rest):
            branch, new = m.group(1), body["sha"]
            if self.refs.get(branch) not in self.commits[new]["parents"] and not body.get("force"):
                return httpx.Response(422, json={"message": "Update is not a fast forward"})
            self.refs[branch] = new
            return httpx.Response(200, json={"object": {"sha": new}})
        if m := re.fullmatch(r"/git/commits/([0-9a-f]+)", rest):
            c = self.commits[m.group(1)]
            return httpx.Response(200, json={"sha": m.group(1), "tree": {"sha": c["tree"]}, "parents": c["parents"]})
        if rest == "/git/blobs":
            content = (
                base64.b64decode(body["content"]) if body.get("encoding") == "base64" else body["content"].encode()
            )
            self.trees.setdefault("_blobs", {})[blob_sha(content)] = content
            return httpx.Response(201, json={"sha": blob_sha(content)})
        if rest == "/git/trees":
            files = dict(self.trees[body["base_tree"]])
            for e in body["tree"]:
                if "content" in e:
                    files[e["path"]] = e["content"].encode()
                elif e.get("sha") is None:
                    files.pop(e["path"], None)
                else:
                    files[e["path"]] = self.trees["_blobs"][e["sha"]]
            return httpx.Response(201, json={"sha": self._tree(files)})
        if rest == "/git/commits" and method == "POST":
            sha = _h("commit", body["tree"], body["parents"], body["message"], len(self.commits))
            self.commits[sha] = {"tree": body["tree"], "parents": body["parents"], "message": body["message"]}
            return httpx.Response(201, json={"sha": sha, "html_url": f"https://github.com/{self.repo}/commit/{sha}"})
        if m := re.fullmatch(r"/contents/(.+)", rest):
            file_path = m.group(1)
            if method == "PUT":
                branch = body.get("branch") or self.default_branch
                sha = self._commit(branch, {file_path: base64.b64decode(body["content"])}, body["message"])
                return httpx.Response(201, json={"commit": {"sha": sha}})
            ref = request.url.params.get("ref") or self.default_branch
            head = self.refs.get(ref) or (ref if ref in self.commits else None)
            content = self.trees[self.commits[head]["tree"]].get(file_path) if head else None
            if content is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "sha": blob_sha(content),
                    "content": base64.b64encode(content).decode(),
                },
            )
        if rest == "/commits":
            head = self.refs.get(request.url.params.get("sha") or self.default_branch)
            target = request.url.params.get("path")
            out, sha = [], head
            while sha:
                c = self.commits[sha]
                parent = c["parents"][0] if c["parents"] else None
                mine = self.trees[c["tree"]].get(target)
                before = self.trees[self.commits[parent]["tree"]].get(target) if parent else None
                if mine != before:
                    out.append(
                        {
                            "sha": sha,
                            "html_url": f"https://github.com/{self.repo}/commit/{sha}",
                            "commit": {
                                "message": c["message"],
                                "author": {"name": "Ada", "date": "2026-11-24T10:00:00Z"},
                            },
                        }
                    )
                sha = parent
            return httpx.Response(200, json=out)
        return httpx.Response(404, json={"message": f"fake: unhandled {method} {rest}"})
