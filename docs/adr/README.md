# Architecture decision records

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-own-ir-and-runtime.md) | Canonical IR and first-party runtime; frameworks as adapters | Accepted |
| [0002](0002-monorepo-and-stack.md) | Monorepo layout and starting stack | Accepted |
| [0003](0003-persistence.md) | SQLite default, PostgreSQL via URL, append-only revisions | Accepted |
| [0004](0004-node-trust-and-isolation.md) | Only core nodes execute in-process until isolation exists | Accepted |
| [0005](0005-in-process-run-execution.md) | Runs execute as asyncio tasks in the API process (for now) | Accepted |
| [0006](0006-edge-semantics.md) | Distinct data/control edges, OR-join, skip propagation, fail-fast | Accepted |
| [0007](0007-artifact-storage.md) | Project files in a scoped artifact store, referenced by ID | Accepted |
| [0008](0008-embeddings-vector-stores-run-targets.md) | Embedding providers, vector store adapters, handles, run targets | Accepted |
| [0009](0009-llm-provider-and-grounded-answers.md) | OpenRouter LLM provider, Context/Citation Builder, chat panel | Accepted |
| [0010](0010-tools-and-agent-node.md) | Tool handles, bounded Agent loop, starter tools | Accepted |
| [0011](0011-loops-router-join.md) | Paired loop regions, Router with config-defined routes, AND Join | Accepted |
| [0012](0012-memory.md) | Conversation state vs consented long-term memory, retention, deletion | Accepted |
| [0013](0013-checkpoints-approval-resume.md) | Durable checkpoints, Human Approval suspension, resume | Accepted |
| [0014](0014-multi-agent-patterns.md) | Subagent handoff, Planner, Critic, third-party tool discovery | Accepted |
| [0015](0015-advanced-retrieval.md) | BM25 keyword index, hybrid fusion, reranker, query rewriting, compression | Accepted |
| [0016](0016-compiler-and-app-export.md) | Compiler: IR to a standalone, vendored, tested FastAPI project | Accepted |
| [0017](0017-evaluation-lab.md) | Evaluation Lab: versioned datasets, evaluators, judges, comparison | Accepted |
| [0018](0018-semantic-database-nodes.md) | Semantic data nodes: read-only database access, SQL policy, NL-to-SQL | Accepted |
| [0019](0019-auto-ontology-adapter.md) | NVIDIA Auto Ontology adapter: pinned commit, contract tests, capability checks | Accepted |
| [0020](0020-deployment-center.md) | Deployment Center: capability checks, hash-bound plans, local Docker deploy, rollback | Accepted |
| [0021](0021-node-packages-isolated-workers.md) | Node packages: installed wheels, isolated workers, approved permissions, SDK CLI | Accepted |
| [0022](0022-ai-workflow-designer.md) | AI workflow designer: validated whole-graph proposals, repair loop, revision + undo | Accepted |
| [0023](0023-authentication-and-settings.md) | Sign-in, profiles, admin/member roles, hashed-cookie sessions, workspace settings (assistant model) | Accepted |
| [0024](0024-model-router-formats-project-deletion.md) | Model Router node (keywords/classifier/hybrid), XLSX/PPTX/EPUB/JSONL/TSV/XML/YAML uploads, project deletion confirmed with DELETE | Accepted |
| [0025](0025-vision-local-models-ocr.md) | Image port type, Image Input/Loader, vision LLM 1.2.0, Ollama + Hugging Face providers, RapidOCR (OCR node, scanned PDFs) | Accepted |
| [0026](0026-data-sources-databases-s3-secrets.md) | Database engines (Supabase/Postgres, MySQL, SQL Server, Snowflake, Databricks, DuckDB, ClickHouse, BigQuery), S3 Loader, encrypted workspace secrets | Accepted |
| [0027](0027-chunking-strategies-layout-parsing.md) | Chunker 1.1.0 (sentence, layout, semantic, document, parent-child), Parent Expander, layout-aware PDF/HTML/DOCX parsing | Accepted |

Template: Context → Decision → Consequences → Revisit when.
