# Changelog

All notable changes to Orbital Signal will be documented in this file.

The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

These changes describe current development beyond `v0.1.0-alpha.2`. Runtime
version metadata remains `0.1.0-alpha.2` until a release is prepared.

### Added

- PostgreSQL runtime persistence through async SQLAlchemy, with Alembic
  migrations and a local Docker Compose database.
- Durable companies, aliases, normalized awards, signals, and ingestion runs.
- Explainable priority scoring and `minimum_priority_score` signal filtering.
- Aggregated company intelligence profiles at `GET /api/v1/companies`.
- Local evidence reclassification at
  `POST /api/v1/reclassifications/signals`, with an optional scoring date.
- USAspending latest transaction action-date enrichment and selective retries
  for transient upstream failures.
- A focused roadmap for reliable company evidence collection, timelines,
  watchlists, alerts, complementary sources, and a simple dashboard.

### Changed

- Upgraded pytest from 8.4.2 to 9.1.1.
- Updated the reproducible `uv.lock` dependency lockfile.
- Signal event dates now prefer transaction action dates, falling back to
  award start dates; action-date search filters are explicit.
- Known established contractors are excluded from the heuristic candidate feed.
- Updated setup, architecture, source semantics, and operational documentation
  to reflect the current application.

### Fixed

- Repeated ingestion preserves first detection time instead of making old
  evidence appear newly observed.
- Reclassification preserves award evidence and detection history so removal
  of a derived signal can be reversed by later rules.
- Malformed upstream data returns `502 Bad Gateway` instead of an unhandled
  internal error.
- Canonical USAspending identifiers prevent awards with the same display ID
  from collapsing across agencies; legacy persisted signals retain their IDs
  when matched during re-ingestion.
- Company matching keeps distinct UEIs separate despite matching names, and
  profile grouping normalizes UEI casing.
- Contractor and technology term boundaries reduce substring false positives;
  future transaction dates receive no priority recency boost.

### Known limitations

- The API assumes trusted local clients and has no authentication.
- Startup-candidate status is heuristic, not verified company-stage data.
- USAspending ingestion is bounded to five pages per configured agency.
- Latest transaction enrichment can produce dates outside the requested search
  window; records are award snapshots rather than transaction histories.
- Ingestion and reclassification run inside API requests, and bulk profile and
  reclassification inputs are loaded into memory.
- No scheduler, job queue, coverage/truncation reporting, or automatic recovery
  of interrupted ingestion runs is implemented.
- SQLite persistence tests do not replace PostgreSQL deployment validation.
- The FastAPI test client emits an upstream `httpx2` deprecation warning.

## [0.1.0-alpha.2] - 2026-08-25

First Git-tracked public release.

### Added

- Organization classification for companies, academic or research institutions,
  government or nonprofit recipients, and unknown organizations.
- Startup-candidate classification kept separate from space-relevance scoring.
- Quality flags for low-dollar, administrative, training, and non-company events.
- A default `$25,000` minimum event-value threshold for startup candidates.
- `startup_candidates_only` filtering on the signals API.
- Regression tests based on live NASA and Department of Defense award results.
- False-positive protection for the phrase `orbital welding`.
- Public GitHub repository and annotated `v0.1.0-alpha.2` release tag.

### Changed

- Refined the startup feed so raw space-relevant evidence remains available while
  academic institutions, training purchases, conference activity, and minor service
  modifications can be excluded.
- Updated the package version from `0.1.0a1` to `0.1.0a2`.

## [0.1.0-alpha.1] - 2026-08-24

Initial application foundation.

### Added

- Typed asynchronous client for the public USAspending API.
- Separate NASA and Department of Defense prime-contract searches.
- Source-independent award and signal domain models.
- Deterministic, explainable space-relevance scoring.
- Scoring evidence from agency names, space terms, NAICS codes, and PSC codes.
- In-memory signal repository with deterministic deduplication.
- FastAPI health, USAspending ingestion, and signal-listing endpoints.
- Stable signal identifiers derived from source and award identifiers.
- Source award identifiers and USAspending evidence URLs.
- Command-line application entry point.
- Unit and API tests that do not require live-network access.

[Unreleased]: https://github.com/J-Varela/orbital-signal/compare/v0.1.0-alpha.2...HEAD
[0.1.0-alpha.2]: https://github.com/J-Varela/orbital-signal/releases/tag/v0.1.0-alpha.2
[0.1.0-alpha.1]: https://github.com/J-Varela/orbital-signal/tree/v0.1.0-alpha.2
