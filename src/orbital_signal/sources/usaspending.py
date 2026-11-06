"""Typed adapter for the public USAspending award search API."""

import asyncio
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import quote
from weakref import WeakKeyDictionary

import httpx

from orbital_signal.domain import AwardRecord
from orbital_signal.source_cache import PendingEnrichmentCache, PendingSearchPage, SourceCacheError

SEARCH_PATH = "/api/v2/search/spending_by_award/"
TRANSACTIONS_PATH = "/api/v2/transactions/"
CONTRACT_AWARD_TYPE_CODES = ["A", "B", "C", "D"]
DEFAULT_AGENCIES = (
    "National Aeronautics and Space Administration",
    "Department of Defense",
)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
RETRYABLE_TRANSPORT_ERRORS = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


def _retry_after_seconds(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse the server's minimum retry delay without truncating long waits."""

    if value is None:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return float(value)
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        return max(0.0, (target - (now or datetime.now(UTC))).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _request_failure_context(error: httpx.HTTPError, *, path: str, attempt: int) -> None:
    """Attach bounded request diagnostics without exposing a proxy or full URL."""

    operation = {
        SEARCH_PATH: "award_search",
        TRANSACTIONS_PATH: "transaction_enrichment",
    }.get(path, "source_request")
    error.__dict__.update(
        source_request_path=path if path in {SEARCH_PATH, TRANSACTIONS_PATH} else "unknown",
        source_operation=operation,
        retry_attempts=attempt,
    )


class SourceDataError(Exception):
    """USAspending returned a response that does not match the expected data format."""


@dataclass(frozen=True)
class AwardSearchPage:
    """One enriched award page and its upstream pagination observation."""

    awards: list[AwardRecord]
    fetched_count: int
    has_more: bool
    cache_hit: bool = False


@dataclass
class _RequestPacer:
    """Space request starts across all source wrappers sharing an HTTP client."""

    interval: float
    last_started: float | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def wait(self) -> None:
        if self.interval == 0:
            return
        async with self.lock:
            if self.last_started is not None:
                delay = self.interval - (monotonic() - self.last_started)
                if delay > 0:
                    await asyncio.sleep(delay)
            self.last_started = monotonic()


_REQUEST_PACERS: WeakKeyDictionary[httpx.AsyncClient, _RequestPacer] = WeakKeyDictionary()


class USAspendingClient:
    """Fetch paginated prime contract awards from USAspending."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        enrichment_concurrency: int = 2,
        retry_attempts: int = 4,
        retry_base_delay_seconds: float = 1.0,
        retry_max_delay_seconds: float = 30.0,
        request_interval_seconds: float = 0.0,
        enrichment_cache_directory: Path | None = None,
        enrichment_cache_ttl_seconds: float = 86400.0,
        enrichment_cache_max_pages: int = 8,
    ) -> None:
        if enrichment_concurrency < 1:
            raise ValueError("enrichment_concurrency must be at least 1")
        if not 1 <= retry_attempts <= 10:
            raise ValueError("retry_attempts must be between 1 and 10")
        if not math.isfinite(retry_base_delay_seconds) or retry_base_delay_seconds <= 0:
            raise ValueError("retry_base_delay_seconds must be finite and positive")
        if (
            not math.isfinite(retry_max_delay_seconds)
            or retry_max_delay_seconds < retry_base_delay_seconds
        ):
            raise ValueError("retry_max_delay_seconds must be finite and at least the base delay")
        if not math.isfinite(request_interval_seconds) or request_interval_seconds < 0:
            raise ValueError("request_interval_seconds must be finite and nonnegative")
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._enrichment_concurrency = enrichment_concurrency
        self._retry_attempts = retry_attempts
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._retry_max_delay_seconds = retry_max_delay_seconds
        pacer = _REQUEST_PACERS.get(client)
        if pacer is None:
            pacer = _RequestPacer(request_interval_seconds)
            _REQUEST_PACERS[client] = pacer
        else:
            pacer.interval = max(pacer.interval, request_interval_seconds)
        self._request_pacer = pacer
        self._enrichment_cache = (
            PendingEnrichmentCache(
                directory=enrichment_cache_directory,
                base_url=self._base_url,
                ttl_seconds=enrichment_cache_ttl_seconds,
                max_pages=enrichment_cache_max_pages,
            )
            if enrichment_cache_directory is not None
            else None
        )

    def clear_cached_pages(
        self, *, start_date: date, end_date: date, agencies: tuple[str, ...] = DEFAULT_AGENCIES
    ) -> None:
        """Discard pending snapshots for an explicit refresh of overlapping windows."""

        if self._enrichment_cache is not None:
            try:
                self._enrichment_cache.invalidate(
                    start_date=start_date, end_date=end_date, agencies=agencies
                )
            except SourceCacheError as exc:
                raise SourceDataError(
                    f"{exc}. Check the pending enrichment cache directory permissions."
                ) from exc

    async def search_awards(
        self,
        *,
        start_date: date,
        end_date: date,
        agencies: tuple[str, ...] = DEFAULT_AGENCIES,
        page_limit: int = 5,
        records_per_page: int = 100,
    ) -> list[AwardRecord]:
        if end_date < start_date:
            raise ValueError("end_date must be on or after start_date")
        if not 1 <= records_per_page <= 100:
            raise ValueError("records_per_page must be between 1 and 100")
        if page_limit < 1:
            raise ValueError("page_limit must be at least 1")

        awards_by_id: dict[str, AwardRecord] = {}
        # Search each agency independently so the much larger defense result set
        # cannot crowd NASA awards out of the bounded alpha ingestion window.
        for agency in agencies:
            for page in range(1, page_limit + 1):
                result = await self.search_awards_page(
                    start_date=start_date,
                    end_date=end_date,
                    agency=agency,
                    page=page,
                    records_per_page=records_per_page,
                )
                for award in result.awards:
                    awards_by_id[award.source_award_id] = award

                if not result.has_more:
                    break

        return list(awards_by_id.values())

    async def search_awards_page(
        self,
        *,
        start_date: date,
        end_date: date,
        agency: str,
        page: int = 1,
        records_per_page: int = 100,
    ) -> AwardSearchPage:
        """Fetch one agency page without hiding truncation or earlier page progress.

        ``has_more`` reflects the source's flag, not a guarantee of complete
        historical coverage. ``fetched_count`` counts rows before deduplication.
        """

        if end_date < start_date:
            raise ValueError("end_date must be on or after start_date")
        if not agency.strip():
            raise ValueError("agency must not be empty")
        if page < 1:
            raise ValueError("page must be at least 1")
        if not 1 <= records_per_page <= 100:
            raise ValueError("records_per_page must be between 1 and 100")

        cache = self._enrichment_cache
        cached: PendingSearchPage | None = None
        cache_hit = False
        row_count: int | None = None
        operation = "pending_enrichment_cache"
        try:
            if cache is not None:
                cached = cache.get(
                    agency=agency,
                    start_date=start_date,
                    end_date=end_date,
                    page=page,
                    records_per_page=records_per_page,
                )
            cache_hit = cached is not None
            if cached is not None:
                page_awards, has_more = cached.awards, cached.has_more
                row_count = len(page_awards)
            else:
                operation = "award_search"
                response = await self._post_with_retry(
                    SEARCH_PATH,
                    json=self._build_payload(
                        start_date=start_date,
                        end_date=end_date,
                        agencies=(agency,),
                        page=page,
                        limit=records_per_page,
                    ),
                )
                response.raise_for_status()
                payload = self._parse_response(response)
                row_count = len(payload["results"])
                operation = "page_metadata"
                page_metadata = payload.get("page_metadata")
                if not isinstance(page_metadata, dict) or not isinstance(
                    page_metadata.get("hasNext"), bool
                ):
                    raise SourceDataError("USAspending search response has invalid page metadata")
                has_more = page_metadata["hasNext"]
                operation = "award_mapping"
                page_awards = [self._map_award(item) for item in payload["results"]]
                if cache is not None:
                    if len(page_awards) > 100:
                        raise SourceDataError("USAspending returned an oversized search page")
                    operation = "pending_enrichment_cache"
                    cached = cache.new_page(
                        agency=agency,
                        start_date=start_date,
                        end_date=end_date,
                        page=page,
                        records_per_page=records_per_page,
                        awards=page_awards,
                        has_more=has_more,
                    )
                    cache.save(cached)

            def checkpoint_lookup() -> None:
                if cache is not None and cached is not None:
                    cache.save(cached)

            operation = "transaction_enrichment"
            enriched_awards = await self._enrich_action_dates(
                page_awards,
                completed_dates=cached.action_dates if cached is not None else None,
                on_success=checkpoint_lookup if cached is not None else None,
            )
            operation = "pending_enrichment_cache"
            if cache is not None and cached is not None:
                cache.discard(cached)
        except Exception as exc:
            error = (
                SourceDataError(f"{exc}. Check the pending enrichment cache directory permissions.")
                if isinstance(exc, SourceCacheError)
                else exc
            )
            if isinstance(exc, SourceCacheError):
                operation = "pending_enrichment_cache"
            error.__dict__["source_operation"] = operation
            if row_count is not None:
                error.__dict__.update(
                    source_page_rows=row_count,
                    source_page_received=True,
                    source_page_cache_hit=cache_hit,
                )
            error.__dict__.setdefault(
                "source_request_path",
                TRANSACTIONS_PATH if operation == "transaction_enrichment" else SEARCH_PATH,
            )
            if error is not exc:
                raise error from exc
            raise
        return AwardSearchPage(
            awards=enriched_awards,
            fetched_count=len(page_awards),
            has_more=has_more,
            cache_hit=cache_hit,
        )

    @staticmethod
    def _build_payload(
        *,
        start_date: date,
        end_date: date,
        agencies: tuple[str, ...],
        page: int,
        limit: int,
    ) -> dict[str, Any]:
        return {
            "filters": {
                "time_period": [
                    {
                        "start_date": start_date.isoformat(),
                        "end_date": end_date.isoformat(),
                        "date_type": "action_date",
                    }
                ],
                "award_type_codes": CONTRACT_AWARD_TYPE_CODES,
                "agencies": [
                    {"type": "awarding", "tier": "toptier", "name": agency} for agency in agencies
                ],
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
                "generated_internal_id",
            ],
            "sort": "Start Date",
            "order": "desc",
            "page": page,
            "limit": limit,
        }

    @staticmethod
    def _map_award(item: dict[str, Any]) -> AwardRecord:
        if not isinstance(item, dict):
            raise SourceDataError("USAspending award result must be a JSON object")

        try:
            generated_id = item.get("generated_internal_id")
            if isinstance(generated_id, str):
                generated_id = generated_id.strip() or None

            # Display award identifiers can repeat across agencies and parent contracts.
            # The generated identifier includes those components and is globally stable.
            award_id = str(generated_id or item.get("Award ID") or "").strip()
            if not award_id:
                raise ValueError("USAspending result is missing an award identifier")

            evidence_url = f"https://www.usaspending.gov/award/{quote(award_id, safe='_-')}"

            return AwardRecord(
                source="usaspending",
                source_award_id=award_id,
                generated_internal_id=generated_id,
                recipient_name=item.get("Recipient Name") or "",
                recipient_uei=item.get("Recipient UEI") or None,
                amount=float(item.get("Award Amount") or 0),
                awarding_agency=item.get("Awarding Agency") or "",
                description=item.get("Description") or "",
                start_date=item.get("Start Date"),
                end_date=item.get("End Date"),
                source_url=evidence_url,
            )
        except (TypeError, ValueError) as exc:
            raise SourceDataError("USAspending returned an invalid award record") from exc

    @staticmethod
    def _parse_response(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceDataError("USAspending returned invalid JSON") from exc

        if not isinstance(payload, dict):
            raise SourceDataError("USAspending response must be a JSON object")

        results = payload.get("results")
        if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
            raise SourceDataError("USAspending response results must be a list of JSON objects")

        return payload

    async def _fetch_latest_action_date(
        self,
        *,
        generated_internal_id: str | None,
    ) -> date | None:
        if not generated_internal_id:
            return None

        response = await self._post_with_retry(
            TRANSACTIONS_PATH,
            json={
                "award_id": generated_internal_id,
                "page": 1,
                "limit": 1,
                "sort": "action_date",
                "order": "desc",
            },
        )
        response.raise_for_status()

        results = self._parse_response(response)["results"]
        if not results:
            return None

        value = results[0].get("action_date")
        if value is None:
            return None

        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise SourceDataError(
                "USAspending returned an invalid transaction action date"
            ) from exc

    async def _enrich_action_dates(
        self,
        awards: list[AwardRecord],
        *,
        concurrency: int | None = None,
        completed_dates: dict[str, date | None] | None = None,
        on_success: Callable[[], None] | None = None,
    ) -> list[AwardRecord]:
        concurrency = self._enrichment_concurrency if concurrency is None else concurrency
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")

        semaphore = asyncio.Semaphore(concurrency)
        known_dates = completed_dates if completed_dates is not None else {}
        lookup_locks: dict[str, asyncio.Lock] = {}

        async def action_date_for(award: AwardRecord) -> date | None:
            identifier = award.generated_internal_id
            if identifier is None:
                return await self._fetch_latest_action_date(generated_internal_id=None)
            lock = lookup_locks.setdefault(identifier, asyncio.Lock())
            async with lock:
                if identifier not in known_dates:
                    value = await self._fetch_latest_action_date(generated_internal_id=identifier)
                    known_dates[identifier] = value
                    if on_success is not None:
                        on_success()
                return known_dates[identifier]

        async def enrich(award: AwardRecord) -> AwardRecord:
            async with semaphore:
                action_date = await action_date_for(award)

            return award.model_copy(
                update={"action_date": action_date},
            )

        tasks = [asyncio.create_task(enrich(award)) for award in awards]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            # A failed page must leave no lookups running when its caller retries
            # or closes the HTTP client. gather alone does not cancel siblings.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _post_with_retry(
        self,
        path: str,
        *,
        json: dict[str, Any],
        attempts: int | None = None,
    ) -> httpx.Response:
        attempts = self._retry_attempts if attempts is None else attempts
        if not 1 <= attempts <= 10:
            raise ValueError("attempts must be between 1 and 10")
        last_error: httpx.HTTPError | None = None

        for attempt in range(1, attempts + 1):
            retry_after = None
            try:
                await self._request_pacer.wait()
                response = await self._client.post(
                    f"{self._base_url}{path}",
                    json=json,
                )
                response.raise_for_status()
                return response

            except RETRYABLE_TRANSPORT_ERRORS as exc:
                _request_failure_context(exc, path=path, attempt=attempt)
                last_error = exc

            except httpx.HTTPStatusError as exc:
                _request_failure_context(exc, path=path, attempt=attempt)
                if exc.response.status_code not in RETRYABLE_STATUS_CODES:
                    exc.__dict__["retry_stop_reason"] = "non_retryable_status"
                    raise
                last_error = exc
                retry_after = _retry_after_seconds(exc.response.headers.get("Retry-After"))
                await exc.response.aclose()

            except httpx.HTTPError as exc:
                _request_failure_context(exc, path=path, attempt=attempt)
                exc.__dict__["retry_stop_reason"] = "non_retryable_transport"
                raise

            if attempt < attempts:
                if retry_after is not None and retry_after > self._retry_max_delay_seconds:
                    assert last_error is not None
                    last_error.add_note(
                        "Retry-After exceeds the configured maximum wait; stopped without "
                        "retrying before the server's requested time."
                    )
                    last_error.__dict__.update(
                        retry_attempts=attempt, retry_stop_reason="retry_after_exceeds_budget"
                    )
                    raise last_error
                backoff = min(
                    self._retry_max_delay_seconds,
                    self._retry_base_delay_seconds * 2 ** (attempt - 1),
                )
                jitter = random.uniform(0.0, min(backoff * 0.25, 1.0))
                delay = min(self._retry_max_delay_seconds, backoff + jitter)
                await asyncio.sleep(max(delay, retry_after or 0.0))

        assert last_error is not None
        last_error.__dict__.update(retry_attempts=attempts, retry_stop_reason="attempt_limit")
        last_error.add_note(f"USAspending request failed after {attempts} bounded attempts.")
        raise last_error
