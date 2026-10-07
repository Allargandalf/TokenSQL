# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.12.18 AS uv

FROM python:3.10-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy
WORKDIR /build

COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --extra ui --no-editable

FROM python:3.10-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
RUN groupadd --system tokensql \
    && useradd --system --gid tokensql --home-dir /app --no-create-home tokensql \
    && mkdir -p /data \
    && chown -R tokensql:tokensql /app /data

COPY --chown=tokensql:tokensql alembic ./alembic
COPY --chown=tokensql:tokensql alembic.ini ./

USER tokensql
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"]

ENTRYPOINT ["tokensql"]
CMD ["serve-api", "--host", "0.0.0.0", "--port", "8000"]
