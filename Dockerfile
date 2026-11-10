
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    ORBITAL_SIGNAL_ENRICHMENT_CACHE_DIRECTORY=/tmp/orbital-signal-cache/enrichment

# Install application files and locked dependencies
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
COPY alembic.ini ./
COPY migrations/ ./migrations/

RUN uv sync --locked --no-dev --no-editable

# Run the application without root privileges
RUN useradd --create-home --uid 10001 orbital
USER orbital

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn orbital_signal.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
