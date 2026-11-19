# ADR-0021: Node packages in isolated workers (M9)

**Status:** Accepted. Supersedes ADR-0004 in part.

## Context
M9 exit criterion: "Third-party node can be developed, tested, installed and permissioned." ADR-0004 refused
non-core nodes at validation until isolated workers existed. Inspecting the code for M9 showed two gaps:
- `NodeRegistry.load_entry_points` imported **every** distribution's `agenticmind.nodes` entry point, so a
  package pip-installed into the platform's environment ran its import-time code inside the API process,
  even though its nodes were later refused.
- Agent tool discovery (`agenticmind.tools`, ADR-0014) did the same.

## Decision
- **Never import third-party code into the platform process.**
  - Only core distributions (`agenticmind_node_sdk.trust.CORE_DISTRIBUTIONS`) are loaded in-process; other
    entry points found in the host environment are skipped and reported as `skipped_in_environment`.
  - `AGENTICMIND_IN_PROCESS_TRUST` with anything beyond `core` remains the explicit operator opt-in.
- **Node packages are wheels installed into their own folder** (`<data>/plugins/packages/<name>-<version>`):
  - `--no-deps` for the wheel; its non-`agenticmind-*` dependencies go next to it;
  - the host supplies the SDK.
  - Wheels are inspected without importing them: name, version, dependencies, entry points, unsafe paths.
    Reserved names (`agenticmind*`) and node types in the `agenticmind.` namespace are refused, and so are
    nodes that clash with existing ones.
- **The isolated worker** (`python -I -m agenticmind_plugins.worker`) is the only code that imports a
  plugin:
  - `describe` reads its manifests once at install; they are stored, so startup needs no plugin code;
  - `run` executes one invocation.
- **Per invocation, the worker gets:**
  - a fresh process with a scrubbed environment (no host variables, so no host secrets);
  - its own temporary working directory;
  - the plugin folder appended (never prepended) to `sys.path`;
  - inputs, config and run input as JSON on stdin;
  - only the secrets referenced in that node's own config, and only with `credentials`;
  - a package storage folder only with `filesystem`;
  - a network guard without `network` (labelled a guard, not a sandbox);
  - the node's timeout (plus CPU and memory rlimits on POSIX).
- **Results:** outputs come back as JSON, events are replayed into the trace, and output types are checked
  by the host engine as for any node. Plugin prints cannot corrupt the protocol. Crashes, `os._exit`,
  non-JSON outputs and timeouts become node errors (`PLUGIN_*`).
- **The host side** is a `RemoteNodeDefinition`: an ordinary registry entry marked `isolated`, with config
  validated against the manifest's JSON Schema (errors surface as normal `CONFIG_INVALID` diagnostics).
- **Permissioned:**
  - An installed package is `pending`, and its nodes are registered with `permitted=False`. They appear
    in the library (marked "not approved") and graphs load, but validation reports `NODE_NOT_PERMITTED`
    (a new public diagnostic code).
  - Approval must grant **exactly** the requested permissions, include `confirm: true` and the reviewed
    wheel hash, and is refused for permissions isolated workers cannot honour yet (`artifacts`, `memory`).
  - Every install, including an upgrade, needs a new approval. Disable, enable, remove and wheel
    download (import/export) are supported.
- **Trust levels:** `core` (built in); installed packages are `community` (default) or `private`, chosen
  by the operator at install. `verified` stays reserved until signing exists.
- **SDK tooling:** the `agenticmind-node` CLI:
  - `new` scaffolds a package with an example node, tests and README; the generated code passes ruff;
  - `validate` checks namespace, semver, descriptions, permissions and tests;
  - `test` runs pytest;
  - `pack` validates, then builds the wheel.
  - `agenticmind_plugins.testing` builds, installs and runs a package exactly as the platform does.
- **Export:** a graph using package nodes cannot be compiled into a standalone app yet (a clear
  `CompileError`, which the Deployment Center reports too).

## Verification
- **16+ isolation tests** with a probe package that tries to misbehave:
  - it sees no host secret and runs in another PID, and the host never imports it;
  - opening the network without permission fails, and only its own secret reaches it;
  - storage is scoped; crashes, exits, non-JSON outputs and timeouts are contained;
  - an unapproved package is refused, and its config is schema-checked.
- **SDK end to end:** new → validate → test → pack → install → run, plus lint-clean scaffolds.
- **API tests:** install → pending → refused → approve (confirm, hash, exact permissions) → run → disable
  → enable → download → remove; new versions need re-approval; state survives a restart.
- **Live in the browser:** the example wheel was installed, the library showed "not approved", the
  Problems panel showed NODE_NOT_PERMITTED with its hint, and after approval the graph ran with the
  correct output.
- Live testing caught relative plugin paths failing (the worker runs in its own working directory),
  invalid stored wheel filenames, and stale validation after approval. All are fixed and covered.

## Consequences
- A process per invocation costs about 0.3–0.6 s, fine for graph steps but not for tight loops. A pooled
  worker per package is a later optimisation.
- On Windows only the timeout bounds a worker (no rlimits), and network/filesystem are not OS-enforced:
  the boundary is the separate process, scrubbed environment, working directory and secret scoping. A
  container worker (`--network none` without the permission, read-only filesystem) is the next step for
  hostile code.
- Plugin-provided agent tools and plugin access to project files and memory are not supported yet.

## Revisit when
Package signing (then `verified`), container workers, a package index/registry service, or plugin tools
and artifacts access are needed.
