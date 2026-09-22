# Fundscout app image: the CLI, worker and API, plus the built admin UI.

FROM node:22-slim AS admin-ui
WORKDIR /ui
COPY admin-ui/package.json admin-ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY admin-ui/ ./
RUN npm run build

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    RAW_STORAGE_DIR=/app/data/raw

# Dependencies first (cached unless the lock file changes).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src
COPY sources ./sources
RUN uv sync --frozen --no-dev
COPY --from=admin-ui /ui/dist ./admin-ui/dist

RUN useradd --create-home --uid 1000 fundscout && mkdir -p /app/data && chown fundscout /app/data
USER fundscout
CMD ["fundscout", "worker"]
