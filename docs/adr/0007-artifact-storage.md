# ADR-0007: Project file (artifact) storage

**Context.** Ingestion needs uploaded files. The blueprint recommends S3-compatible storage; local
development and the CLI should need no services, and the IR must stay free of file contents.

**Decision.**
- Files are stored outside the IR, behind an `ArtifactStore` with a project-scoped, read-only
  `ArtifactReader` given to nodes. The IR references files only by ID (e.g. Document Loader `files`).
- First implementation: `LocalArtifactStore` (`<data_dir>/artifacts/<project>/<id>/{blob,meta.json}`),
  no database rows, shared by the API and the CLI. IDs are server-generated (`art_<16 hex>`) and
  validated on every access; client filenames are sanitised and only used for display.
- Uploads are restricted to an allowlist of extensions with a server-recorded media type and a size limit
  (`AGENTICMIND_MAX_UPLOAD_MB`, default 50). Downloads are always `attachment` + `nosniff`.
- Nodes need the new `artifacts` permission to receive the reader; the runtime scopes it to the run's project.
- Deleting a project deletes its files.

**Consequences.** Simple, portable, testable. Exported projects reference file IDs that must be
re-uploaded or bundled; the compiler (M4) will decide how to package referenced files.

**Revisit when.** Multi-instance deployments (move to S3-compatible storage behind the same interface).
