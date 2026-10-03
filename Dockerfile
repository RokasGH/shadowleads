# syntax=docker/dockerfile:1.7
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app

# dependency layer (cached unless the lockfile changes)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY app ./app
COPY examples ./examples
COPY labels ./labels
COPY queries ./queries
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 1000 analyst && mkdir -p /app/data /app/output \
    && chown -R analyst /app/data /app/output /app/queries
USER analyst

ENTRYPOINT ["shadowleads"]
CMD ["--help"]
