# Orbital Signal

Orbital Signal is an early-warning intelligence system for emerging space
companies.

It connects public government-award evidence, recipient identities, explainable
space-relevance scoring, and event-quality classification so users can see where
space funding and contract activity are moving.

Current application version: `v0.1.0-alpha.2`

## Current capability

Orbital Signal implements its first complete intelligence path:

```text
USAspending API
    -> normalized federal award
    -> space-relevance assessment
    -> organization and event-quality assessment
    -> queryable company signal
```

The system currently:

- Searches NASA and Department of Defense prime contracts.
- Normalizes external awards into typed domain models.
- Scores space relevance using deterministic, explainable rules.
- Separates space relevance from startup-candidate eligibility.
- Preserves source identifiers and direct evidence URLs.
- Deduplicates awards and signals with stable identifiers.
- Exposes ingestion and signal retrieval through FastAPI.
- Supports a startup-candidate-only feed.
- Runs unit and API tests without live-network access.

## Why the classifications are separate

A record can be strongly space-related without being useful for tracking an
emerging company.

Examples include:

- University research awards
- Conference and exhibit purchases
- Training contracts
- Minor equipment service modifications
- Awards to established research laboratories

Orbital Signal therefore asks two separate questions:

1. Is this event related to the space sector?
2. Does this event belong in the startup-candidate feed?

Raw space-relevant evidence remains available even when an event is excluded
from the startup-candidate view.

`startup_candidate=true` currently means a commercial recipient with a
qualifying space-related event. It is not a verified claim about the company's
age, funding stage, ownership, or venture backing.

## Technology

- Python 3.12
- FastAPI
- Pydantic
- Pydantic Settings
- HTTPX
- Uvicorn
- pytest
- Ruff
- uv

## Setup

Clone the GitHub repository:

```bash
cd ~/dev/hermes-lab/apps

git clone https://github.com/J-Varela/orbital-signal.git

cd orbital-signal
```

Install the locked environment:

```bash
uv sync
```

`uv sync` creates `.venv` automatically when necessary. Manual activation is
optional when commands are run through `uv run`.

Run the quality gates:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## Run the API

Start the development server:

```bash
uv run orbital-signal
```

Or run Uvicorn directly:

```bash
uv run uvicorn orbital_signal.api:app --reload
```

Open the interactive API documentation:

```text
http://127.0.0.1:8000/docs
```

Verify the service:

```bash
curl -s http://127.0.0.1:8000/health \
  | python3 -m json.tool
```

Expected response:

```json
{
  "status": "ok",
  "version": "0.1.0-alpha.2"
}
```

## First live ingestion

With the API running, ingest a date range:

```bash
curl -s -X POST \
  "http://127.0.0.1:8000/api/v1/ingestions/usaspending?start_date=2026-01-01&end_date=2026-08-25" \
  | python3 -m json.tool
```

Retrieve retained space signals:

```bash
curl -s \
  "http://127.0.0.1:8000/api/v1/signals?minimum_score=4&limit=25" \
  | python3 -m json.tool
```

Retrieve the cleaner startup-candidate feed:

```bash
curl -s \
  "http://127.0.0.1:8000/api/v1/signals?minimum_score=4&limit=25&startup_candidates_only=true" \
  | python3 -m json.tool
```

The public USAspending API currently requires no API key.

## Relevance scoring

Every retained signal records the evidence used to calculate its score.

| Evidence | Points |
| --- | ---: |
| Space-focused awarding organization | 3 |
| Strong space term | 4 |
| Supporting aerospace term | 2 |
| Space-manufacturing NAICS code | 5 |
| Space-related PSC code | 4 |

The default inclusion threshold is `4`.

Agency membership alone is insufficient. This prevents ordinary NASA facility
and administrative contracts from entering the signal feed solely because NASA
issued them.

The current USAspending adapter does not yet populate NAICS or PSC fields, so
live Alpha 2 USAspending records are presently evaluated through agency and text
evidence.

## API

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Return service status and version |
| `POST` | `/api/v1/ingestions/usaspending` | Ingest awards for a date range |
| `GET` | `/api/v1/signals` | Retrieve retained signals |

Signal retrieval supports:

- `minimum_score`
- `limit`
- `startup_candidates_only`

## Documentation

- [Changelog](CHANGELOG.md)
- [Architecture](docs/architecture.md)
- [Data sources](docs/data-sources.md)
- [Development and release workflow](docs/development.md)

## Current boundary

Alpha 2 stores signals in memory.

Restarting the API clears all ingested records, and multiple application
processes do not share state.

The next milestone will introduce:

- PostgreSQL persistence
- Alembic migrations
- Durable ingestion runs
- Canonical company identities
- Company aliases
- Idempotent database upserts
- Explicit award-date semantics

Additional sources such as SEC Form D, NASA procurement forecasts, and SBIR/STTR
records will follow after the durable storage and identity layers are established.

## Evidence and interpretation

Each signal preserves its source award identifier and USAspending evidence URL.

A relevance score expresses how strongly the available evidence relates to the
space sector. It does not represent:

- Investment quality
- Financial health
- Confirmed startup status
- Guaranteed future revenue
- Confirmation of private fundraising

Users should inspect the underlying source evidence before making business,
employment, or investment decisions.

## Repository

GitHub: [J-Varela/orbital-signal](https://github.com/J-Varela/orbital-signal)