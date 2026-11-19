# ADR-0024: Model Router node, more document formats, confirmed project deletion

**Status:** Accepted.

## Context
Three requests:
- a model router, so each task goes to a suitable model;
- uploads for more document types;
- a list of saved projects in Settings, where a project can be deleted only after typing `DELETE`.

## Decision

### Model Router (`agenticmind.models.model_router@1.0.0`)
- **One node:** a request goes in, and the chosen model's reply comes out (`text`), with the `route` and
  `model` that answered. Data edges stay simple, and a trace shows what happened: a `model_route` event
  records the route, the model, how it was chosen and why.
- **Routes:** each route has a `name`, a `description` (what kind of task it handles), `keywords`, a
  `model` and an optional `system_prompt`. There are 1–12 routes, names are unique, and a `default_route`
  is required.
- **Strategies:**
  - `keywords`: routes are checked top to bottom, and the first whose keyword appears as a whole word or
    phrase (case-insensitive) wins. Free and predictable.
  - `classifier`: a small model (`classifier_model`, default `google/gemini-3.1-flash-lite`) reads the
    route descriptions and up to 4,000 characters of the request, and returns JSON
    `{"route", "reason"}`.
  - `hybrid` (default): keywords first; the classifier only when no keyword matches.
- **Fallbacks:**
  - an unknown or invalid classifier answer, or a non-retryable classifier error, falls back to
    `default_route`;
  - retryable provider errors are raised as transient, so the engine retries the node;
  - a `route` input forces a route, and an unknown name fails with `ROUTE_UNKNOWN`.
- **Provider and usage:** one provider and key are shared by all routes, which matches OpenRouter
  (many vendors behind one key). Usage reports the classifier and answer tokens and cost added together.
- **Defaults:** the routes `general` (the default model), `quick` (Gemini Flash Lite), `reasoning`
  (Claude Sonnet 5.5) and `code` (Qwen3 Coder). Their ids were checked against OpenRouter's live model list
  in Nov 2026. The routes are a plain schema default, so the editor shows them.
- **Editor:** a route list (name, model, description, keywords, optional prompt, reorder, add/remove)
  via the `model_routes` widget, and a dropdown of route names for `default_route` (`route_ref`).

### More document formats
- **New upload types:** `.xlsx`, `.pptx`, `.epub`, `.tsv`, `.jsonl`/`.ndjson`, `.xml`, `.yaml`/`.yml`,
  `.rst`, `.log`. CSV and Markdown were already supported.
- **Parsing:** standard library only (`zipfile` + `xml.etree`); no new dependencies.
  - XLSX: one part per sheet (or per row with `csv_mode="rows"`); the first row is the header; shared,
    inline and boolean strings are handled; formulas give their cached value.
  - PPTX: one part per slide, in presentation order, including speaker notes.
  - EPUB: one part per chapter, in spine order, through the HTML extractor.
  - JSONL: per record. TSV: through the CSV parser. XML, YAML, RST and log files: as text.
- **Zip-bomb limits:** at most 5,000 entries; 64 MB per entry and 256 MB per file uncompressed, measured
  on the bytes actually read rather than the declared sizes.
- Downloads stay attachments with `nosniff` (ADR-0007), so uploaded XML or HTML never renders in the app.

### Project deletion
- **Settings → Projects** lists every project, searchable, with Open and Delete.
- **Delete** opens an inline warning naming what will be removed. The button stays disabled until
  exactly `DELETE` is typed.
- The API enforces the same rule: `DELETE /api/projects/{id}` requires `?confirm=DELETE` (otherwise
  `400 CONFIRMATION_REQUIRED`) and an admin (ADR-0023). Deleting the open project switches to another one.

### Fix found while testing: one session cookie per port
Browsers share cookies across all ports of a host. A sign-in on one local instance (`localhost:5173`)
replaced the session cookie of another (`localhost:5199`), which then showed the sign-in screen. The
cookie is now named after the browser-facing port (`am_session_<port>`; plain `am_session` without a
port), so instances on one machine keep separate sign-ins. Everyone signs in once more after upgrading.

## Consequences
- The router's routes share one provider. Per-route providers, cost or latency budgets, and fallback
  chains are not built.
- Spreadsheet dates appear as serial numbers. Legacy `.xls`, `.ppt` and `.doc` files are not supported.
- Members cannot delete projects until projects have owners.

## Revisit when
Per-route providers or fallbacks, a cost-aware strategy, legacy Office formats or OCR, or project
ownership are needed.
