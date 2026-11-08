# registry-service (admin & authoring plane): FastAPI REST wrapper + mounted MCP server.
# Build from the repo root:  docker build -f services/platform/registry-service/Dockerfile .
# Same layout as the other Python services: uv-built venv, slim non-root runtime, no build tools in the final image.

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.11-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0 UV_PYTHON_DOWNLOADS=never
COPY services/common /app/services/common
COPY services/platform/registry-service/pyproject.toml services/platform/registry-service/uv.lock /app/services/platform/registry-service/
WORKDIR /app/services/platform/registry-service
RUN uv sync --frozen --no-cache --no-install-project --no-dev \
    && find .venv \( -type d \( -name "__pycache__" -o -name "tests" -o -name "test" \) \) -prune -exec rm -rf {} + \
    && find .venv -type f \( -name "*.pyc" -o -name "*.pyi" \) -delete

FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/services/platform/registry-service/.venv/bin:$PATH" \
    PYTHONPATH=/app/services/platform/registry-service/src
WORKDIR /app/services/platform/registry-service
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=builder /app/services/platform/registry-service/.venv ./.venv
COPY services/platform/registry-service/src ./src
EXPOSE 8005
USER 10001
CMD ["uvicorn", "registry_service.server:app", "--host", "0.0.0.0", "--port", "8005", "--no-access-log"]
