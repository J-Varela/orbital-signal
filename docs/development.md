# Development Guide

These instructions target current development, beyond the tagged Alpha 2
release. Runtime metadata remains `0.1.0-alpha.2`.

## Local environment

Install Python 3.12+, `uv`, Git, and Docker Compose. GitHub CLI is useful for
pull requests and releases. A separately managed PostgreSQL service can replace
Compose.

From the repository root:

```bash
uv sync --locked
docker compose up -d --wait postgres
uv run alembic upgrade head
uv run orbital-signal
```

Compose starts PostgreSQL 17 at `127.0.0.1:55432`, with local development
credentials and a named persistent volume. It starts the database only; run the
API separately. The defaults in `Settings` match the Compose database.

The API listens at `http://127.0.0.1:8000`. Interactive documentation is at
`/docs`. To enable code reload directly:

```bash
uv run uvicorn orbital_signal.api:app --reload
```

Use the API in a trusted local environment. It has no authentication, including
on ingestion and reclassification routes. `/health` confirms application
liveness but does not verify the database or migrations.

To stop the database while retaining its volume:

```bash
docker compose stop postgres
```

## Configuration

Copy `.env.example` to `.env` if overrides are needed. Settings use the
`ORBITAL_SIGNAL_` prefix:

```dotenv
ORBITAL_SIGNAL_USASPENDING_BASE_URL=https://api.usaspending.gov
ORBITAL_SIGNAL_HTTP_TIMEOUT_SECONDS=30
ORBITAL_SIGNAL_DATABASE_URL=postgresql+asyncpg://orbital_signal:orbital_signal@127.0.0.1:55432/orbital_signal
ORBITAL_SIGNAL_DATABASE_ECHO=false
```

`.env` is ignored by Git. Keep credentials and machine-specific overrides out
of commits. USAspending currently requires no API key.

## Schema changes

Alembic reads the same application database settings. Apply checked-in
migrations before starting database-backed requests:

```bash
uv run alembic upgrade head
uv run alembic current
```

For an intentional model change, generate and review a migration:

```bash
uv run alembic revision --autogenerate -m "describe schema change"
```

Inspect generated operations and constraints before applying them. Test upgrade
behavior against PostgreSQL, including upgrades from an existing schema, rather
than relying only on SQLite repository tests. API startup does not create or
migrate tables.

## Verification

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
git diff --check
```

Normal tests do not need the live USAspending service or a PostgreSQL server.
They use mocked HTTP responses, injected repositories, and async SQLite. Keep
new source fixtures and regression checks deterministic.

After changes to source access or persistence, a deliberate live smoke test
can ingest a narrow date range and inspect `/api/v1/signals` and
`/api/v1/companies`. See the [README](../README.md#ingest-and-query) for requests.
Inspect actual evidence rather than treating changing live result counts as a
fixed test expectation.

Reclassification runs current rules over persisted evidence:

```bash
curl --fail-with-body -s -X POST \
  "http://127.0.0.1:8000/api/v1/reclassifications/signals?as_of=2026-10-16"
```

An explicit `as_of` makes recency scoring reproducible. This mutates derived
signals and can remove signals that fail current relevance rules while retaining
their awards. It runs inside the HTTP request without a job queue; schedule
bulk work deliberately and avoid overlapping ingestion and reclassification.

## Review and commit

Use a focused branch when starting new work, without discarding existing local
changes. Before staging, review tracked and untracked files:

```bash
git status --short
git diff
git diff --check
```

Stage intended files explicitly, inspect `git diff --cached`, and use concise
commit messages such as `fix: preserve first signal detection time`. Never stage
local credentials, caches, virtual environments, or database volumes.

Push the current branch to its intended remote and use a pull request when the
repository workflow calls for one. A useful pull request describes the behavior
change, verification, and material remaining limits.

## Dependencies and releases

Manage dependencies with `uv` and commit `pyproject.toml` and `uv.lock` together
when they change. Use `uv sync --locked` to reproduce the checked-in environment.
Run the quality gates after an intended dependency update.

Version metadata currently appears in two locations:

- `pyproject.toml`: `0.1.0a2` (Python package form)
- `src/orbital_signal/__init__.py`: `0.1.0-alpha.2` (display form)

Keep these aligned when preparing a release. Development changes belong under
`Unreleased` in [CHANGELOG.md](../CHANGELOG.md); do not invent a new version for
ordinary review fixes or describe roadmap work as delivered.

For a release, prepare and review the metadata and dated changelog on the
intended branch, run checks, merge according to repository workflow, and tag the
tested release commit. Publish an annotated version tag and release notes only
after confirming the remote commit and release scope. Published tags should not
be moved.
