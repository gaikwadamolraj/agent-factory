# Document-reader agent (PDF/DOCX/XLSX extraction + guardrail scan + chunk/embed), MCP server.
# Build from the repo root:  docker build -f services/agents/agent-document/Dockerfile .
# Same layout for every Python service: uv builds the venv (incl. services/common), the runtime image is slim
# Python + venv + code, non-root. The venv keeps its build path so console scripts keep working.
#
# Extraction tiers: fast (pypdfium2, text only) and tables (pdfplumber, ruled tables) are always installed.
# The docling tier (layout ML + optional OCR, multi-GB models) is an OPTIONAL extra and is left out by default:
#   docker build -f services/agents/agent-document/Dockerfile --build-arg WITH_DOCLING=1 .
# With WITH_DOCLING=1 the builder installs the `docling` optional-dependency group and bakes the models into
# /models, and the runtime image points DOCLING_ARTIFACTS_PATH at them. Without it the router simply falls back
# to the lighter tiers, so the default image runs unchanged.

ARG WITH_DOCLING=0

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.11-slim-bookworm AS builder
ARG WITH_DOCLING
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0 UV_PYTHON_DOWNLOADS=never
COPY services/common /app/services/common
COPY services/agents/agent-document/pyproject.toml services/agents/agent-document/uv.lock /app/services/agents/agent-document/
WORKDIR /app/services/agents/agent-document
# The extra only resolves if uv.lock carries it (re-run `uv lock` in this service after changing the group).
RUN uv sync --frozen --no-cache --no-install-project --no-dev \
        $( [ "$WITH_DOCLING" = "1" ] && echo --extra docling ) \
    && find .venv \( -type d \( -name "__pycache__" -o -name "tests" -o -name "test" \) \) -prune -exec rm -rf {} + \
    && find .venv -type f \( -name "*.pyc" -o -name "*.pyi" \) -delete
# Pre-download the layout/OCR models so the runtime image never reaches the network on the first PDF.
# NOTE: the `docling-tools` console script ships with docling>=2.x; verify the name against the installed
# package the first time this image is built with the extra, and adjust here if upstream renamed it.
RUN mkdir -p /models \
    && if [ "$WITH_DOCLING" = "1" ]; then .venv/bin/docling-tools models download --output-dir /models; fi

FROM python:3.11-slim-bookworm AS runtime-base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/services/agents/agent-document/.venv/bin:$PATH" PYTHONPATH=/app/services/agents/agent-document/src
WORKDIR /app/services/agents/agent-document
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=builder /app/services/agents/agent-document/.venv ./.venv
COPY services/agents/agent-document/src ./src
EXPOSE 8006
USER 10001
CMD ["uvicorn", "agent_document.server:app", "--host", "0.0.0.0", "--port", "8006", "--no-access-log"]

# WITH_DOCLING picks one of the two runtime variants below; both keep the non-root user and carry no build tools.
FROM runtime-base AS runtime-0

FROM runtime-base AS runtime-1
USER root
COPY --from=builder --chown=10001:0 /models /models
USER 10001
ENV DOCLING_ARTIFACTS_PATH=/models

FROM runtime-${WITH_DOCLING} AS runtime
