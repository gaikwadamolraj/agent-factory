# ATS schema job: creates the database, runs Alembic migrations, seeds demo data (runs to completion).
# Build from the repo root:  docker build -f services/schema/Dockerfile .

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.11-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0 UV_PYTHON_DOWNLOADS=never
COPY services/common /app/services/common
COPY services/schema/pyproject.toml services/schema/uv.lock /app/services/schema/
WORKDIR /app/services/schema
RUN uv sync --frozen --no-cache --no-install-project --no-dev \
    && find .venv \( -type d \( -name "__pycache__" -o -name "tests" -o -name "test" \) \) -prune -exec rm -rf {} + \
    && find .venv -type f \( -name "*.pyc" -o -name "*.pyi" \) -delete

FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/services/schema/.venv/bin:$PATH" PYTHONPATH=/app/services/schema/src
WORKDIR /app/services/schema
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=builder /app/services/schema/.venv ./.venv
COPY services/schema/src ./src
COPY services/schema/alembic.ini ./
COPY services/schema/migrations ./migrations
COPY services/schema/seed ./seed
USER 10001
CMD ["python", "-m", "ats_schema"]
