# ATS compute agent (Strands + MCP server; runs code in ats-sandbox)
# Build from the repo root:  docker build -f services/compute/Dockerfile .
# Same layout for every Python service: uv builds the venv (incl. services/common), the runtime image is slim
# Python + venv + code, non-root. The venv keeps its build path so console scripts keep working.

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.11-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0 UV_PYTHON_DOWNLOADS=never
COPY services/common /app/services/common
COPY services/compute/pyproject.toml services/compute/uv.lock /app/services/compute/
WORKDIR /app/services/compute
RUN uv sync --frozen --no-cache --no-install-project --no-dev \
    && find .venv \( -type d \( -name "__pycache__" -o -name "tests" -o -name "test" \) \) -prune -exec rm -rf {} + \
    && find .venv -type f \( -name "*.pyc" -o -name "*.pyi" \) -delete

FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/services/compute/.venv/bin:$PATH" PYTHONPATH=/app/services/compute/src
WORKDIR /app/services/compute
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=builder /app/services/compute/.venv ./.venv
COPY services/compute/src ./src
EXPOSE 8002
USER 10001
CMD ["uvicorn", "ats_compute.server:app", "--host", "0.0.0.0", "--port", "8002", "--no-access-log"]
