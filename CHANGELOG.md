# Changelog

All notable changes to Orbital Signal will be documented in this file.

The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Upgraded pytest from 8.4.2 to 9.1.1.
- Updated the reproducible `uv.lock` dependency lockfile.

### Known limitations

- Signals are stored in memory and disappear when the API restarts.
- Startup-candidate status is heuristic, not verified company-stage data.
- `occurred_on` currently represents the award start date and will be renamed for clarity.
- USAspending ingestion is bounded to five pages per configured agency.
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