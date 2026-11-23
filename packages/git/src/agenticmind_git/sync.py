"""Project ↔ repository sync (ADR-0038): what is written, and when a push or pull is safe.

Repository layout under ``folder`` (``""`` = repository root):

* ``project.agenticmind.json``: the canonical IR (sorted keys, stable formatting: diffs are meaningful);
* ``README.md``: generated (name, description, nodes, how to run it locally);
* ``app/``: optionally the exported standalone FastAPI app (ADR-0019).

Safety: a push compares the remote IR's blob id with the one we last pushed or pulled. If someone else changed
it since, the push stops with ``GIT_REMOTE_CHANGED`` (pull first, or push with ``force`` to write a new commit
on top; history is never rewritten).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agenticmind_ir import ProjectDocument, to_canonical_json

from .github import GitError, blob_sha

IR_FILE = "project.agenticmind.json"


@dataclass
class GitLink:
    repo: str
    branch: str = "main"
    folder: str = ""
    token_secret: str = "GITHUB_TOKEN"
    auto_push: bool = False
    include_app: bool = False
    create_repo: bool = False
    private: bool = True
    api_base: str = "https://api.github.com"

    @classmethod
    def validate(cls, raw: dict[str, Any]) -> GitLink:
        repo = str(raw.get("repo") or "").strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}", repo):
            raise ValueError("repo: enter owner/name (e.g. acme/support-bot)")
        branch = str(raw.get("branch") or "main").strip()
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch) or ".." in branch or branch.startswith("/"):
            raise ValueError("branch: invalid branch name")
        folder = str(raw.get("folder") or "").strip().strip("/")
        if folder and (not re.fullmatch(r"[A-Za-z0-9._/ -]{1,200}", folder) or ".." in folder.split("/")):
            raise ValueError("folder: use a relative path such as agents/support-bot")
        secret = str(raw.get("token_secret") or "GITHUB_TOKEN").strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", secret):
            raise ValueError("token_secret: enter a secret name such as GITHUB_TOKEN")
        api_base = str(raw.get("api_base") or "https://api.github.com").strip().rstrip("/")
        if not api_base.startswith("https://"):
            raise ValueError("api_base: must be an https URL (GitHub Enterprise: https://host/api/v3)")
        return cls(
            repo=repo,
            branch=branch,
            folder=folder,
            token_secret=secret,
            auto_push=bool(raw.get("auto_push", False)),
            include_app=bool(raw.get("include_app", False)),
            create_repo=bool(raw.get("create_repo", False)),
            private=bool(raw.get("private", True)),
            api_base=api_base,
        )

    def path(self, name: str) -> str:
        return f"{self.folder}/{name}" if self.folder else name


def readme(doc: ProjectDocument, folder: str) -> str:
    p = doc.project
    lines = [f"# {p.name}", ""]
    if p.description:
        lines += [p.description, ""]
    lines += [
        "An AgenticMind workflow, kept under version control.",
        "",
        "## Nodes",
        "",
    ]
    for n in doc.nodes:
        label = f" ({n.ui.label})" if n.ui and n.ui.label else ""
        lines.append(f"- `{n.id}`{label}: `{n.type}@{n.version}`")
    where = f"{folder}/" if folder else ""
    lines += [
        "",
        "## Run it",
        "",
        "Open it in the AgenticMind IDE (New → Import IR), or run it from the command line:",
        "",
        "```bash",
        f"agenticmind run {where}{IR_FILE} -i key=value",
        "```",
        "",
        'Secrets are referenced by name (`{"$secret": "NAME"}`) and never stored in this repository.',
        "",
    ]
    return "\n".join(lines)


def project_files(doc: ProjectDocument, link: GitLink, app_files: dict[str, bytes] | None = None) -> dict[str, bytes]:
    files = {
        link.path(IR_FILE): to_canonical_json(doc).encode("utf-8"),
        link.path("README.md"): readme(doc, link.folder).encode("utf-8"),
    }
    for path, content in (app_files or {}).items():
        files[link.path(f"app/{path}")] = content
    return files


def check_push(remote_sha: str | None, local_sha: str, synced_sha: str | None, force: bool) -> str:
    """'up_to_date' | 'push'; raises GIT_REMOTE_CHANGED when the remote changed since the last sync."""
    if remote_sha == local_sha:
        return "up_to_date"
    if remote_sha is None or remote_sha == synced_sha or force:
        return "push"
    raise GitError(
        "GIT_REMOTE_CHANGED",
        "The project file changed on GitHub since your last push or pull. Pull it first, or push with "
        "'force' to put your version on top (history is kept).",
        409,
    )


__all__ = ["IR_FILE", "GitLink", "blob_sha", "check_push", "project_files", "readme"]
