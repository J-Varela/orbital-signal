"""Small-window collection planning, recovery, and bounded source traversal."""

import asyncio
import calendar
import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from orbital_signal.collection_models import (
    CollectionAttempt,
    CollectionRepository,
    CollectionWindow,
)
from orbital_signal.domain import AwardRecord, IngestionResult
from orbital_signal.repository import SignalRepository
from orbital_signal.services import AwardIngestionService
from orbital_signal.sources.usaspending import DEFAULT_AGENCIES, AwardSearchPage

COVERAGE_NOTE = (
    "Completed means no further pagination was reported for the searched windows, "
    "not guaranteed source completeness. Results are current award snapshots, not "
    "a historical transaction ledger. Counts are source observations, not unique awards; "
    "split probes, retries, and overlapping windows can observe the same award repeatedly. "
    "Cumulative counts include retained attempts for intersecting windows, which can extend "
    "outside the selection. Legacy or interrupted history can provide only lower bounds."
)


def parse_month(value: str) -> date:
    """Parse a strict YYYY-MM period without accepting an arbitrary day."""
    if len(value) != 7 or value[4] != "-":
        raise ValueError("Months must use YYYY-MM")
    try:
        return date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise ValueError("Months must use a valid YYYY-MM") from exc


def month_windows(start_month: date, end_month: date) -> list[tuple[date, date]]:
    """Return inclusive calendar months, retaining the original planning helper."""
    if start_month.day != 1 or end_month.day != 1:
        raise ValueError("Month boundaries must be the first day of a month")
    if end_month < start_month:
        raise ValueError("end_month must be on or after start_month")
    windows = []
    current = start_month
    while True:
        windows.append(
            (current, current.replace(day=calendar.monthrange(current.year, current.month)[1]))
        )
        if current == end_month:
            return windows
        current = (
            date(current.year + 1, 1, 1)
            if current.month == 12
            else current.replace(month=current.month + 1)
        )


def selection_windows(
    *,
    start_month: date | None = None,
    end_month: date | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    window_days: int = 3,
) -> list[tuple[date, date]]:
    """Plan exact inclusive windows without settings, storage, or source access."""
    if window_days < 1:
        raise ValueError("window_days must be positive")
    has_months = start_month is not None or end_month is not None
    has_dates = start_date is not None or end_date is not None
    if has_months == has_dates:
        raise ValueError("Choose either start_month/end_month or start_date/end_date")
    if has_months:
        if start_month is None or end_month is None:
            raise ValueError("Both start_month and end_month are required")
        months = month_windows(start_month, end_month)
        start_date, end_date = months[0][0], months[-1][1]
    if start_date is None or end_date is None:
        raise ValueError("Both start_date and end_date are required")
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    windows = []
    current = start_date
    while True:
        month_end = current.replace(day=calendar.monthrange(current.year, current.month)[1])
        remaining = min((end_date - current).days, (month_end - current).days, window_days - 1)
        end = current + timedelta(days=remaining)
        windows.append((current, end))
        if end == end_date:
            return windows
        current = end + timedelta(days=1)


def _agencies(agencies: tuple[str, ...]) -> tuple[str, ...]:
    if not agencies or any(agency not in DEFAULT_AGENCIES for agency in agencies):
        raise ValueError("Select NASA, Department of Defense, or both")
    return tuple(dict.fromkeys(agencies))


class PagedAwardSource(Protocol):
    async def search_awards_page(
        self,
        *,
        start_date: date,
        end_date: date,
        agency: str,
        page: int = 1,
        records_per_page: int = 100,
    ) -> AwardSearchPage: ...


class CollectionMetrics(BaseModel):
    """Checkpointed observations, never presented as a unique-award total."""

    attempts: int = 0
    fetched_count: int = 0
    relevant_count: int = 0
    stored_count: int = 0
    duplicate_count: int = 0
    failed_attempts: int = 0
    history_complete: bool = True
    unique_awards: int | None = None


class CollectionSummary(BaseModel):
    source: str = "usaspending"
    start_month: date
    end_month: date
    start_date: date | None = None
    end_date: date | None = None
    window_days: int = 3
    agencies: tuple[str, ...] = DEFAULT_AGENCIES
    status: Literal["completed", "incomplete"]
    skipped_windows: int = 0
    windows: list[CollectionWindow]
    attempts: list[CollectionAttempt] = Field(default_factory=list)
    cumulative: CollectionMetrics = Field(default_factory=CollectionMetrics)
    stopped_early: bool = False
    stop_reason: str | None = None
    consecutive_failures: int = 0
    attempted_windows: int = 0
    cached_pages_reused: int = 0
    window_delay_seconds: float | None = None
    max_consecutive_failures: int | None = None
    coverage_note: str = COVERAGE_NOTE


