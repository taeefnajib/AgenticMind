# ADR-0003: Persistence

**Context.** The blueprint recommends PostgreSQL. Local development should need no services.

**Decision.** SQLAlchemy 2 models with SQLite as the zero-setup default and PostgreSQL selected via
`AGENTICMIND_DATABASE_URL` (JSON columns work on both). Projects store **append-only revisions** of
canonical IR with a content hash; saves with unchanged content are no-ops; optimistic concurrency via
`expected_revision`. Runs snapshot the executed IR and hash. Events and node outputs are first-class rows.

**Consequences.** Simple local dev; reproducible runs; cheap version history. `create_all` is used until
Alembic migrations are introduced (required before production deployments). Large artifacts will go to
S3-compatible storage, not the database.

**Revisit when.** Multi-user deployments or event volume make JSONB/indexing or partitioning necessary.
