FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /uvx /bin/
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY scripts ./scripts
COPY supabase ./supabase
RUN uv sync --locked --no-dev --no-editable \
    && useradd --create-home --uid 10001 agent \
    && chown -R agent:agent /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 CREWAI_STORAGE_DIR=/tmp/crewai OTEL_SDK_DISABLED=true
USER agent
EXPOSE 8000
CMD ["serve"]