def _segments(
    start: date, end: date, windows: list[CollectionWindow], agency: str
) -> list[tuple[date, date, bool]]:
    """Partition at saved boundaries; prefer precise evidence over old broad probes.

    This also preserves failed fine windows when a broader completed checkpoint
    exists. Completed descendants can supersede failed/split legacy parents.
    """
    relevant = [
        window
        for window in windows
        if window.agency == agency
        and window.status not in {"split", "pending"}
        and window.start_date <= end
        and window.end_date >= start
    ]
    boundaries = {start.toordinal(), end.toordinal() + 1}
    for window in relevant:
        boundaries.add(max(start, window.start_date).toordinal())
        boundaries.add(min(end, window.end_date).toordinal() + 1)
    ordered = sorted(boundaries)
    segments = []
    for left, right in pairwise(ordered):
        segment_start, segment_end = date.fromordinal(left), date.fromordinal(right - 1)
        covering = [
            window
            for window in relevant
            if window.start_date <= segment_start and window.end_date >= segment_end
        ]
        latest = max(
            covering,
            key=lambda window: (
                -(window.end_date - window.start_date).days,
                (window.started_at or datetime.min.replace(tzinfo=UTC)).replace(tzinfo=UTC)
                if window.started_at is None or window.started_at.tzinfo is None
                else window.started_at.astimezone(UTC),
                window.attempts,
                window.status != "completed",
            ),
            default=None,
        )
        segments.append(
            (segment_start, segment_end, latest is not None and latest.status == "completed")
        )
    return segments


async def collection_progress(
    *,
    checkpoints: CollectionRepository,
    start_month: date | None = None,
    end_month: date | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    window_days: int = 3,
    agencies: tuple[str, ...] = DEFAULT_AGENCIES,
    skipped: int = 0,
) -> CollectionSummary:
    plan = selection_windows(
        start_month=start_month,
        end_month=end_month,
        start_date=start_date,
        end_date=end_date,
        window_days=window_days,
    )
    agencies = _agencies(agencies)
    first, last = plan[0][0], plan[-1][1]
    first_month, last_month = first.replace(day=1), last.replace(day=1)
    windows = [
        window
        for window in await checkpoints.list_windows(start_month=first_month, end_month=last_month)
        if window.source == "usaspending"
        and window.agency in agencies
        and window.start_date <= last
        and window.end_date >= first
    ]
    window_ids = {window.window_id for window in windows}
    attempts = [
        attempt
        for attempt in await checkpoints.list_attempts(
            start_month=first_month, end_month=last_month
        )
        if attempt.window_id in window_ids
    ]
    history_complete = all(
        {attempt.attempt_number for attempt in attempts if attempt.window_id == window.window_id}
        == set(range(1, window.attempts + 1))
        for window in windows
    ) and all(attempt.origin == "recorded" and attempt.counts_complete for attempt in attempts)
    cumulative = CollectionMetrics(
        attempts=len(attempts),
        fetched_count=sum(attempt.fetched_count for attempt in attempts),
        relevant_count=sum(attempt.relevant_count for attempt in attempts),
        stored_count=sum(attempt.stored_count for attempt in attempts),
        duplicate_count=sum(attempt.duplicate_count for attempt in attempts),
        failed_attempts=sum(attempt.status == "failed" for attempt in attempts),
        history_complete=history_complete,
    )
    done = all(
        covered
        for start, end in plan
        for agency in agencies
        for _, _, covered in _segments(start, end, windows, agency)
    )
    return CollectionSummary(
        start_month=first_month,
        end_month=last_month,
        start_date=first,
        end_date=last,
        window_days=window_days,
        agencies=agencies,
        status="completed" if done else "incomplete",
        skipped_windows=skipped,
        windows=windows,
        attempts=attempts,
        cumulative=cumulative,
    )


class _PageSource:
    def __init__(self, awards: list[AwardRecord]) -> None:
        self.awards = awards

    async def search_awards(self, *, start_date: date, end_date: date) -> list[AwardRecord]:
        return self.awards


def _finished_at(window: CollectionWindow) -> datetime:
    now = datetime.now(UTC)
    return max(now, window.started_at or now)


