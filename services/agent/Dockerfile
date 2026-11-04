# ATS screening agent (Strands + MCP server)
# Build from the repo root:  docker build -f services/agent/Dockerfile .
# Same layout for every Python service: uv builds the venv (incl. services/common), the runtime image is slim
# Python + venv + code, non-root. The venv keeps its build path so console scripts keep working.

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.11-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0 UV_PYTHON_DOWNLOADS=never
COPY services/common /app/services/common
COPY services/agent/pyproject.toml services/agent/uv.lock /app/services/agent/
WORKDIR /app/services/agent
RUN uv sync --frozen --no-cache --no-install-project --no-dev \
    && find .venv \( -type d \( -name "__pycache__" -o -name "tests" -o -name "test" \) \) -prune -exec rm -rf {} + \
    && find .venv -type f \( -name "*.pyc" -o -name "*.pyi" \) -delete

FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/services/agent/.venv/bin:$PATH" PYTHONPATH=/app/services/agent/src
WORKDIR /app/services/agent
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=builder /app/services/agent/.venv ./.venv
COPY services/agent/src ./src
EXPOSE 8001
USER 10001
CMD ["uvicorn", "ats_agent.server:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]
