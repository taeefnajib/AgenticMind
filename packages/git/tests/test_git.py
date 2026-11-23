"""GitHub client and sync rules against the in-memory fake GitHub."""

from __future__ import annotations

import json

import pytest
from agenticmind_git import GitError, GitHubClient, GitLink, blob_sha, check_push, project_files
from agenticmind_git.testing import FakeGitHub
from agenticmind_ir import load_project

TOKEN = "fixture-gh-token"
DOC = load_project(
    {
        "ir_version": "1.0",
        "project": {"id": "p1", "name": "Support bot", "description": "Answers tickets."},
        "nodes": [
            {
                "id": "q",
                "type": "agenticmind.core.manual_input",
                "version": "1.0.0",
                "config": {"key": "q"},
                "ui": {"label": "Question"},
            }
        ],
        "edges": [],
        "metadata": {},
    }
)


def client(fake: FakeGitHub) -> GitHubClient:
    return GitHubClient(TOKEN, fake.repo, transport=fake.transport)


def test_blob_sha_matches_git():
    assert blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"  # `git hash-object` of "hello\n"


def test_link_validation_and_layout():
    link = GitLink.validate({"repo": "https://github.com/acme/agents.git", "folder": "/bots/support/"})
    assert link.repo == "acme/agents" and link.folder == "bots/support" and link.branch == "main"
    for bad in (
        {"repo": "nope"},
        {"repo": "a/b", "branch": "../x"},
        {"repo": "a/b", "folder": "../up"},
        {"repo": "a/b", "token_secret": "lower"},
        {"repo": "a/b", "api_base": "http://x"},
    ):
        with pytest.raises(ValueError):
            GitLink.validate(bad)
    files = project_files(DOC, link, {"main.py": b"print()"})
    assert set(files) == {"bots/support/project.agenticmind.json", "bots/support/README.md", "bots/support/app/main.py"}
    ir = json.loads(files["bots/support/project.agenticmind.json"])
    assert ir["project"]["name"] == "Support bot" and list(ir) == sorted(ir)  # canonical: sorted keys
    readme = files["bots/support/README.md"].decode()
    assert "# Support bot" in readme and "`q` (Question)" in readme and "agenticmind run bots/support/" in readme


def test_check_push_rules():
    assert check_push("a", "a", None, False) == "up_to_date"
    assert check_push(None, "a", None, False) == "push"  # new file
    assert check_push("old", "new", "old", False) == "push"  # fast-forward from our last sync
    with pytest.raises(GitError) as exc:
        check_push("theirs", "mine", "old", False)
    assert exc.value.code == "GIT_REMOTE_CHANGED"
    assert check_push("theirs", "mine", "old", True) == "push"


async def test_atomic_commit_delete_and_noop():
    fake = FakeGitHub()
    async with client(fake) as gh:
        r = await gh.commit_files("main", {"a/x.json": b"{}", "a/y.md": b"hi"}, "two files")
        assert r["created"] and fake.file("main", "a/x.json") == b"{}" and fake.file("main", "README.md")  # base kept
        head_before = fake.refs["main"]
        same = await gh.commit_files("main", {"a/x.json": b"{}"}, "no change")
        assert not same["created"] and fake.refs["main"] == head_before
        await gh.commit_files("main", {}, "remove y", delete=["a/y.md"])
        assert fake.file("main", "a/y.md") is None
        commits = await gh.list_commits("a/y.md", "main")
        assert [c["message"] for c in commits] == ["remove y", "two files"]
        assert await gh.get_file("a/x.json", "main") == (b"{}", blob_sha(b"{}"))
        assert await gh.get_file("missing.txt", "main") is None
    patches = [r for r in fake.requests if r.method == "PATCH"]
    assert all(json.loads(r.content)["force"] is False for r in patches)  # history is never rewritten
    assert all(r.headers["x-github-api-version"] == "2022-11-28" for r in fake.requests)


async def test_new_branch_empty_repo_and_errors():
    fake = FakeGitHub()
    async with client(fake) as gh:
        await gh.commit_files("agents", {"f.txt": b"1"}, "on a branch", default_branch="main")
        assert fake.file("agents", "f.txt") == b"1" and fake.file("main", "f.txt") is None
    empty = FakeGitHub(empty=True)
    async with client(empty) as gh:
        r = await gh.commit_files("main", {"a.json": b"{}", "b.md": b"x"}, "first push")
        assert empty.file("main", "a.json") == b"{}" and empty.file("main", "b.md") == b"x" and r["created"]
    async with GitHubClient("wrong", fake.repo, transport=fake.transport) as gh:
        with pytest.raises(GitError) as exc:
            await gh.repo()
        assert exc.value.code == "GIT_AUTH_FAILED"
    async with GitHubClient(TOKEN, "acme/other", transport=fake.transport) as gh:
        with pytest.raises(GitError) as exc:
            await gh.repo()
        assert exc.value.code == "GIT_NOT_FOUND"


async def test_branch_moved_during_push(monkeypatch):
    fake = FakeGitHub()
    async with client(fake) as gh:
        real = gh._call

        async def racing(method, path, **kw):
            if method == "POST" and path.endswith("/git/commits"):
                fake.edit_remote("main", "other.txt", b"someone else")  # lands between our read and our ref update
            return await real(method, path, **kw)

        monkeypatch.setattr(gh, "_call", racing)
        with pytest.raises(GitError) as exc:
            await gh.commit_files("main", {"x.txt": b"mine"}, "racing")
    assert exc.value.code == "GIT_BRANCH_MOVED" and fake.file("main", "other.txt") == b"someone else"
