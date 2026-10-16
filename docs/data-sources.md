# Data Sources

This document describes the adapter in current development beyond tagged Alpha
2, whose runtime version remains `0.1.0-alpha.2`.

## Source status

| Source | Status | Intended evidence |
| --- | --- | --- |
| USAspending | Implemented | Federal prime contract awards |
| SBIR/STTR | Proposed | Small-business research awards and phase history |
| SEC Form D | Proposed | Private securities offering filings |
| NASA procurement forecasts | Investigation | Anticipated contracting opportunities |

Only USAspending has an implemented adapter. Source adapters fetch and normalize
evidence; relevance, candidate quality, and priority decisions belong to the
application's deterministic scoring rules.

## USAspending requests

The adapter uses `https://api.usaspending.gov` with:

- `POST /api/v2/search/spending_by_award/` for award searches.
- `POST /api/v2/transactions/` for latest transaction action-date enrichment.

The base URL and request timeout can be overridden with
`ORBITAL_SIGNAL_USASPENDING_BASE_URL` and
`ORBITAL_SIGNAL_HTTP_TIMEOUT_SECONDS`. The application sends a versioned
`User-Agent`; no API key is currently required.

Searches run independently for NASA and Department of Defense, using awarding
toptier agency filters and prime contract award codes `A`, `B`, `C`, and `D`.
Other assistance types are outside this adapter's coverage.

The supplied date range uses `date_type: action_date`. Results are sorted by
`Start Date` descending. A search over recent actions can therefore include an
award whose original start date is much earlier.

| Bound | Current default |
| --- | ---: |
| Records per page | 100 |
| Pages per agency | 5 |
| Agency searches | 2 |
| Maximum fetched search rows before deduplication | 1,000 |
| Concurrent action-date lookups per page | 10 |

Pagination stops earlier when `page_metadata.hasNext` is false. Reaching five
pages is a coverage limit, not evidence that all awards in the range were
retrieved. The API does not yet report truncation explicitly or persist a
pagination checkpoint. Narrowing the date range can reduce truncation but does
not turn bounded searches into a complete dataset.

Transaction enrichment requests the latest transaction, sorted by
`action_date`, for each award with a generated internal identifier. It can add
up to one upstream request per search row before retries. Awards without that
identifier retain no enriched action date.

## Normalized fields and attribution

| Source field | Normalized field |
| --- | --- |
| `generated_internal_id`, falling back to display `Award ID` | `source_award_id` |
| `generated_internal_id` | `generated_internal_id` |
| `Recipient Name`, `Recipient UEI` | `recipient_name`, `recipient_uei` |
| `Award Amount` | `amount` |
| `Awarding Agency`, `Description` | `awarding_agency`, `description` |
| `Start Date`, `End Date` | `start_date`, `end_date` |
| Latest transaction `action_date` | `action_date` |
| Constant `usaspending` | `source` |
| Constructed USAspending award URL | `source_url` |

The generated internal identifier is preferred because display award IDs are not
globally unique. An award without either identifier is invalid. Empty
descriptions are retained; missing optional UEIs and dates remain nullable.
Evidence links use the generated identifier when available, with the display
award identifier as a fallback:

```text
https://www.usaspending.gov/award/{identifier}
```

The domain model and relevance engine support NAICS and PSC codes, but the
adapter does not currently request or populate them. Live classification thus
uses agency and text evidence. Adding code fields requires mapping tests and
review of their impact on scoring.

Within a fetch, repeated canonical source award identifiers collapse to one
normalized award. Persistent awards are unique by source and source award
identifier; signal identifiers are derived deterministically from the same
pair. Existing legacy awards can be matched by generated internal identifier
on re-ingestion, retaining signal IDs and detection history. Repeated ingestion
can update an award without creating a new signal or resetting its first
detection time.

## Date and amount interpretation

These dates describe different things:

- Requested ingestion range: the action-date filter sent to award search.
- Latest transaction action date: the enriched date used for signal recency.
- Award start/end dates: source-provided contract dates.
- `detected_at`: when Orbital Signal first retained the evidence/signal.

`occurred_on` uses the enriched action date when available and otherwise the
award start date. Priority recency points require an action date. Because the
enrichment requests the latest transaction without restricting it to the
ingestion window, its date can fall outside that window. The current data model
is an award snapshot, not a history of individual transaction events.

Award amounts and company-profile totals describe retained source award
snapshots. They are not verified revenue, net obligations during the requested
window, cash received, or independently confirmed company growth.

## Failure and run behavior

Requests retry selected transient failures up to three attempts, with one- and
two-second backoff delays: read/connect timeouts and HTTP `429`, `500`, `502`,
`503`, and `504`. Other upstream HTTP failures propagate without retry. There
is no coordinated rate limiter, `Retry-After` scheduling, circuit breaker, or
durable partial-fetch recovery.

At the HTTP boundary, upstream HTTP failures and malformed source payloads
return `502 Bad Gateway`. Invalid caller date ranges return `422`. Ingestion
runs store status and diagnostics in PostgreSQL. A process interruption can
leave a run marked running; automatic recovery is not implemented.

Fetching finishes before awards are scored and persisted. Relevant awards are
then committed individually, so a persistence failure can leave earlier
records stored. Initially irrelevant awards are not retained. Reclassification
can reconsider evidence retained by earlier successful classifications,
including awards whose derived signals were subsequently removed.

## Calibration and testing

A historical Alpha 2 live run over `2026-01-01` through `2026-08-25` fetched
1,000 awards, retained 20 relevant signals, and returned seven candidate
results. This is a dated calibration snapshot from the earlier implementation,
not a current expected total or proof of complete coverage. Source records and
classification rules can change.

Normal tests use `httpx.MockTransport` to check request construction,
pagination, normalization, action-date enrichment, retries, and malformed
payload handling. Live network access is not required by the test suite.

## Proposed sources and their meaning

SBIR/STTR records could add phase and topic evidence plus stronger indications
of small-business activity. They would still need source-specific normalization,
company matching, coverage reporting, and care when interpreting maturity or
commercial success.

SEC Form D could add offering filings, amounts, amendments, and issuer details.
An offering filing is not proof that the full offering amount was raised.
Implementation needs appropriate request behavior, filing-level deduplication,
amendment handling, and cautious issuer matching. Financing filings should use
a dedicated event model rather than being forced into `AwardRecord`.

NASA forecasts could show anticipated opportunities but require a stable,
attributable source path before automation. Forecasts must remain distinct from
awarded contracts.

## Source principles

Preserve source identifiers, evidence links, and date semantics; keep inference
distinct from source facts. Document pagination and interpretation limits and
respect source access requirements. Every new adapter needs deterministic
deduplication, mocked normalization/failure tests, a coverage strategy, and a
clear way to match evidence to companies without silently merging unrelated
organizations.

Neither relevance nor priority establishes financial health, investability,
verified startup status, guaranteed revenue, or completed fundraising. Users
should inspect source evidence when interpreting a signal.
