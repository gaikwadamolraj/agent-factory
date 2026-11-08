# Shared knowledge-graph service (Graphiti + FalkorDB, MCP server and ingestion worker)
# Build from the repo root:  docker build -f services/agents/agent-knowledge-graph/Dockerfile .
# Same layout for every Python service: uv builds the venv (incl. services/common), the runtime image is slim
# Python + venv + code, non-root. The venv keeps its build path so console scripts keep working.

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.11-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0 UV_PYTHON_DOWNLOADS=never
COPY services/common /app/services/common
COPY services/agents/agent-knowledge-graph/pyproject.toml services/agents/agent-knowledge-graph/uv.lock /app/services/agents/agent-knowledge-graph/
WORKDIR /app/services/agents/agent-knowledge-graph
RUN uv sync --frozen --no-cache --no-install-project --no-dev \
    && find .venv \( -type d \( -name "__pycache__" -o -name "tests" -o -name "test" \) \) -prune -exec rm -rf {} + \
    && find .venv -type f \( -name "*.pyc" -o -name "*.pyi" \) -delete

FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/services/agents/agent-knowledge-graph/.venv/bin:$PATH" PYTHONPATH=/app/services/agents/agent-knowledge-graph/src
WORKDIR /app/services/agents/agent-knowledge-graph
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=builder /app/services/agents/agent-knowledge-graph/.venv ./.venv
COPY services/agents/agent-knowledge-graph/src ./src
EXPOSE 8002
USER 10001
# Worker: same image, command ["python", "-m", "agent_knowledge_graph.worker"]
CMD ["uvicorn", "agent_knowledge_graph.api.server:app", "--host", "0.0.0.0", "--port", "8002", "--no-access-log"]
