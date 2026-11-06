# Monthly historical collection

Phase 1 collects current NASA and Department of Defense prime-contract award
snapshots, month by month, through the existing USAspending adapter and scoring
service. It writes qualifying awards to the same company and signal tables used
by the dashboard. Classification remains space-focused; broader technology
sources and classification rules are separate future work.

## Review and plan without collecting

The initial historical period is January through October 2026. Planning does
not read settings, connect to a database, or contact USAspending:

```bash
uv run orbital-signal collect-monthly --start-month 2026-01 --end-month 2026-10 --plan
```

This prints 212 initial windows: 106 small windows for each of the two agencies.
`--window-days` defaults to 3; use 1 for daily ingestion. Windows never cross a
month boundary and use the shorter remainder at month end. `--agency nasa`,
`--agency dod`, or `--agency all` selects the source agency independently.
All periods use inclusive dates, including leap days. Only completed calendar
months can be selected. There is no automatic collection on application startup.

Alternatively, use a complete `--start-date YYYY-MM-DD --end-date YYYY-MM-DD`
pair for exact historical dates, including a single day or completed days in
the current month. Both dates must be before today. Date and month arguments
are mutually exclusive; missing halves, reversed dates, and nonpositive window
sizes are rejected before any settings or I/O. Plans show initial source windows;
resume can skip or further partition them using saved checkpoint coverage.

## Checkpoint schema

Migration `c961fd3e5a22` adds only `collection_windows`. It does not alter or
remove companies, awards, signals, aliases, or existing ingestion runs. Durable
agency/window identities are necessary to distinguish finished work from failed,
interrupted, or truncated work after a process restart.

Additive migration `d84fc8a61b35` adds `collection_attempts` and copies only each
existing checkpoint's known last-attempt snapshot, labelled `legacy_snapshot`.
It never rewrites the original checkpoints or evidence. Earlier overwritten
attempt counters cannot be reconstructed and are not invented. The existing
`c961fd3e5a22` migration is unchanged. There is no automatic schema creation.

Review the migration and test results before applying it to your database.
The commands below require the reviewed migration to have been applied; they
are operational examples, not part of the implementation's verification run.
The existing dashboard, API feeds, and legacy ingestion routes continue to use
their existing tables independently of the new checkpoint table.

## Collect, inspect, and resume

After review and schema setup, an explicit command starts collection:

```bash
uv run orbital-signal collect-monthly --start-month 2026-01 --end-month 2026-10
```

Repeat the same command to resume. Finished date coverage is skipped. Failed,
interrupted (`running`), and incomplete windows retry from page 1, because a
saved upstream page offset can become stale as source records change. Existing
award identities and upserts prevent repeated observations from creating
duplicate records and preserve first detection times.

Read progress without contacting the source:

```bash
uv run orbital-signal collection-status --start-month 2026-01 --end-month 2026-10
```

To revisit finished periods for delayed publication or revisions, explicitly
pass `--refresh`. Existing finer checkpoint boundaries are retained. Completed commands exit 0;
failed or truncated work returns a JSON summary and exits 2; storage failures
exit 1. A status query also exits 2 while planned work remains unfinished.

In the dashboard, open **Collection**, select **From month** and **Through
month**, set the window size and agency, then choose **Load saved progress** or **Collect or resume**. The
optional revisit checkbox corresponds to `--refresh`. Monthly progress survives
reloads; the separate legacy collection session list still lasts only until
reload. Company and signal feeds refresh after monthly collection returns.

The corresponding API is:

| Method | Route | Behavior |
| --- | --- | --- |
| GET | `/api/v1/collections/monthly?start_month=2026-01&end_month=2026-10` | Read checkpoints without a source fetch |
| POST | `/api/v1/collections/monthly?start_month=2026-01&end_month=2026-10` | Collect or resume selected months |
| POST | Same route with `&refresh=true` | Revisit finished windows |

GET and POST also accept `window_days=3` and `agency=nasa|dod|all`. A complete
`start_date=2026-09-01&end_date=2026-09-01` pair can replace the month pair.

POST returns HTTP 200 with `status: incomplete` when individual windows fail or
remain truncated. Inspect window statuses and errors; HTTP success alone does
not mean all work finished. Invalid periods return 422, and overlapping monthly
collectors targeting the same PostgreSQL database/source return 409. The request runs in process; there
is no background queue. A stopped request or process may leave `running` work
for a later explicit resume.

## Pagination, counters, and limits

Each initial window covers up to three days by default and searches one agency,
with up to five pages of 100 rows. When the
last page reports more results, a multi-day window is split at its midpoint into
two non-overlapping, inclusive subwindows. Subdivision continues until the
source reports no next page or the window reaches a single day. A capped day
stays `incomplete`; it is never described as fully covered. Successful earlier
pages remain persisted if a later page fails, and other agency/month work can
continue.

