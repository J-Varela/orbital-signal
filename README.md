# Orbital Signal

Orbital Signal turns public federal contract records into explainable evidence
about space companies. It helps users find relevant recipients, prioritize
activity, and inspect the awards behind each result.

Runtime version: `0.1.0-alpha.2` (package version `0.1.0a2`). This documentation
covers current development beyond the tagged Alpha 2 release; see
[Unreleased changes](CHANGELOG.md#unreleased).

## What it does today

```text
USAspending NASA and DoD prime contracts
    -> normalized award evidence and transaction-date enrichment
    -> explainable space relevance, recipient quality, and priority scoring
    -> PostgreSQL companies, aliases, awards, signals, and ingestion runs
    -> FastAPI signal feeds and aggregated company profiles
```

- Searches NASA and Department of Defense separately, with bounded pagination.
- Preserves source identifiers, evidence URLs, award dates, and first detection
  times across repeated ingestion.
- Separates space relevance from heuristic startup-candidate eligibility.
- Ranks signals using relevance, transaction recency, award value, candidate
  eligibility, and technology terms, with reasons attached to each score.
- Filters signals by relevance, priority, and startup-candidate eligibility.
- Aggregates company signal counts, award amounts, agencies, and latest dates.
- Reclassifies persisted evidence using current rules without another source
  fetch, preserving evidence for later reconsideration.
- Records ingestion outcomes and retries selected transient upstream failures.

There is no dashboard, scheduler, alert delivery, or additional source adapter
yet. A focused product direction is described in the [roadmap](docs/roadmap.md).

## Run locally

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and Docker Compose (or a
separately managed PostgreSQL database).

```bash
uv sync --locked
docker compose up -d --wait postgres
uv run alembic upgrade head
uv run orbital-signal
```

The Compose service runs PostgreSQL 17 on `127.0.0.1:55432` and stores data in a
named volume. The application's default database URL matches that service.
Migrations must run before database-backed API requests; startup does not create
the schema automatically.

Copy `.env.example` to `.env` when overrides are needed. `.env` is ignored by Git.
For another database, set `ORBITAL_SIGNAL_DATABASE_URL` to an async SQLAlchemy
PostgreSQL URL such as `postgresql+asyncpg://user:password@host:5432/database`.

The API listens at `http://127.0.0.1:8000`; interactive documentation is at
[`/docs`](http://127.0.0.1:8000/docs). This is a local, trusted API with no
authentication. Ingestion and reclassification routes mutate stored data.

```bash
curl --fail-with-body -s http://127.0.0.1:8000/health
```

The response is `{"status":"ok","version":"0.1.0-alpha.2"}`. This checks
application liveness, not database connectivity, migration status, or source
availability.

## Ingest and query

With the API running:

```bash
curl --fail-with-body -s -X POST \
  "http://127.0.0.1:8000/api/v1/ingestions/usaspending?start_date=2026-01-01&end_date=2026-10-16"

curl --fail-with-body -s \
  "http://127.0.0.1:8000/api/v1/signals?minimum_score=4&minimum_priority_score=40&startup_candidates_only=true&limit=25"

curl --fail-with-body -s \
  "http://127.0.0.1:8000/api/v1/companies?startup_candidates_only=true&limit=25"
```

USAspending currently requires no API key. A large date range can hit the
five-page-per-agency limit and does not guarantee complete coverage.

After scoring rules change, recompute retained evidence locally:

```bash
curl --fail-with-body -s -X POST \
  "http://127.0.0.1:8000/api/v1/reclassifications/signals?as_of=2026-10-16"
```

`as_of` controls priority recency scoring and defaults to the server's current
date. Reclassification returns scanned, reclassified, and removed counts;
removing a derived signal preserves the underlying award evidence.

## API

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Application liveness and version |
| `POST` | `/api/v1/ingestions/usaspending` | Ingest awards for a required date range |
| `GET` | `/api/v1/signals` | Ranked signals with relevance, priority, and candidate filters |
| `GET` | `/api/v1/companies` | Ranked company profiles, optionally candidate-only |
| `POST` | `/api/v1/reclassifications/signals` | Recompute classifications from local evidence |

Signal queries accept `minimum_score` (default `4`),
`minimum_priority_score` (default `0`), `startup_candidates_only` (default
`false`), and `limit` (default `100`, maximum `500`). Company queries accept
`startup_candidates_only` and the same limit bounds.

## Understand the evidence

Space relevance uses deterministic agency, text, NAICS, and PSC rules with a
default threshold of `4`. Agency membership alone does not qualify an award.
The live USAspending adapter currently populates agency and text evidence, not
NAICS or PSC codes.

Startup-candidate status means a recipient passes commercial-name and event
quality rules, including a `$25,000` minimum amount and exclusions for known
established contractors, institutions, and administrative or training events.
It does not verify company age, ownership, funding stage, or venture backing.

`occurred_on` uses the latest available transaction `action_date`, falling back
to the award start date. Priority recency points require an action date.
`detected_at` records first observation separately. The latest transaction date
can be outside the requested ingestion window; these remain award-level
signals, not a transaction ledger.

Profile `total_amount` sums source award amounts across retained signals. It is
not verified revenue, new obligations in the search window, or proof of growth.
Relevance and priority are explainable ranking heuristics, not investment or
financial-health assessments. Inspect the linked source evidence.

## Operational boundaries

- Runtime storage is PostgreSQL; the in-memory repository is for isolated tests.
- Ingestion fetches at most five pages of 100 awards per agency (1,000 records
  before deduplication for the two default agencies).
- Transaction enrichment can add one upstream request per award with an
  internal identifier, with bounded concurrency and selective retries.
- Ingestion and reclassification run within the HTTP request. Company profile
  aggregation and reclassification load their inputs into application memory.
- Ingestion commits records incrementally. A failed run can leave earlier
  records persisted, and a process interruption can leave a run marked running.
- Concurrent ingestion and reclassification are not coordinated by a job queue.

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Tests use mocked upstream requests and injected repositories; database tests
use SQLite without a live PostgreSQL service. For real deployment changes,
also validate migrations and behavior against PostgreSQL.

## Documentation

- [Architecture](docs/architecture.md)
- [Data sources and interpretation](docs/data-sources.md)
- [Development and release workflow](docs/development.md)
- [Product direction](docs/roadmap.md)
- [Changelog](CHANGELOG.md)

Repository: [J-Varela/orbital-signal](https://github.com/J-Varela/orbital-signal)
