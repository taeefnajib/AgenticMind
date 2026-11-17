# ADR-0002: Monorepo layout and starting stack

**Decision.** One repository with a uv workspace (Python packages: `packages/*`, `nodes/*`, `apps/api`)
and an npm workspace (`apps/web`). Stack: Python 3.11 + Pydantic 2 + FastAPI + SQLAlchemy 2; React 18 +
TypeScript (strict) + Vite + React Flow (`@xyflow/react`) + Tailwind.

Deviation from the blueprint: shadcn/ui is not installed yet; the small component set is hand-written
with Tailwind to keep the foundation lean. Adopt shadcn/ui when the component surface grows (dialogs,
menus, toasts).

**Consequences.** Each subsystem is an independently versioned package with explicit dependencies, so the
runtime can be embedded in generated projects without the API.
