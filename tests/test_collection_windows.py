from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any

import httpx
import pytest

from orbital_signal.collection import MonthlyCollectionService, selection_windows
from orbital_signal.collection_models import CollectionStatus, CollectionWindow
from orbital_signal.collection_repository import InMemoryCollectionRepository
from orbital_signal.domain import AwardRecord
from orbital_signal.repository import InMemorySignalRepository
from orbital_signal.sources.usaspending import DEFAULT_AGENCIES, AwardSearchPage

NASA, DOD = DEFAULT_AGENCIES
JANUARY = date(2026, 1, 1)
JANUARY_END = date(2026, 1, 31)


@dataclass(frozen=True)
class Request:
    agency: str
    start: date
    end: date
    page: int


class Source:
    def __init__(self, respond: Callable[[Request], AwardSearchPage] | None = None) -> None:
        self.requests: list[Request] = []
        self.respond = respond or (lambda _: AwardSearchPage([], 0, False))

    async def search_awards_page(
        self,
        *,
        start_date: date,
        end_date: date,
        agency: str,
        page: int = 1,
        records_per_page: int = 100,
    ) -> AwardSearchPage:
        request = Request(agency, start_date, end_date, page)
        self.requests.append(request)
        return self.respond(request)


def saved_window(
    agency: str,
    start: date,
    end: date,
    status: CollectionStatus,
    *,
    started_at: datetime | None = None,
) -> CollectionWindow:
    return CollectionWindow(
        window_id=f"{'NASA' if agency == NASA else 'DOD'}:{start}:{end}",
        agency=agency,
        month_start=start.replace(day=1),
        start_date=start,
        end_date=end,
        status=status,
        attempts=1,
        started_at=started_at or datetime(2026, 10, 1, tzinfo=UTC),
        has_more=status in {"failed", "incomplete", "split"},
    )


def days(start: date, end: date) -> set[date]:
    return {start + timedelta(days=offset) for offset in range((end - start).days + 1)}


def request_days(requests: list[Request], agency: str) -> list[date]:
    return [
        day
        for request in requests
        if request.agency == agency
        for day in sorted(days(request.start, request.end))
    ]


def test_default_window_plan_covers_whole_month_without_gaps_or_crossing_boundaries() -> None:
    windows = selection_windows(start_month=JANUARY, end_month=JANUARY)

    assert len(windows) == 11
    assert windows[0] == (date(2026, 1, 1), date(2026, 1, 3))
    assert windows[-1] == (JANUARY_END, JANUARY_END)
    assert all(1 <= (end - start).days + 1 <= 3 for start, end in windows)
    assert all(end + timedelta(days=1) == right for (_, end), (right, _) in pairwise(windows))
    assert [day for start, end in windows for day in sorted(days(start, end))] == sorted(
        days(JANUARY, JANUARY_END)
    )


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            date(2024, 2, 27),
            date(2024, 3, 4),
            [
                (date(2024, 2, 27), date(2024, 2, 29)),
                (date(2024, 3, 1), date(2024, 3, 3)),
                (date(2024, 3, 4), date(2024, 3, 4)),
            ],
        ),
        (
            date(2025, 12, 30),
            date(2026, 1, 2),
            [
                (date(2025, 12, 30), date(2025, 12, 31)),
                (date(2026, 1, 1), date(2026, 1, 2)),
            ],
        ),
    ],
)
def test_explicit_date_plan_preserves_leap_days_and_month_year_boundaries(
    start: date, end: date, expected: list[tuple[date, date]]
) -> None:
    assert selection_windows(start_date=start, end_date=end) == expected