def _failure_message(exc: Exception) -> str:
    detail = f"{type(exc).__name__}: {str(exc) or type(exc).__name__}"
    operation = getattr(exc, "source_operation", None)
    if operation is not None:
        detail += f" [operation: {operation}]"
    path = getattr(exc, "source_request_path", None)
    if path is not None:
        detail += f" [endpoint: {path}]"
    attempts = getattr(exc, "retry_attempts", None)
    if attempts is not None:
        detail += f" (HTTP attempts: {attempts}; {getattr(exc, 'retry_stop_reason', 'exhausted')})"
    if getattr(exc, "source_page_cache_hit", False):
        detail += " [search page: retained enrichment cache]"
    return detail[:2000]


class _CollectionStopped(Exception):
    """Unwind all window/subdivision loops after saving the triggering failure."""


@dataclass
class _CollectionRun:
    attempted_windows: int = 0
    consecutive_failures: int = 0
    cached_pages_reused: int = 0
    skipped_windows: int = 0
    stop_reason: str | None = None


class MonthlyCollectionService:
    """Collect sequentially under a shared, database-wide source lock."""

    def __init__(
        self,
        *,
        source: PagedAwardSource,
        repository: SignalRepository,
        checkpoints: CollectionRepository,
        page_limit: int = 5,
        records_per_page: int = 100,
        window_delay_seconds: float = 0.0,
        max_consecutive_failures: int = 3,
    ) -> None:
        if not 1 <= page_limit <= 5:
            raise ValueError("page_limit must be between 1 and 5")
        if not 1 <= records_per_page <= 100:
            raise ValueError("records_per_page must be between 1 and 100")
        if not math.isfinite(window_delay_seconds) or window_delay_seconds < 0:
            raise ValueError("window_delay_seconds must be finite and nonnegative")
        if (
            isinstance(max_consecutive_failures, bool)
            or not isinstance(max_consecutive_failures, int)
            or max_consecutive_failures < 1
        ):
            raise ValueError("max_consecutive_failures must be a positive integer")
        self.source, self.repository, self.checkpoints = source, repository, checkpoints
        self.page_limit, self.records_per_page = page_limit, records_per_page
        self.window_delay_seconds = window_delay_seconds
        self.max_consecutive_failures = max_consecutive_failures

    async def collect(
        self,
        *,
        start_month: date | None = None,
        end_month: date | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        window_days: int = 3,
        agencies: tuple[str, ...] = DEFAULT_AGENCIES,
        refresh: bool = False,
    ) -> CollectionSummary:
        plan = selection_windows(
            start_month=start_month,
            end_month=end_month,
            start_date=start_date,
            end_date=end_date,
            window_days=window_days,
        )
        agencies = _agencies(agencies)
        if end_month is not None and end_month >= date.today().replace(day=1):
            raise ValueError("Only completed calendar months can be collected")
        if end_date is not None and end_date >= date.today():
            raise ValueError("Only dates before today can be collected")
        run = _CollectionRun()
        async with self.checkpoints.collection_lock():
            if refresh:
                clear_cache = getattr(self.source, "clear_cached_pages", None)
                if clear_cache is not None:
                    clear_cache(start_date=plan[0][0], end_date=plan[-1][1], agencies=agencies)
            try:
                for start, end in plan:
                    known = await self.checkpoints.list_windows(
                        start_month=start.replace(day=1), end_month=end.replace(day=1)
                    )
                    known = [window for window in known if window.source == "usaspending"]
                    for agency in agencies:
                        for left, right, covered in _segments(start, end, known, agency):
                            if covered and not refresh:
                                run.skipped_windows += 1
                                continue
                            await self._collect_window(
                                start=left, end=right, agency=agency, refresh=refresh, run=run
                            )
            except _CollectionStopped:
                pass
            summary = await self.progress(
                start_month=start_month,
                end_month=end_month,
                start_date=start_date,
                end_date=end_date,
                window_days=window_days,
                agencies=agencies,
                skipped=run.skipped_windows,
            )
            summary.stopped_early = run.stop_reason is not None
            summary.stop_reason = run.stop_reason
            summary.consecutive_failures = run.consecutive_failures
            summary.attempted_windows = run.attempted_windows
            summary.cached_pages_reused = run.cached_pages_reused
            summary.window_delay_seconds = self.window_delay_seconds
            summary.max_consecutive_failures = self.max_consecutive_failures
            if summary.stopped_early:
                summary.status = "incomplete"
            return summary

    async def progress(
        self,
        *,
        start_month: date | None = None,
        end_month: date | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        window_days: int = 3,
        agencies: tuple[str, ...] = DEFAULT_AGENCIES,
        skipped: int = 0,
    ) -> CollectionSummary:
        return await collection_progress(
            checkpoints=self.checkpoints,
            start_month=start_month,
            end_month=end_month,
            start_date=start_date,
            end_date=end_date,
            window_days=window_days,
            agencies=agencies,
            skipped=skipped,
        )

    async def _collect_window(
        self, *, start: date, end: date, agency: str, refresh: bool, run: _CollectionRun
    ) -> int:
        previous = await self.checkpoints.get_window(
            source="usaspending", agency=agency, start_date=start, end_date=end
        )
        if previous is not None and previous.status == "completed" and not refresh:
            run.skipped_windows += 1
            return 1
        if previous is not None and previous.status == "split" and start != end:
            return await self._collect_children(previous, refresh=refresh, run=run)
        if run.attempted_windows and self.window_delay_seconds:
            await asyncio.sleep(self.window_delay_seconds)
        run.attempted_windows += 1
        if previous is not None and previous.status == "running":
            previous.status = "failed"
            previous.error_message = "Interrupted collection; counters may omit in-flight writes."
            previous.completed_at = _finished_at(previous)
            await self.checkpoints.save_window(previous, counts_complete=False)
        identity = f"usaspending:{agency}:{start.isoformat()}:{end.isoformat()}"
        window = CollectionWindow(
            window_id=previous.window_id
            if previous
            else hashlib.sha256(identity.encode()).hexdigest()[:32],
            agency=agency,
            month_start=start.replace(day=1),
            start_date=start,
            end_date=end,
            status="running",
            attempts=previous.attempts + 1 if previous else 1,
            started_at=datetime.now(UTC),
        )
        await self.checkpoints.save_window(window, counts_complete=False)
        processing_page = False
        counts_complete = True
        try:
            for page_number in range(1, self.page_limit + 1):
                page = await self.source.search_awards_page(
                    start_date=start,
                    end_date=end,
                    agency=agency,
                    page=page_number,
                    records_per_page=self.records_per_page,
                )
                run.cached_pages_reused += bool(getattr(page, "cache_hit", False))
                window.pages_fetched += 1
                window.fetched_count += page.fetched_count
                window.has_more = page.has_more
                await self.checkpoints.save_window(window, counts_complete=False)
                prior_counts = window.relevant_count, window.stored_count, window.duplicate_count

                def record_progress(
                    result: IngestionResult, *, prior: tuple[int, int, int] = prior_counts
                ) -> None:
                    window.relevant_count = prior[0] + result.relevant_count
                    window.stored_count = prior[1] + result.stored_count
                    window.duplicate_count = prior[2] + result.duplicate_count

                processing_page = True
                result = await AwardIngestionService(
                    source=_PageSource(page.awards), repository=self.repository
                ).ingest(start_date=start, end_date=end, on_progress=record_progress)
                processing_page = False
                record_progress(result)
                await self.checkpoints.save_window(window, counts_complete=False)
                if not page.has_more:
                    window.status = "completed"
                    break
            else:
                window.status = "incomplete" if start == end else "split"
                window.error_message = (
                    "Single-day page limit reached; more source results remain."
                    if start == end
                    else None
                )
        except asyncio.CancelledError:
            window.status = "failed"
            window.error_message = "Interrupted collection; counters may omit in-flight writes."
            window.completed_at = _finished_at(window)
            await asyncio.shield(self.checkpoints.save_window(window, counts_complete=False))
            raise
        except Exception as exc:
            counts_complete = not processing_page
            run.cached_pages_reused += bool(getattr(exc, "source_page_cache_hit", False))
            if getattr(exc, "source_page_received", False):
                window.pages_fetched += 1
                window.fetched_count += getattr(exc, "source_page_rows", 0)
            window.status = "failed"
            window.error_message = _failure_message(exc)
        window.completed_at = _finished_at(window)
        await self.checkpoints.save_window(window, counts_complete=counts_complete)
        if window.status == "failed":
            run.consecutive_failures += 1
            if run.consecutive_failures >= self.max_consecutive_failures:
                run.stop_reason = (
                    f"Stopped after {run.consecutive_failures} consecutive failed window attempts. "
                    f"Last failed window: {agency}, {start.isoformat()} through {end.isoformat()}. "
                    "Saved progress is retained; remaining windows were not attempted."
                )
                raise _CollectionStopped
        else:
            run.consecutive_failures = 0
        if window.status == "split":
            return await self._collect_children(window, refresh=refresh, run=run)
        return 0

    async def _collect_children(
        self, window: CollectionWindow, *, refresh: bool, run: _CollectionRun
    ) -> int:
        midpoint = window.start_date + (window.end_date - window.start_date) // 2
        skipped = 0
        for start, end in (
            (window.start_date, midpoint),
            (midpoint + timedelta(days=1), window.end_date),
        ):
            skipped += await self._collect_window(
                start=start, end=end, agency=window.agency, refresh=refresh, run=run
            )
        return skipped
