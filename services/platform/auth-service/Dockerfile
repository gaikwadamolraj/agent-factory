# ATS auth-service (FastAPI: sign-in, user management, ES256 tokens + JWKS, client_credentials)
# Build from the repo root:  docker build -f services/platform/auth-service/Dockerfile .
# Same layout for every Python service: uv builds the venv (incl. services/common), the runtime image is slim
# Python + venv + code, non-root. The venv keeps its build path so console scripts keep working.

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.11-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0 UV_PYTHON_DOWNLOADS=never
COPY services/common /app/services/common
COPY services/platform/auth-service/pyproject.toml services/platform/auth-service/uv.lock /app/services/platform/auth-service/
WORKDIR /app/services/platform/auth-service
RUN uv sync --frozen --no-cache --no-install-project --no-dev \
    && find .venv \( -type d \( -name "__pycache__" -o -name "tests" -o -name "test" \) \) -prune -exec rm -rf {} + \
    && find .venv -type f \( -name "*.pyc" -o -name "*.pyi" \) -delete

FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/services/platform/auth-service/.venv/bin:$PATH" PYTHONPATH=/app/services/platform/auth-service/src
WORKDIR /app/services/platform/auth-service
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=builder /app/services/platform/auth-service/.venv ./.venv
COPY services/platform/auth-service/src ./src
EXPOSE 8004
USER 10001
CMD ["uvicorn", "auth_service.main:app", "--host", "0.0.0.0", "--port", "8004", "--no-access-log"]
