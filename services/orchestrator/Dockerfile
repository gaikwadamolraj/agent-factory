# ATS orchestrator (FastAPI /api/v1 + LangGraph assistant)
# Build from the repo root:  docker build -f services/orchestrator/Dockerfile .
# Same layout for every Python service: uv builds the venv (incl. services/common), the runtime image is slim
# Python + venv + code, non-root. The venv keeps its build path so console scripts keep working.

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.11-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0 UV_PYTHON_DOWNLOADS=never
COPY services/common /app/services/common
COPY services/orchestrator/pyproject.toml services/orchestrator/uv.lock /app/services/orchestrator/
WORKDIR /app/services/orchestrator
RUN uv sync --frozen --no-cache --no-install-project --no-dev \
    && find .venv \( -type d \( -name "__pycache__" -o -name "tests" -o -name "test" \) \) -prune -exec rm -rf {} + \
    && find .venv -type f \( -name "*.pyc" -o -name "*.pyi" \) -delete

FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/services/orchestrator/.venv/bin:$PATH" PYTHONPATH=/app/services/orchestrator/src
WORKDIR /app/services/orchestrator
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=builder /app/services/orchestrator/.venv ./.venv
COPY services/orchestrator/src ./src
EXPOSE 5000
USER 10001
CMD ["uvicorn", "orchestrator.main:app", "--host", "0.0.0.0", "--port", "5000", "--no-access-log"]