Checkpoints record latest-attempt status, attempts, received-page
counts, fetched rows, relevant results, signal insertions, duplicate observations,
continuation metadata, timestamps, and failure messages. Counters are saved
after each page and after ordinary processing failures. Abrupt termination can
leave the current page's processing counters behind its persisted evidence;
resuming re-observes that page safely. Individual ingestion attempts continue
using the existing ingestion-run mechanism. If search succeeds but enrichment
fails, received page/row counts and the failing operation are retained; those
rows may not yet have been classified or stored.
If a relevant award's write fails, it is counted as relevant without being
labelled a stored signal or duplicate. An uncertain write outcome marks the
attempt's cumulative history incomplete rather than inventing a committed count.

Split-parent rows retain their original probe counts. Do not sum parents and
children as unique awards. The same award can also occur in multiple pages,
windows, or retries. Latest counters describe one attempt. The separate attempt
audit retains prior attempts and is saved atomically with each checkpoint.
Cumulative counters sum retained attempt observations once, including split
probes and repeats. They are not distinct awards or monthly obligations.
`stored_count` counts signal insertions, not independently verified unique awards.
Legacy snapshots and interrupted attempts make cumulative history incomplete;
`cumulative.history_complete=false` labels totals as lower bounds. Metrics for
intersecting old wide windows can include observations outside an exact-day
selection. No fabricated unique-award total is returned.

Resume partitions planned windows at existing checkpoint boundaries. The most
precise saved checkpoint controls each date segment, with newer attempts
breaking ties. Completed old months/half-months are reused; a failed fine window
is retried despite an older broad completion. Completed descendants can supersede
a failed broad probe. Old rows remain available for inspection rather than being
deleted or relabelled. Every selected day must have effective completed coverage
before the summary can report `completed`.

`completed` means no further pagination was reported across the planned window
tree. It does not guarantee all public source data was available or that paging
was a consistent snapshot. The source can change during a run, return imperfect
continuation metadata, or publish records later. Date subdivision reduces page
pressure but cannot establish independent completeness.

The existing adapter uses `date_type: action_date` for award searches and then
enriches each award's latest transaction date. Historical searches reflect
current award snapshots, rather than an immutable archive of every transaction
that occurred during a month. Later revisions can move an award's latest action
date. Award amounts are snapshots, not monthly obligations or company revenue.
See the official [award-search contract](https://github.com/fedspendingtransparency/usaspending-api/blob/master/usaspending_api/api_contracts/contracts/v2/search/spending_by_award.md)
and [time-period filter contract](https://github.com/fedspendingtransparency/usaspending-api/blob/master/usaspending_api/api_contracts/search_filters.md).

PostgreSQL collectors take one nonblocking transaction advisory lock per
database/source, held on a dedicated connection for the whole run. CLI and API
processes therefore cannot collect overlapping windows concurrently. Local
SQLite tests use a file lock keyed by canonical database path. Locks release on
normal completion, failure, cancellation, or process exit. GET progress is
read-only and does not take the collector lock. Avoid overlapping legacy
ingestion or reclassification with collection: those older operations do not
modify collection windows or participate in the new lock.

New ingestion timestamps use the application UTC clock consistently, and
completion is clamped at or after start if clocks move backwards. Checkpoint and
attempt timestamps are UTC-normalized and terminal end times cannot precede
their starts. Old inconsistent timestamps are normalized when read, without a
bulk rewrite of existing PostgreSQL records.

See [collection reliability and controlled validation](collection-reliability.md)
for retry settings, confirmed findings, remaining hypotheses, and exact one-day
planning commands.
See [collection pacing and enrichment recovery](collection-pacing.md) for the
window/request delays, one-worker option, safe failure limit, and pending-page
cache used when resuming after repeated disconnects.

## Validation and next phase

Tests use mocked source responses and in-memory repositories or SQLite. They
cover month boundaries, pagination/subdivision, truncated days, duplicates,
failure counts, interrupted state, durable checkpoints, resume, API feeds,
and CLI planning. The new migration is exercised against SQLite while existing
company data is retained. No test performs a live backfill or applies migrations
to the development PostgreSQL database.

After review, apply the migration and validate a narrow historical period before
attempting the full initial range. PostgreSQL and real upstream behavior remain
to be validated deliberately.

Phase 2 is deferred until historical collection has been reviewed and validated.
It will add a locally runnable job for the previous completed month plus recent
periods revisited with refresh semantics. No recurring scheduler, deployment,
DARPA/DOE/ARPA-E/SBIR/STTR adapter, or additional source is implemented here.
