# Guardrails agent (NeMo Guardrails + injection classifier on CPU PyTorch), MCP server.
# Build from the repo root:  docker build -f services/agents/agent-guardrail/Dockerfile .
# The classifier model is NOT baked into the image: it is downloaded at start-up into MODEL_CACHE_DIR (a volume in
# k8s, so restarts reuse it). Same layout as the other Python services: uv-built venv, slim non-root runtime.

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.11-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0 UV_PYTHON_DOWNLOADS=never
COPY services/common /app/services/common
COPY services/agents/agent-guardrail/pyproject.toml services/agents/agent-guardrail/uv.lock /app/services/agents/agent-guardrail/
WORKDIR /app/services/agents/agent-guardrail
RUN uv sync --frozen --no-cache --no-install-project --no-dev \
    && find .venv \( -type d \( -name "__pycache__" -o -name "tests" -o -name "test" \) \) -prune -exec rm -rf {} + \
    && find .venv -type f \( -name "*.pyc" -o -name "*.pyi" \) -delete

FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/services/agents/agent-guardrail/.venv/bin:$PATH" PYTHONPATH=/app/services/agents/agent-guardrail/src \
    MODEL_CACHE_DIR=/models HF_HOME=/models HF_HUB_DISABLE_TELEMETRY=1
WORKDIR /app/services/agents/agent-guardrail
RUN useradd --system --uid 10001 --no-create-home app && mkdir -p /models && chown 10001 /models
COPY --from=builder /app/services/agents/agent-guardrail/.venv ./.venv
COPY services/agents/agent-guardrail/src ./src
EXPOSE 8003
USER 10001
CMD ["uvicorn", "agent_guardrail.server:app", "--host", "0.0.0.0", "--port", "8003", "--no-access-log"]