@pytest.mark.parametrize(
    "selection",
    [
        {},
        {"start_month": JANUARY},
        {"start_date": JANUARY},
        {"start_date": JANUARY_END, "end_date": JANUARY},
        {"start_month": JANUARY, "end_month": JANUARY, "start_date": JANUARY},
        {"start_date": JANUARY, "end_date": JANUARY_END, "window_days": 0},
        {"start_date": JANUARY, "end_date": JANUARY_END, "window_days": -1},
    ],
)
def test_invalid_or_mixed_window_selections_are_rejected(selection: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        selection_windows(**selection)


async def test_default_collection_uses_three_day_windows_for_both_agencies() -> None:
    source = Source()
    service = MonthlyCollectionService(
        source=source,
        repository=InMemorySignalRepository(),
        checkpoints=InMemoryCollectionRepository(),
    )

    result = await service.collect(start_month=JANUARY, end_month=JANUARY)

    assert result.status == "completed"
    assert result.window_days == 3
    assert len(source.requests) == len(result.windows) == 22
    for agency in DEFAULT_AGENCIES:
        collected = request_days(source.requests, agency)
        assert collected == sorted(days(JANUARY, JANUARY_END))
    assert all(request.page == 1 for request in source.requests)
    assert all((request.end - request.start).days < 3 for request in source.requests)


@pytest.mark.parametrize("agencies", [(NASA,), (DOD,), (NASA, NASA, DOD)])
async def test_explicit_period_requests_only_selected_agencies_once(
    agencies: tuple[str, ...],
) -> None:
    source = Source()
    service = MonthlyCollectionService(
        source=source,
        repository=InMemorySignalRepository(),
        checkpoints=InMemoryCollectionRepository(),
    )
    start, end = date(2026, 1, 17), date(2026, 1, 20)

    result = await service.collect(start_date=start, end_date=end, agencies=agencies)

    assert result.status == "completed"
    assert result.start_date == start and result.end_date == end
    assert set(result.agencies) == set(agencies)
    assert len(source.requests) == 2 * len(set(agencies))
    for agency in set(agencies):
        assert request_days(source.requests, agency) == sorted(days(start, end))
    assert all(request.agency in agencies for request in source.requests)


@pytest.mark.parametrize("agencies", [(), ("DARPA",), (NASA, "Department of Energy")])
async def test_unsupported_agency_selection_fails_before_source_access(
    agencies: tuple[str, ...],
) -> None:
    source = Source()
    service = MonthlyCollectionService(
        source=source,
        repository=InMemorySignalRepository(),
        checkpoints=InMemoryCollectionRepository(),
    )

    with pytest.raises(ValueError):
        await service.collect(start_date=JANUARY, end_date=JANUARY, agencies=agencies)

    assert source.requests == []


async def test_legacy_completed_month_and_partial_split_month_resume_only_missing_half() -> None:
    checkpoints = InMemoryCollectionRepository()
    for window in (
        saved_window(NASA, JANUARY, JANUARY_END, "completed"),
        saved_window(DOD, JANUARY, JANUARY_END, "split"),
        saved_window(DOD, JANUARY, date(2026, 1, 16), "completed"),
        saved_window(DOD, date(2026, 1, 17), JANUARY_END, "failed"),
    ):
        await checkpoints.save_window(window)
    source = Source()
    service = MonthlyCollectionService(
        source=source, repository=InMemorySignalRepository(), checkpoints=checkpoints
    )

    result = await service.collect(start_month=JANUARY, end_month=JANUARY)

    assert result.status == "completed"
    assert all(request.agency == DOD for request in source.requests)
    assert request_days(source.requests, DOD) == sorted(days(date(2026, 1, 17), JANUARY_END))
    assert all((request.end - request.start).days < 3 for request in source.requests)
    assert any(window.status == "split" for window in result.windows)
    assert any(window.status == "failed" for window in result.windows)
    source.requests.clear()
    repeated = await service.collect(start_month=JANUARY, end_month=JANUARY)
    assert repeated.status == "completed"
    assert source.requests == []


async def test_completed_descendants_supersede_newer_failed_legacy_parent() -> None:
    checkpoints = InMemoryCollectionRepository()
    for window in (
        saved_window(
            NASA,
            JANUARY,
            JANUARY_END,
            "failed",
            started_at=datetime(2026, 11, 1, tzinfo=UTC),
        ),
        saved_window(NASA, JANUARY, date(2026, 1, 16), "completed"),
        saved_window(NASA, date(2026, 1, 17), JANUARY_END, "failed"),
        saved_window(DOD, JANUARY, JANUARY_END, "completed"),
    ):
        await checkpoints.save_window(window)
    source = Source()
    service = MonthlyCollectionService(
        source=source, repository=InMemorySignalRepository(), checkpoints=checkpoints
    )
    assert (await service.progress(start_month=JANUARY, end_month=JANUARY)).status == "incomplete"

    result = await service.collect(start_month=JANUARY, end_month=JANUARY)

    assert request_days(source.requests, NASA) == sorted(days(date(2026, 1, 17), JANUARY_END))
    assert all(request.agency == NASA for request in source.requests)
    assert result.status == "completed"
    assert any(
        window.status == "failed" and window.start_date == JANUARY for window in result.windows
    )


async def test_failed_fine_window_overrides_broader_completed_checkpoint() -> None:
    checkpoints = InMemoryCollectionRepository()
    failed_start, failed_end = date(2026, 1, 11), date(2026, 1, 13)
    for window in (
        saved_window(
            NASA,
            JANUARY,
            JANUARY_END,
            "completed",
            started_at=datetime(2026, 11, 1, tzinfo=UTC),
        ),
        saved_window(NASA, failed_start, failed_end, "failed"),
        saved_window(DOD, JANUARY, JANUARY_END, "completed"),
    ):
        await checkpoints.save_window(window)
    source = Source()
    service = MonthlyCollectionService(
        source=source, repository=InMemorySignalRepository(), checkpoints=checkpoints
    )
    assert (await service.progress(start_month=JANUARY, end_month=JANUARY)).status == "incomplete"

    result = await service.collect(start_month=JANUARY, end_month=JANUARY)

    assert request_days(source.requests, NASA) == sorted(days(failed_start, failed_end))
    assert all(request.agency == NASA for request in source.requests)
    assert result.status == "completed"


@pytest.mark.parametrize("failed_is_newer", [False, True])
async def test_equally_precise_overlapping_checkpoints_use_the_latest_attempt(
    failed_is_newer: bool,
) -> None:
    older = datetime(2026, 10, 1, tzinfo=UTC)
    newer = datetime(2026, 11, 1, tzinfo=UTC)
    checkpoints = InMemoryCollectionRepository()
    for window in (
        saved_window(
            NASA,
            JANUARY,
            date(2026, 1, 3),
            "completed",
            started_at=older if failed_is_newer else newer,
        ),
        saved_window(
            NASA,
            date(2026, 1, 2),
            date(2026, 1, 4),
            "failed",
            started_at=newer if failed_is_newer else older,
        ),
    ):
        await checkpoints.save_window(window)
    source = Source()
    service = MonthlyCollectionService(
        source=source, repository=InMemorySignalRepository(), checkpoints=checkpoints
    )

    result = await service.collect(
        start_date=JANUARY,
        end_date=date(2026, 1, 4),
        window_days=31,
        agencies=(NASA,),
    )

    expected = days(date(2026, 1, 2), date(2026, 1, 4)) if failed_is_newer else {date(2026, 1, 4)}
    assert request_days(source.requests, NASA) == sorted(expected)
    assert result.status == "completed"


async def test_a_single_uncovered_day_is_not_hidden_between_completed_legacy_windows() -> None:
    checkpoints = InMemoryCollectionRepository()
    for window in (
        saved_window(NASA, JANUARY, date(2026, 1, 10), "completed"),
        saved_window(NASA, date(2026, 1, 12), JANUARY_END, "completed"),
        saved_window(DOD, JANUARY, JANUARY_END, "completed"),
    ):
        await checkpoints.save_window(window)
    source = Source()
    service = MonthlyCollectionService(
        source=source, repository=InMemorySignalRepository(), checkpoints=checkpoints
    )
    assert (await service.progress(start_month=JANUARY, end_month=JANUARY)).status == "incomplete"

    result = await service.collect(start_month=JANUARY, end_month=JANUARY)

    assert source.requests == [Request(NASA, date(2026, 1, 11), date(2026, 1, 11), 1)]
    assert result.status == "completed"


async def test_overlapping_completed_legacy_windows_cover_selection_without_new_requests() -> None:
    checkpoints = InMemoryCollectionRepository()
    for window in (
        saved_window(NASA, JANUARY, date(2026, 1, 16), "completed"),
        saved_window(NASA, date(2026, 1, 10), JANUARY_END, "completed"),
    ):
        await checkpoints.save_window(window)
    source = Source()
    service = MonthlyCollectionService(
        source=source, repository=InMemorySignalRepository(), checkpoints=checkpoints
    )

    progress = await service.progress(start_month=JANUARY, end_month=JANUARY, agencies=(NASA,))
    resumed = await service.collect(start_month=JANUARY, end_month=JANUARY, agencies=(NASA,))

    assert progress.status == resumed.status == "completed"
    assert source.requests == []


async def test_single_day_selection_never_expands_to_legacy_parent_window(
    satellite_award: AwardRecord,
) -> None:
    selected = date(2026, 1, 17)
    checkpoints = InMemoryCollectionRepository()
    await checkpoints.save_window(saved_window(NASA, date(2026, 1, 17), JANUARY_END, "failed"))
    source = Source(lambda _: AwardSearchPage([satellite_award], 1, True))
    service = MonthlyCollectionService(
        source=source,
        repository=InMemorySignalRepository(),
        checkpoints=checkpoints,
        page_limit=1,
    )

    result = await service.collect(start_date=selected, end_date=selected, agencies=(NASA,))

    assert source.requests == [Request(NASA, selected, selected, 1)]
    assert result.status == "incomplete"
    [window] = [
        window for window in result.windows if window.start_date == window.end_date == selected
    ]
    assert window.status == "incomplete"
    assert window.has_more is True


async def test_refresh_preserves_finer_saved_boundaries_and_deduplicates_signals(
    satellite_award: AwardRecord,
) -> None:
    source = Source(lambda _: AwardSearchPage([satellite_award], 1, False))
    checkpoints = InMemoryCollectionRepository()
    repository = InMemorySignalRepository()
    service = MonthlyCollectionService(
        source=source, repository=repository, checkpoints=checkpoints
    )
    last = date(2026, 1, 3)
    await service.collect(start_date=JANUARY, end_date=last, window_days=1, agencies=(NASA,))
    [original_signal] = await repository.list()
    await checkpoints.save_window(saved_window(NASA, JANUARY, last, "completed"))
    source.requests.clear()

    result = await service.collect(
        start_date=JANUARY, end_date=last, agencies=(NASA,), refresh=True
    )

    assert source.requests == [Request(NASA, day, day, 1) for day in sorted(days(JANUARY, last))]
    assert result.status == "completed"
    retried = [window for window in result.windows if window.start_date == window.end_date]
    assert len(retried) == 3
    assert all(window.attempts == 2 and window.duplicate_count == 1 for window in retried)
    assert await repository.count() == 1
    [signal] = await repository.list()
    assert signal.signal_id == original_signal.signal_id
    assert signal.detected_at == original_signal.detected_at


async def test_resuming_failed_small_window_restarts_page_one_and_retains_attempt_totals(
    satellite_award: AwardRecord,
) -> None:
    fail = True
    error = httpx.RemoteProtocolError("server disconnected")
    error.__dict__.update(retry_attempts=4, retry_stop_reason="attempt_limit")
    second_award = satellite_award.model_copy(
        update={"source_award_id": "SECOND", "generated_internal_id": "CONT_AWD_SECOND"}
    )

    def respond(request: Request) -> AwardSearchPage:
        if request.page == 1:
            return AwardSearchPage([satellite_award], 1, True)
        if fail:
            raise error
        return AwardSearchPage([second_award], 1, False)

    source = Source(respond)
    checkpoints = InMemoryCollectionRepository()
    repository = InMemorySignalRepository()
    service = MonthlyCollectionService(
        source=source, repository=repository, checkpoints=checkpoints
    )
    last = date(2026, 1, 3)

    first = await service.collect(start_date=JANUARY, end_date=last, agencies=(NASA,))
    assert first.status == "incomplete"
    assert first.windows[0].stored_count == 1
    assert "RemoteProtocolError" in (first.windows[0].error_message or "")
    assert "HTTP attempts: 4" in (first.windows[0].error_message or "")
    assert "attempt_limit" in (first.windows[0].error_message or "")
    source.requests.clear()
    fail = False

    resumed = await service.collect(start_date=JANUARY, end_date=last, agencies=(NASA,))

    assert source.requests == [Request(NASA, JANUARY, last, 1), Request(NASA, JANUARY, last, 2)]
    assert resumed.status == "completed"
    assert len(resumed.attempts) == 2
    assert {attempt.attempt_number for attempt in resumed.attempts} == {1, 2}
    assert resumed.windows[0].stored_count == resumed.windows[0].duplicate_count == 1
    assert resumed.cumulative.attempts == 2
    assert resumed.cumulative.fetched_count == 3
    assert resumed.cumulative.stored_count == 2
    assert resumed.cumulative.duplicate_count == 1
    assert resumed.cumulative.failed_attempts == 1
    assert resumed.cumulative.unique_awards is None
    assert await repository.count() == 2
