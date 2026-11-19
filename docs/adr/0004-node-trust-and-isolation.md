# ADR-0004: Node trust levels and isolation

**Context.** Third-party node code must run with declared permissions and isolation. Process/container
sandboxing is not built yet.

**Decision.** Trust (`core`, `verified`, `community`, `private`) is assigned by the registry from the
providing distribution, never self-declared. The validator rejects nodes whose trust level is not in the
in-process allowlist (default: `core` only) with `NODE_ISOLATION_UNAVAILABLE`. Operators may extend the
allowlist (`AGENTICMIND_IN_PROCESS_TRUST`) as an explicit, documented opt-in. Secret access is enforced
in-process: nodes may resolve only secrets referenced in their own config, and only with the
`credentials` permission.

**Consequences.** Safe by default; community nodes cannot run until isolated workers exist. Network/FS
permissions are declared but not OS-enforced in-process.

**Revisit when.** Docker-based node workers land (planned before M9).

**Update (M9).** Superseded in part by ADR-0021: third-party nodes now run as installed packages in
isolated worker processes with approved permissions, and non-core packages in the host environment are no
longer imported by default.
