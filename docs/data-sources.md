# Data Sources

## Purpose

Orbital Signal uses public records as evidence of activity in the space economy.

Each source adapter is responsible for:

1. Fetching records from an external system.
2. Validating source-specific inputs.
3. Mapping external fields into source-independent domain models.
4. Preserving identifiers and evidence URLs.
5. Returning normalized records to the ingestion service.

Source adapters do not decide whether an event is space-relevant or whether a
recipient is a startup candidate. Those decisions belong to the relevance and
quality engines.

## Active sources

| Source | Status | Evidence type |
| --- | --- | --- |
| USAspending | Active in Alpha 2 | Federal prime contract awards |
| SEC Form D | Planned | Private securities offerings |
| NASA procurement forecasts | Planned investigation | Anticipated contracting opportunities |
| SBIR/STTR awards | Planned | Innovation awards and company-stage signals |

Only USAspending is implemented in the current release.

## USAspending

### Overview

[USAspending.gov](https://www.usaspending.gov/) publishes federal spending data
under the requirements of the DATA Act.

Orbital Signal uses its public API to search for prime federal contract awards
connected to NASA and the Department of Defense.

API base URL:

```text
https://api.usaspending.gov
```

Search endpoint:

```text
POST /api/v2/search/spending_by_award/
```

The current API integration does not require an API key.

### Configuration

The source can be configured through environment variables:

```dotenv
ORBITAL_SIGNAL_USASPENDING_BASE_URL=https://api.usaspending.gov
ORBITAL_SIGNAL_HTTP_TIMEOUT_SECONDS=30
```

The default base URL points to the public USAspending service. The configurable
URL also allows tests or development environments to use a mock service.

### Default agency searches

Orbital Signal searches these awarding agencies independently:

1. National Aeronautics and Space Administration
2. Department of Defense

Independent searches prevent the much larger defense contract dataset from
crowding NASA awards out of the bounded Alpha ingestion window.

The source client can accept a different agency tuple when called directly, but
the public ingestion endpoint currently uses the defaults.

### Award types

The adapter requests prime contract award types represented by these
USAspending codes:

```text
A
B
C
D
```

Other assistance and transaction types are not included in the current
implementation.

### Request shape

A simplified request resembles:

```json
{
  "filters": {
    "time_period": [
      {
        "start_date": "2026-01-01",
        "end_date": "2026-08-25"
      }
    ],
    "award_type_codes": ["A", "B", "C", "D"],
    "agencies": [
      {
        "type": "awarding",
        "tier": "toptier",
        "name": "National Aeronautics and Space Administration"
      }
    ]
  },
  "fields": [
    "Award ID",
    "Recipient Name",
    "Recipient UEI",
    "Award Amount",
    "Start Date",
    "End Date",
    "Awarding Agency",
    "Description",
    "generated_internal_id"
  ],
  "sort": "Start Date",
  "order": "desc",
  "page": 1,
  "limit": 100
}
```

The date range is supplied by the caller. Orbital Signal preserves the award
dates returned by USAspending rather than treating them as detection timestamps.

### Pagination boundary

Current defaults are:

| Setting | Value |
| --- | ---: |
| Records per page | 100 |
| Maximum pages per agency | 5 |
| Default agency searches | 2 |
| Maximum fetched records | 1,000 |

Pagination stops early when USAspending returns:

```json
{
  "page_metadata": {
    "hasNext": false
  }
}
```

The 1,000-record boundary is an Alpha safety limit, not a claim of complete
coverage for large date ranges.

A durable ingestion system should eventually support resumable pagination,
incremental checkpoints, and explicit reporting when a search reaches its
configured limit.

### Field normalization

USAspending results are mapped into `AwardRecord` fields as follows:

| USAspending field | AwardRecord field |
| --- | --- |
| `Award ID` | `source_award_id` |
| `generated_internal_id` | `generated_internal_id` |
| `Recipient Name` | `recipient_name` |
| `Recipient UEI` | `recipient_uei` |
| `Award Amount` | `amount` |
| `Awarding Agency` | `awarding_agency` |
| `Description` | `description` |
| `Start Date` | `start_date` |
| `End Date` | `end_date` |
| Constant `usaspending` | `source` |
| Generated USAspending URL | `source_url` |

If `Award ID` is missing, the adapter attempts to use
`generated_internal_id`. A record without either identifier is rejected because
it cannot be deduplicated or attributed safely.

Blank descriptions are retained as empty strings. Missing UEI and date values
remain nullable.

### Classification-code limitation

The domain model and relevance engine support:

- NAICS codes
- Product and Service Classification codes

The current USAspending request does not yet fetch or populate these fields.
Therefore, live Alpha 2 USAspending records are presently scored using agency
and text evidence only.

Adding classification codes to the adapter is a planned calibration improvement.
It should be covered by mapping tests before its scoring impact is enabled in
production.

### Evidence URLs

Each normalized award includes an evidence URL constructed from the
USAspending-generated internal identifier when available:

```text
https://www.usaspending.gov/award/{generated_internal_id}
```

If the generated identifier is unavailable, the source award identifier is used
as the fallback.

Evidence links allow users to inspect the underlying public record rather than
relying on Orbital Signal's classification alone.

### Source deduplication

The adapter stores normalized awards in a temporary dictionary keyed by
`source_award_id`.

If the same award appears on multiple pages or searches, the final source result
contains one normalized award for that identifier.

After relevance scoring, the application creates a signal identifier from:

```text
usaspending:{source_award_id}
```

This provides a second idempotency boundary inside the signal repository.

### Live calibration snapshot

A live Alpha 2 ingestion covering `2026-01-01` through `2026-08-25` produced:

| Metric | Result |
| --- | ---: |
| Fetched awards | 1,000 |
| Space-relevant awards | 20 |
| Stored signals | 20 |
| Duplicate signals | 0 |
| Startup candidates returned | 7 |

The startup-candidate results included commercial recipients associated with
lunar systems, rocket-engine cooling, telemetry, in-space assembly, and
satellite operations.

This snapshot is calibration evidence, not a permanent expected result.
USAspending records can change, and later ingestions of the same date range may
produce different totals.

### Date semantics

The API request accepts a caller-supplied start and end date, while each returned
signal currently exposes the source award start date as `occurred_on`.

These values represent different concepts:

- Ingestion range: the search window submitted to USAspending
- Award start date: a source-provided contract field
- Detection time: when Orbital Signal processed the record

A returned award start date may not match the date on which Orbital Signal first
observed the award. The domain model should rename `occurred_on` to
`award_start_date` in a future release.

### Failure behavior

The source client calls `raise_for_status()` for non-successful USAspending
responses.

At the API boundary, upstream HTTP failures become:

```text
502 Bad Gateway
```

with this public detail:

```text
USAspending request failed
```

Invalid date ranges are rejected before ingestion. The end date must be on or
after the start date.

Current retry behavior is intentionally minimal. The client does not yet
implement:

- Automatic retries
- Exponential backoff
- Rate-limit coordination
- Durable partial-run recovery
- Circuit breaking

These capabilities should accompany persistent ingestion runs.

### Testing

USAspending tests use `httpx.MockTransport`.

The test suite verifies:

- Request pagination
- Award mapping
- Source evidence URLs
- Invalid date-range handling
- Service-level ingestion counts
- API translation of upstream failures

Normal tests do not contact the live USAspending service. This keeps tests fast,
deterministic, and usable without network access.

## Planned sources

### SEC Form D

SEC Form D filings can provide evidence that a company is offering private
securities.

Potential value:

- Financing event detection
- Offering amount
- Filing date
- Issuer identity
- Company address and industry information

Important limitation:

A Form D filing is evidence of an offering, not proof that the full amount was
raised. Orbital Signal must preserve that distinction in its language and data
model.

Before implementation, the project needs:

- A reliable SEC data-access method
- Issuer normalization
- Company-to-UEI identity matching
- Amendment handling
- Filing-level deduplication
- SEC-compliant request headers and rate behavior

### NASA procurement forecasts

NASA procurement forecasts may reveal future contracting demand before an award
appears in USAspending.

Potential value:

- Early opportunity detection
- NASA center and program attribution
- Estimated solicitation timing
- Industry and capability demand signals

This source remains an investigation item because publication formats and access
patterns can vary. No automated adapter should be built until a stable,
attributable source path is confirmed.

### SBIR and STTR awards

SBIR and STTR records can identify small companies receiving non-dilutive
research and development funding.

Potential value:

- Stronger small-business evidence
- Phase I, II, and III progression
- Technology-topic classification
- Agency interest over time
- Company maturity signals

The source will require canonical company matching because recipient names may
differ across USAspending, SBIR records, company websites, and SEC filings.

## Adding a source

A new adapter should implement the service's `AwardSource` protocol when it
produces award-like records:

```python
class AwardSource(Protocol):
    async def search_awards(
        self,
        *,
        start_date: date,
        end_date: date,
    ) -> list[AwardRecord]: ...
```

Sources that represent different event types, such as financing filings, should
receive their own normalized source model and ingestion service rather than
being forced into `AwardRecord`.

Every new source should include:

1. Typed normalization models.
2. Stable source identifiers.
3. Direct evidence URLs.
4. Deterministic deduplication.
5. Mocked source tests.
6. Documented pagination and coverage boundaries.
7. Clear failure behavior.
8. Attribution and interpretation warnings.
9. A strategy for matching records to canonical companies.

## Data interpretation

Orbital Signal records evidence and applies deterministic classifications. It
does not establish that:

- A company is investable.
- A company is financially healthy.
- A recipient is a young startup.
- A contract guarantees future revenue.
- A securities offering was fully raised.
- A high score represents investment quality.

Scores express evidence relevance to the space sector. Users should inspect
source records before making business, employment, or investment decisions.

## Source governance

The following rules apply to every integration:

1. Prefer public, attributable, first-party sources.
2. Preserve the original source identifier.
3. Store a direct evidence URL when possible.
4. Record ingestion and detection times separately from event dates.
5. Avoid presenting inference as verified fact.
6. Make coverage limits visible.
7. Respect published access and rate requirements.
8. Keep source-specific parsing outside the core domain and API layers.