# ATS compute agent (Strands + MCP server; runs code in agent-sandbox)
# Build from the repo root:  docker build -f services/agents/agent-compute/Dockerfile .
# Same layout for every Python service: uv builds the venv (incl. services/common), the runtime image is slim
# Python + venv + code, non-root. The venv keeps its build path so console scripts keep working.

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.11-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0 UV_PYTHON_DOWNLOADS=never
COPY services/common /app/services/common
COPY services/agents/agent-compute/pyproject.toml services/agents/agent-compute/uv.lock /app/services/agents/agent-compute/
WORKDIR /app/services/agents/agent-compute
RUN uv sync --frozen --no-cache --no-install-project --no-dev \
    && find .venv \( -type d \( -name "__pycache__" -o -name "tests" -o -name "test" \) \) -prune -exec rm -rf {} + \
    && find .venv -type f \( -name "*.pyc" -o -name "*.pyi" \) -delete

FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/services/agents/agent-compute/.venv/bin:$PATH" PYTHONPATH=/app/services/agents/agent-compute/src
WORKDIR /app/services/agents/agent-compute
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=builder /app/services/agents/agent-compute/.venv ./.venv
COPY services/agents/agent-compute/src ./src
EXPOSE 8002
USER 10001
CMD ["uvicorn", "agent_compute.server:app", "--host", "0.0.0.0", "--port", "8002", "--no-access-log"]
