# Collection pacing, early stopping, and enrichment recovery

## Reviewed NASA results

The existing primary database was inspected with a connection forced into
read-only mode. Its migration version is `d84fc8a61b35`; no data, settings, or
schema were changed. The older database on port 55432 was not contacted.

The latest September 2026 checkpoints confirm:

| Dates | Outcome | Observation |
| --- | --- | --- |
| September 5–22 | Completed | Seven saved windows; 936 search-row observations, 40 signal insertions, 48 duplicate observations |
| September 23 | Failed | 100 search rows received; transaction enrichment exhausted four attempts with `RemoteProtocolError` |
| September 24–25 | Failed | Award search exhausted four attempts before receiving a page |
| September 26–28 | Failed | Award search exhausted four attempts before receiving a page |
| September 29–30 | Failed | Award search exhausted four attempts before receiving a page |

The successful windows retain their original identifiers, counts, and evidence.
These row/insert counters are observations, not a distinct-award census.
Recent windows started roughly 8–29 milliseconds after the preceding window
finished. That confirms the absence of a meaningful inter-window pause. It
does not prove that request pressure caused the remote disconnects: upstream
load, an intermediary, and connection behavior remain possible explanations.

## Controls

CLI overrides apply only when explicitly provided; omitted values retain the
existing environment/configuration. Environment names use `ORBITAL_SIGNAL_`:

| CLI flag | Setting suffix | Runtime default |
| --- | --- | --- |
| `--window-delay-seconds` | `COLLECTION_WINDOW_DELAY_SECONDS` | 5 seconds |
| `--request-interval-seconds` | `HTTP_REQUEST_INTERVAL_SECONDS` | 0.5 seconds |
| `--enrichment-concurrency` | `ENRICHMENT_CONCURRENCY` | 2; accepts 1–10 |
| `--max-consecutive-failures` | `COLLECTION_MAX_CONSECUTIVE_FAILURES` | 3 failed window attempts |

Delay values must be finite and nonnegative. Zero explicitly disables that
delay. The failure threshold must be a positive integer. The existing three-day
planning strategy, date/agency selection, subdivision, request retry limits,
TLS verification, and proxy behavior are preserved.

There is no sleep before the first actual window attempt. Subsequent attempts
pause after the prior attempt, including agency transitions and split children.
Skipped completed windows do not sleep, create attempts, or reset a failure
streak. Pages within one window do not incur a separate window delay. Instead,
a monotonic pacer spaces every physical HTTP request start, including retries
and transaction lookups, across source wrappers sharing one HTTPX client.
Existing exponential backoff and `Retry-After` waits still apply.

Only newly executed `failed` window attempts increment the consecutive-failure
counter. An executed completed, split, or pagination-incomplete window resets
it because the source responded successfully; this does not upgrade an
incomplete window's coverage. Old failures and skipped completions are neutral.
The triggering failed checkpoint and audit are saved before stopping. The stop
unwinds all subdivision, agency, and period loops, without creating or changing
later checkpoints. The existing collector lock releases normally.

The returned summary includes `stopped_early`, `stop_reason`,
`consecutive_failures`, and `attempted_windows`. A stopped collection returns
`status: incomplete`, and the CLI exits 2. Stop information describes that
invocation; a later read-only GET shows durable checkpoint progress rather than
reconstructing a past process's stop state. A new collection invocation starts
a fresh failure streak and resumes using existing saved coverage.

## Enrichment recovery

Transaction HTTP requests already had independent bounded retries. Previously,
exhausting them discarded the in-memory search page when the source client
closed, so another invocation had to repeat search and successful lookups.

The revised source saves valid mapped search pages awaiting enrichment in
`.orbital-signal-cache/enrichment` by default. This folder is ignored by Git.
Files are written atomically with mode 0600. Their keys hash the source origin,
agency, exact dates, page, and page size; credentials are not stored. Successful
lookup dates are checkpointed separately, including a valid source response
with no action date. Failed lookups are never converted into a successful null.

A new CLI process using the same cache directory can reload the pending search
page and retry only unresolved lookups. Successfully fetched search rows and
completed lookup results therefore survive an enrichment disconnect. The page
still raises a failure while any lookup is unresolved; the collector does not
ingest the partially enriched page or mark its window complete. Existing saved
signals are not overwritten with unenriched data.

Cache settings are `ENRICHMENT_CACHE_DIRECTORY`, `ENRICHMENT_CACHE_TTL_SECONDS`
(default 86400), and `ENRICHMENT_CACHE_MAX_PAGES` (default 8). Paths are relative
to the working directory unless configured absolutely. Expired, corrupt, or
evicted entries require a fresh search; no incomplete cache is treated as a
successful source response. Read/write failures remain visible failures.
`--refresh` invalidates matching pending pages before explicitly revisiting the
selected period. A pending entry is removed once its entire page is enriched.
Database failures after that point may require another search; this cache is
for enrichment recovery, not a full historical raw-data archive.

Search-row counters include retained-page replay observations and must not be
interpreted as new HTTP response rows. The execution summary reports
`cached_pages_reused`, and failures identify retained-cache replay where it
occurred. Cached pagination metadata is an observation from the saved search,
not a promise of current source completeness; the existing coverage caveats
still apply.

The old September 23 run did not save its 100 raw rows. Aggregate checkpoints
cannot reconstruct them. Its first retry must fetch search once; the new cache
protects newly fetched pending pages if enrichment then fails again.

## Suggested review plan

The following command is a dry run only, with no database or source access:

```bash
uv run --no-sync orbital-signal collect-monthly --agency nasa --start-date 2026-09-23 --end-date 2026-09-30 --window-days 3 --window-delay-seconds 30 --request-interval-seconds 1 --enrichment-concurrency 1 --max-consecutive-failures 2 --plan
```

It keeps the three-day strategy and explicitly selects conservative pacing.
The initial plan is September 23–25, 26–28, and 29–30. Saved finer boundaries
mean resume will first consider September 23 and 24–25 separately. Completed
September 5–22 is outside this selection and remains untouched.

The corresponding live retry would omit `--plan`; it has not been executed.
Do not add `--refresh` for ordinary resume: it deliberately revisits finished
coverage and invalidates pending enrichment recovery pages.

Validation uses mocked HTTP, fake pacing clocks, in-memory repositories, and
isolated SQLite. Tests cover failure streak resets/limits, recursion stop,
preserved completed data, fresh resume, API policy wiring, durable cache recovery
through new clients/interpreters, cache limits/corruption, and physical request
spacing. No new schema migration is needed for these changes.

The full regression suite passed 432 tests, with one existing TestClient
deprecation warning. Ruff lint, formatting, and JavaScript syntax checks passed.
Full Python type diagnostics match the pre-change workspace: nine existing
annotation errors and one existing lifecycle-annotation warning; no new type
diagnostics were introduced.
The updated dashboard stop notice passed JavaScript syntax checks. Its optional
browser rerun did not complete because the browser-launch approval remained
pending and was aborted; the new UI smoke assertions are not counted as passed.
