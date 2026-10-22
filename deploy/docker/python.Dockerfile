# One image for every Python service; the command picks the service (see deploy/helm/adce/values.yaml):
#   python -m api_gateway                 API gateway (runs the orchestrator in process for now)
#   python -m kg_mcp.server               knowledge tools over MCP
#   uvicorn hitl_service.api:app ...      review service
#   alembic -c migrations/alembic.ini upgrade head   schema migrations (Helm pre-install/upgrade hook)
# Build from the repository root:  docker build -f deploy/docker/python.Dockerfile -t adce-python .
FROM ghcr.io/astral-sh/uv:0.11.21 AS uv

FROM python:3.12-slim AS build
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY services services
RUN uv sync --frozen --no-dev --all-packages --all-extras --no-editable

FROM python:3.12-slim
RUN useradd --system --uid 10001 --home /app adce
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY prompts prompts
COPY migrations migrations
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    ADCE_PROMPTS_DIR=/app/prompts \
    GRAPHITI_TELEMETRY_ENABLED=false
USER 10001
EXPOSE 8700 8701 8702
CMD ["python", "-m", "api_gateway", "--port", "8700"]
