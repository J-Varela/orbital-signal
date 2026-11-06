# Collection reliability and controlled validation

For the later September results review, pacing, consecutive-failure stop, and
durable pending-enrichment recovery, see [collection pacing](collection-pacing.md).

This change prepares a small, reviewable NASA collection day before expanding
to September 2026 and larger historical ranges. No live collection or migration
application is part of its automated validation.

## Findings

The reported September disconnect is consistent with HTTPX `RemoteProtocolError`.
The previous adapter retried read/connect timeouts and selected HTTP statuses,
but allowed protocol disconnects and connection errors to fail immediately.
This is a confirmed recovery gap in the inspected implementation, not proof
of why the remote peer closed a particular connection.

The previous default started up to ten transaction-enrichment requests at once.
`asyncio.gather` propagated a failure without cancelling its siblings, allowing
requests from the failed page to overlap later work. The revised implementation
cancels and drains all sibling tasks before leaving a failed page. A fetched
count of zero in old checkpoints does not prove award search failed: a page was
previously counted only after every transaction lookup succeeded. New errors
record the operation/endpoint and received-row context where available.

The active settings were inspected without opening a database connection and
resolved to `127.0.0.1:5432/orbital_signal`. Neither configuration nor credentials
were changed. The older Docker database at port `55432` was not contacted.
All standard uppercase/lowercase HTTP proxy variables were absent in the agent
process. That observation does not establish the environment of the failed run.

Possible upstream load, intermediary disconnects, and stale reused connections
remain hypotheses. There are no packet traces or live reproductions proving one
of those causes. TLS verification remains enabled, and HTTPX retains environment
proxy support. No proxy bypass or TLS workaround is introduced.

Ingestion runs previously obtained their start timestamp from the database and
their end timestamp from the application clock. That confirmed mixed-clock
design permits reversed timestamps if those clocks differ. Clock skew as the
specific cause of the reported rows is unconfirmed. New writes use application
UTC consistently and enforce nonnegative ordering; existing checkpoints are
preserved and normalized on read.

## HTTP policy

Both API and CLI use the same client/source factory. Settings use the
`ORBITAL_SIGNAL_` environment prefix:

| Setting | Default | Behavior |
| --- | --- | --- |
| `HTTP_TIMEOUT_SECONDS` | 30 | HTTPX operation timeout; pool wait capped at 5 seconds |
| `HTTP_RETRY_ATTEMPTS` | 4 | Total attempts, including the first; maximum 10 |
| `HTTP_RETRY_BASE_DELAY_SECONDS` | 1 | Exponential backoff base |
| `HTTP_RETRY_MAX_DELAY_SECONDS` | 30 | Maximum retry delay budget |
| `ENRICHMENT_CONCURRENCY` | 2 | Concurrent transaction lookups; configurable 1–10 |
| `HTTP_MAX_CONNECTIONS` | 4 | Connection-pool ceiling |
| `HTTP_KEEPALIVE_EXPIRY_SECONDS` | 5 | Idle pooled-connection expiry; 0 disables reuse |

Retryable failures include remote protocol disconnects, connection/read/write
errors, connect/read/write/pool timeouts, and HTTP 429/500/502/503/504. Other HTTP
statuses and invalid source payloads fail without repeated identical retries.
Backoff grows exponentially with bounded jitter. Valid `Retry-After` seconds or
HTTP dates provide a minimum wait. If that wait exceeds the configured budget,
the operation stops rather than retrying earlier than requested. Original error
types, total attempts, and the reason for stopping are retained in checkpoint
failure information. Received pages and already committed evidence survive a
later failure. Retries replay search/read operations only.

## Checkpoints and metrics

The existing checkpoint identities, status vocabulary, and upsert behavior are
preserved. Failed/incomplete work restarts at page one. Finer date segmentation
reuses effective completed coverage from old monthly and half-month rows and
keeps old diagnostics. A capped single day remains incomplete.

New migration `d84fc8a61b35` adds only an attempt-audit table and seeds known
last-attempt snapshots. It does not reset or rewrite evidence or checkpoints.
Review it before applying it to the primary database; it has not been applied
by this implementation. `c961fd3e5a22` remains unchanged. Newly recorded attempts
have persistent individual and cumulative counters. Unknown old attempts and
in-flight writes lost to abrupt interruption are explicitly flagged as
incomplete history; overlapping observations are never advertised as unique
awards. See the [collection guide](monthly-collection.md) for exact semantics.

## Dry-run demonstrations

These commands perform no settings, database, or source access:

```bash
# NASA: exactly September 1 (one search window)
uv run --no-sync orbital-signal collect-monthly --agency nasa --start-date 2026-09-01 --end-date 2026-09-01 --window-days 1 --plan

# NASA: September, ten consecutive three-day windows
uv run --no-sync orbital-signal collect-monthly --agency nasa --start-month 2026-09 --end-month 2026-09 --window-days 3 --plan

# NASA and DoD: September, ten windows per agency, twenty searches
uv run --no-sync orbital-signal collect-monthly --agency all --start-month 2026-09 --end-month 2026-09 --window-days 3 --plan
```

The September windows are 1–3, 4–6, 7–9, 10–12, 13–15, 16–18, 19–21, 22–24,
25–27, and 28–30. These are initial plans; resume can skip covered dates and
pagination can subdivide a busy window further.

After reviewing tests and applying the reviewed audit migration, the first
live collection command should be the following, run from the repository root:

```bash
uv run --no-sync orbital-signal collect-monthly --agency nasa --start-date 2026-09-01 --end-date 2026-09-01 --window-days 1 --refresh
```

This command is provided for review and has not been executed. `--refresh`
ensures the selected day is actually queried even if an older wide checkpoint
already covers it. It does not widen the dates or add DoD. It uses the existing
database configuration; do not switch to the older Docker instance.

Inspect the exit code, terminal window status, continuation flag, per-attempt
counts, errors, and existing company/signal feeds before expanding the period.
Exit 2 or a single-day `incomplete` result is not a verified day. Even a
`completed` result means only no reported pagination remaining; source reporting
delays and award-snapshot semantics still prevent a claim of universal coverage.

The shared PostgreSQL lock has deterministic transaction-lifetime tests and
local cross-process equivalents, but no live PostgreSQL validation was performed.
It depends on the dedicated owner connection remaining alive. A database restart,
connection loss, or idle-transaction timeout can release that advisory lock;
heartbeat/fencing protection is not implemented. Stop and inspect any database
connection failure before starting another collector. Legacy collector processes
that predate the shared lock do not participate in it.
