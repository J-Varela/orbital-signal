# Orbital Signal

Orbital Signal is an early-warning intelligence system for emerging space companies. It
connects government awards, procurement activity, private-capital signals, and company
identities so users can see where space funding and government work are moving.

This `v0.1.0-alpha.2` foundation implements the first complete path:

```text
USAspending API -> normalized award -> space relevance scoring -> signal API
```

## What is included

- A typed asynchronous USAspending client
- NASA and Department of Defense award searches
- Explainable space-relevance scoring using agencies, terms, NAICS codes, and PSC codes
- An in-memory repository with deterministic award deduplication
- FastAPI endpoints for ingestion and signal retrieval
- Organization classification and event-quality flags
- A startup-candidate view that stays separate from raw space relevance
- Unit and API tests with no live-network dependency

## Set up

```bash
cd ~/dev/hermes-lab/apps
unzip orbital-signal-v0.1.0-alpha.1.zip
cd orbital-signal
uv sync
```

Run quality checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Start the API:

```bash
uv run uvicorn orbital_signal.api:app --reload
```

Then open <http://127.0.0.1:8000/docs>.

## First live ingestion

With the API running:

```bash
curl -X POST \
  "http://127.0.0.1:8000/api/v1/ingestions/usaspending?start_date=2026-01-01&end_date=2026-08-24"

curl "http://127.0.0.1:8000/api/v1/signals?minimum_score=4"

curl \
  "http://127.0.0.1:8000/api/v1/signals?minimum_score=4&startup_candidates_only=true"
```

The public USAspending API currently requires no API key. The ingestion request asks for
prime contract awards from NASA and the Department of Defense, then retains records whose
evidence crosses the relevance threshold.

## Relevance scoring

The scoring system is deliberately explainable. Every signal records the evidence that
caused it to be retained.

| Evidence | Points |
| --- | ---: |
| NASA or a named military space organization | 3 |
| Strong space term such as `spacecraft`, `orbital`, or `lunar` | 4 |
| Supporting aerospace term such as `propulsion` or `telemetry` | 2 |
| Space manufacturing NAICS code | 5 |
| Space research/product PSC code | 4 |

The default inclusion threshold is 4. Agency membership alone is not enough, which prevents
ordinary NASA facility and administrative contracts from dominating the feed.

## API

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Service status and release version |
| `POST` | `/api/v1/ingestions/usaspending` | Fetch and classify awards for a date range |
| `GET` | `/api/v1/signals` | List retained signals by descending relevance |

Use `startup_candidates_only=true` to hide academic institutions, obvious training or
administrative purchases, and events below the current $25,000 quality floor. This is a
candidate classification, not a verified claim that the recipient is a startup.

## Current boundary

Storage is intentionally in-memory in this alpha. Restarting the API clears imported signals.
The next release should introduce PostgreSQL, Alembic migrations, company identity aliases,
and durable ingestion runs before adding SEC Form D and NASA forecast sources.

## Evidence and attribution

Each signal includes a source URL and the source award identifier. Scores indicate relevance,
not investment quality, company quality, or confirmation of a private funding round.
