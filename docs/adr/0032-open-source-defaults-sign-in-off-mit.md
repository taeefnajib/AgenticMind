# ADR-0032: Open-source defaults — sign-in off by default, MIT licence

**Status:** Accepted. Amends ADR-0023.

## Context
AgenticMind will be developed as a local, open-source tool for now; a hosted multi-tenant version is not
planned yet. Requiring an admin account before first use adds friction for the common case (one engineer on
their own machine), and the project had no licence file.

## Decision
- **Sign-in is off by default** (`AGENTICMIND_AUTH` defaults to `off`). Everything from ADR-0023 still works
  with `AGENTICMIND_AUTH=required`: accounts, roles, sessions, the cross-site write guard.
- **Safe exposure defaults:**
  - the API logs a warning at start-up while sign-in is off;
  - `compose.yaml` publishes the port on `127.0.0.1` unless `AGENTICMIND_BIND` is set;
  - the dev servers already listen on localhost.
  - Exposing a server to a network is documented together with turning sign-in on.
- The cross-site write guard (Origin/Referer check on unsafe methods) stays active with sign-in off, so a web
  page in the same browser cannot drive the local API.
- **Licence:** MIT (`LICENSE` at the root, `license = "MIT"` in the package metadata).

## Consequences
- A first run opens straight into the IDE.
- Anyone who can reach an exposed server without sign-in can use its secrets and run graphs, so the
  defaults keep it on localhost and the docs say when to turn sign-in on.
- MIT allows anyone, including commercial hosts, to reuse the code; relicensing later would need
  contributors' agreement for their contributions.
