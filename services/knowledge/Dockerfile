# Shared knowledge-graph service (Graphiti + FalkorDB, MCP server and ingestion worker)
# Build from the repo root:  docker build -f services/knowledge/Dockerfile .
# Same layout for every Python service: uv builds the venv (incl. services/common), the runtime image is slim
# Python + venv + code, non-root. The venv keeps its build path so console scripts keep working.

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.11-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0 UV_PYTHON_DOWNLOADS=never
COPY services/common /app/services/common
COPY services/knowledge/pyproject.toml services/knowledge/uv.lock /app/services/knowledge/
WORKDIR /app/services/knowledge
RUN uv sync --frozen --no-cache --no-install-project --no-dev \
    && find .venv \( -type d \( -name "__pycache__" -o -name "tests" -o -name "test" \) \) -prune -exec rm -rf {} + \
    && find .venv -type f \( -name "*.pyc" -o -name "*.pyi" \) -delete

FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/services/knowledge/.venv/bin:$PATH" PYTHONPATH=/app/services/knowledge/src
WORKDIR /app/services/knowledge
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=builder /app/services/knowledge/.venv ./.venv
COPY services/knowledge/src ./src
EXPOSE 8002
USER 10001
# Worker: same image, command ["python", "-m", "ats_knowledge.worker"]
CMD ["uvicorn", "ats_knowledge.api.server:app", "--host", "0.0.0.0", "--port", "8002", "--no-access-log"]
