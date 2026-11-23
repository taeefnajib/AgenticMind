# ADR-0038: GitHub version control for projects

**Status:** Accepted. Feature 6 of the open-source production plan (ADR-0032).

## Context
Projects are versioned inside AgenticMind as revisions. Teams also want their agent workflows in Git: code
review, history and blame, CI, sharing, and backups outside the AgenticMind database.

## Decision

### What goes into the repository (under an optional folder)
- `project.agenticmind.json`: the **canonical IR**. Keys are sorted and formatting is stable, so diffs and
  reviews show real changes.
- `README.md`: generated from the project's name, description and node list, plus how to run it locally
  (`agenticmind run`).
- `app/`: optionally (`include_app`), the exported standalone FastAPI app (ADR-0019).
- Secrets stay references (`{"$secret": ...}`); no secret value is ever written.

### `agenticmind-git` package
- **`GitHubClient`** (httpx, raw REST):
  - headers `Authorization: Bearer`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version:
    2022-11-28` (supported until 2028 and understood by GitHub Enterprise Server), and a `User-Agent`
    (GitHub returns 403 without one);
  - an optional `api_base` for GitHub Enterprise (`https://host/api/v3`).
- **A push is one commit** through the Git Data API:
  - head ref, then its tree;
  - **one** `POST git/trees` with `base_tree` and inline text contents: unrelated files are kept, `sha: null`
    deletes, and binary files go through blobs. This stays well inside GitHub's content-creation limits;
  - `POST git/commits`;
  - a **fast-forward-only** `PATCH git/refs` (`force: false`), so history is never rewritten. If the branch
    moved meanwhile, the push stops with `GIT_BRANCH_MOVED`.
- **Branches and repositories:**
  - **New branch:** created from the default branch.
  - **Empty repository:** it has no branch for refs, so its first file is written through the Contents API,
    which creates the branch; the remaining files then follow in an atomic commit.
  - **Missing repository** (with `create_repo`): created under the token's user as a private repo with
    `auto_init`.
- **Local blob ids:** `blob_sha` computes git's blob id locally, so AgenticMind can compare with GitHub
  without downloading files.
- **`agenticmind_git.testing.FakeGitHub`:** an in-memory GitHub with real git semantics (full trees,
  base-tree merges, deletes, fast-forward-only refs, empty repositories answering 409).

### Sync rules
- **The link** (repo, branch, folder, token secret name and options) and **sync state** (last commit, URL,
  synced revision, and the IR blob id last pushed or pulled) are stored per project. The token stays in the
  secret store.
- **Push sends the latest saved revision.**
  - The project file is unchanged on GitHub (byte-identical, or the same project in other formatting):
    nothing to push.
  - It is unchanged since our last sync: a commit.
  - **Someone else changed it:** `GIT_REMOTE_CHANGED` (409). Pull first, or push with `force` to add your
    version as a new commit on top; history is still kept.
- **Pull** saves the branch's file as a new revision, so earlier revisions remain. **History** lists the
  commits that touched the file. **Restore** loads any commit as a new revision. Restore does not move the
  sync point, so a later push still detects remote changes.
- **Auto-push:** after each save the push runs in the background. Saves run in the threadpool, so the push
  is handed to the event loop. A refused or failed auto-push is recorded in the state, never a failed save.

### API and IDE
- `GET/PUT/DELETE /api/projects/{id}/git` (link), `POST …/git/check` (reachability and push permission),
  `…/push` (`message`, `force`), `…/pull`, `GET …/history`, `POST …/restore` (`sha`).
- **The project panel's GitHub section has:**
  - the link form: repo, branch, folder, token secret, auto-push, include app, create repo;
  - "Check access";
  - Push with a message, and "Push anyway" offered on a remote change;
  - Pull, history with links and Restore;
  - last sync and error, and a reminder that pushes send the saved revision.

### Upstream docs
- **Checked against GitHub's REST docs:**
  - API versions, the Git refs / trees / commits / blobs pages, contents and commits;
  - repository creation;
  - the fine-grained token permissions table (Contents: write for commits; Administration: write to create
    repositories);
  - authentication (Bearer and `User-Agent`), rate limits and status codes.
- **Not confirmed:** creating the first commit on an empty repository through the Contents API (implemented
  and tested against the fake).

## Verification
- **Package:**
  - blob ids equal `git hash-object`;
  - link validation and layout (canonical sorted IR, README);
  - push rules;
  - the atomic multi-file commit keeps the base tree; a no-change commit is skipped; deletes; history per
    path; reading files;
  - branch created from the default branch; the empty repository seeded and then committed;
  - auth and not-found errors;
  - a branch moved mid-push refused, with every ref update fast-forward-only.
- **API:**
  - link validation, check, first push and up-to-date;
  - edit and push, then history;
  - a remote edit blocks the push (409), then pull, then up to date;
  - restore of the first commit; an invalid sha;
  - auto-push on save to a new branch, then a refused auto-push recorded, then a force push;
  - a missing token; unlink; repository creation (private, `auto_init`).
- **Found by the tests:** after pulling a hand-edited remote file, pushing made a reformat-only commit.
  Content is now compared canonically, so formatting alone is not a change.
- **Not live:** pushing to GitHub needs your token and a repository you choose, and was not run from this
  machine.

## Consequences
- **One provider:** only GitHub (and GitHub Enterprise); GitLab and Bitbucket would plug in behind the same
  link and state.
- **No merging:** pulls replace the project with the remote version as a new revision; there is no
  three-way merge of graph changes.
- **Pushes are manual or on save;** there is no webhook-driven pull from GitHub yet.

## Revisit when
GitLab or Bitbucket, pull requests from the IDE, merge tooling for diverged graphs, or GitHub-to-AgenticMind
webhooks are needed.
