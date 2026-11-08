# agent-sandbox: Python code executor for the compute agent (standard library only, no secrets, no network).
# Build:  docker build -t agent-sandbox:dev services/agents/agent-sandbox
FROM python:3.13-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --system --uid 10002 --no-create-home sandbox
WORKDIR /app
COPY executor.py /app/executor.py
EXPOSE 8080
USER 10002
CMD ["python", "-I", "/app/executor.py"]
