# Single-container AgenticMind: FastAPI control plane serving the built web IDE at /.
# Build from the repository root:  docker build -f infra/docker/Dockerfile -t agenticmind .

FROM node:22-alpine AS web
WORKDIR /src
COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/
RUN npm ci --workspace apps/web --include-workspace-root=false
COPY apps/web apps/web
RUN npm run build -w apps/web

FROM python:3.11-slim AS app
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY nodes nodes
COPY apps/api apps/api
RUN uv sync --frozen --no-dev --package agenticmind-api
COPY --from=web /src/apps/web/dist /app/web
ENV PATH="/app/.venv/bin:$PATH" \
    AGENTICMIND_WEB_DIST=/app/web \
    AGENTICMIND_DATABASE_URL=sqlite:////data/agenticmind.db
RUN useradd --create-home --uid 10001 agenticmind && mkdir -p /data && chown agenticmind /data
USER agenticmind
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "--factory", "agenticmind_api.main:app_factory", "--host", "0.0.0.0", "--port", "8000"]
