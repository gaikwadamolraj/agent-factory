# ATS schema job: creates the database, runs Alembic migrations, seeds demo data (runs to completion).
# Build from the repo root:  docker build -f services/platform/schema/Dockerfile .

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.11-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0 UV_PYTHON_DOWNLOADS=never
COPY services/common /app/services/common
COPY services/platform/schema/pyproject.toml services/platform/schema/uv.lock /app/services/platform/schema/
WORKDIR /app/services/platform/schema
RUN uv sync --frozen --no-cache --no-install-project --no-dev \
    && find .venv \( -type d \( -name "__pycache__" -o -name "tests" -o -name "test" \) \) -prune -exec rm -rf {} + \
    && find .venv -type f \( -name "*.pyc" -o -name "*.pyi" \) -delete

FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/services/platform/schema/.venv/bin:$PATH" PYTHONPATH=/app/services/platform/schema/src
WORKDIR /app/services/platform/schema
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=builder /app/services/platform/schema/.venv ./.venv
COPY services/platform/schema/src ./src
COPY services/platform/schema/alembic.ini ./
COPY services/platform/schema/migrations ./migrations
COPY services/platform/schema/seed ./seed
USER 10001
CMD ["python", "-m", "ats_schema"]
