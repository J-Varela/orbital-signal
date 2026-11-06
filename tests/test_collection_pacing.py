from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from orbital_signal.collection import MonthlyCollectionService
from orbital_signal.collection_models import CollectionWindow
from orbital_signal.collection_repository import InMemoryCollectionRepository
from orbital_signal.domain import AwardRecord
from orbital_signal.repository import InMemorySignalRepository
from orbital_signal.services import build_company_signal
from orbital_signal.sources.usaspending import DEFAULT_AGENCIES, AwardSearchPage

NASA, DOD = DEFAULT_AGENCIES
START = date(2026, 9, 23)


@dataclass(frozen=True)
class Request:
    agency: str
    start: date
    end: date
    page: int


class Source:
    def __init__(self, respond: Callable[[Request], AwardSearchPage]) -> None:
        self.respond = respond
        self.requests: list[Request] = []

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


def empty_page(*, has_more: bool = False) -> AwardSearchPage:
    return AwardSearchPage(awards=[], fetched_count=0, has_more=has_more)


async def checkpoint(
    checkpoints: InMemoryCollectionRepository,
    start: date,
    end: date | None = None,
    agency: str = NASA,
) -> CollectionWindow | None:
    return await checkpoints.get_window(
        source="usaspending", agency=agency, start_date=start, end_date=end or start
    )


async def test_failed_window_limit_preserves_progress_and_skipped_days_are_neutral(
    satellite_award: AwardRecord,
) -> None:
    previously_completed = START + timedelta(days=1)
    second_failure = START + timedelta(days=2)
    not_yet_attempted = START + timedelta(days=3)
    repository = InMemorySignalRepository()
    existing_signal = build_company_signal(satellite_award, as_of=START)
    assert existing_signal is not None
    await repository.upsert(satellite_award, existing_signal)
    checkpoints = InMemoryCollectionRepository()
    saved = CollectionWindow(
        window_id="completed-before-this-invocation",
        agency=NASA,
        month_start=START.replace(day=1),
        start_date=previously_completed,
        end_date=previously_completed,
        attempts=1,
        status="completed",
        started_at=datetime(2026, 11, 1, tzinfo=UTC),
        stored_count=1,
        fetched_count=1,
        relevant_count=1,
        has_more=False,
    )
    await checkpoints.save_window(saved)
    fail = True

    def respond(request: Request) -> AwardSearchPage:
        if fail:
            error = httpx.RemoteProtocolError("simulated source disconnect")
            if request.start == START:
                error.__dict__.update(
                    source_operation="transaction_enrichment",
                    source_page_received=True,
                    source_page_rows=7,
                )
            else:
                error.__dict__.update(source_operation="award_search")
            raise error
        return empty_page()

    source = Source(respond)
    service = MonthlyCollectionService(
        source=source,
        repository=repository,
        checkpoints=checkpoints,
        window_delay_seconds=0,
        max_consecutive_failures=2,
    )
    stopped = await service.collect(
        start_date=START, end_date=not_yet_attempted, window_days=1, agencies=(NASA,)
    )

    assert [request.start for request in source.requests] == [START, second_failure]
    assert stopped.stopped_early is True
    assert stopped.status == "incomplete"
    assert stopped.stop_reason
    assert stopped.consecutive_failures == stopped.attempted_windows == 2
    assert stopped.skipped_windows == 1
    assert await checkpoint(checkpoints, not_yet_attempted) is None
    assert await checkpoint(checkpoints, previously_completed) == saved
    first_failed = await checkpoint(checkpoints, START)
    assert first_failed is not None and first_failed.status == "failed"
    assert first_failed.pages_fetched == 1
    assert first_failed.fetched_count == 7
    assert first_failed.stored_count == first_failed.duplicate_count == 0
    assert "transaction_enrichment" in (first_failed.error_message or "")
    assert "simulated source disconnect" in (first_failed.error_message or "")
    assert len(stopped.attempts) == 3
    assert sum(attempt.status == "failed" for attempt in stopped.attempts) == 2
    assert await repository.list() == [existing_signal]

    fail = False
    source.requests.clear()
    resumed = await service.collect(
        start_date=START, end_date=not_yet_attempted, window_days=1, agencies=(NASA,)
    )

    assert [request.start for request in source.requests] == [
        START,
        second_failure,
        not_yet_attempted,
    ]
    assert resumed.status == "completed"
    assert resumed.stopped_early is False
    assert resumed.stop_reason is None
    assert resumed.consecutive_failures == 0
    assert resumed.attempted_windows == 3
    assert resumed.skipped_windows == 1
    assert await checkpoint(checkpoints, previously_completed) == saved
    assert await repository.list() == [existing_signal]


async def test_successful_attempt_resets_the_failure_streak() -> None:
    failed_days = {START + timedelta(days=offset) for offset in (0, 2, 4)}

    def respond(request: Request) -> AwardSearchPage:
        if request.start in failed_days:
            raise httpx.ReadTimeout("intermittent source outage")
        return empty_page()

    source = Source(respond)
    result = await MonthlyCollectionService(
        source=source,
        repository=InMemorySignalRepository(),
        checkpoints=InMemoryCollectionRepository(),
        window_delay_seconds=0,
        max_consecutive_failures=2,
    ).collect(
        start_date=START,
        end_date=START + timedelta(days=4),
        window_days=1,
        agencies=(NASA,),
    )

    assert len(source.requests) == result.attempted_windows == 5
    assert result.stopped_early is False
    assert result.stop_reason is None
    assert result.status == "incomplete"
    assert result.consecutive_failures == 1
    assert sum(window.status == "completed" for window in result.windows) == 2
    assert sum(window.status == "failed" for window in result.windows) == 3


async def test_stopping_preserves_existing_failed_windows_that_were_not_retried() -> None:
    checkpoints = InMemoryCollectionRepository()
    saved: dict[date, CollectionWindow] = {}
    for offset in range(4):
        day = START + timedelta(days=offset)
        saved[day] = CollectionWindow(
            window_id=f"prior-failed-day-{offset}",
            agency=NASA,
            month_start=START.replace(day=1),
            start_date=day,
            end_date=day,
            attempts=5,
            status="failed",
            error_message="failure from an earlier invocation",
            fetched_count=10,
            stored_count=1,
        )
        await checkpoints.save_window(saved[day])

    def respond(request: Request) -> AwardSearchPage:
        raise httpx.ReadTimeout("source remains unavailable")

    source = Source(respond)
    stopped = await MonthlyCollectionService(
        source=source,
        repository=InMemorySignalRepository(),
        checkpoints=checkpoints,
        max_consecutive_failures=2,
    ).collect(
        start_date=START,
        end_date=START + timedelta(days=3),
        window_days=1,
        agencies=(NASA,),
    )

    assert [request.start for request in source.requests] == [START, START + timedelta(days=1)]
    assert stopped.stopped_early is True
    assert stopped.consecutive_failures == stopped.attempted_windows == 2
    for offset in (2, 3):
        day = START + timedelta(days=offset)
        assert await checkpoint(checkpoints, day) == saved[day]


async def test_failure_limit_unwinds_split_children_and_remaining_agencies() -> None:
    end = START + timedelta(days=3)
    midpoint = START + timedelta(days=1)
    fail = True

    def respond(request: Request) -> AwardSearchPage:
        if (request.start, request.end) == (START, end):
            return empty_page(has_more=True)
        if fail:
            raise httpx.ConnectError("split child unavailable")
        return empty_page()

    source = Source(respond)
    checkpoints = InMemoryCollectionRepository()
    service = MonthlyCollectionService(
        source=source,
        repository=InMemorySignalRepository(),
        checkpoints=checkpoints,
        page_limit=1,
        window_delay_seconds=0,
        max_consecutive_failures=1,
    )
    stopped = await service.collect(start_date=START, end_date=end, window_days=4)

    assert source.requests == [Request(NASA, START, end, 1), Request(NASA, START, midpoint, 1)]
    assert stopped.stopped_early is True
    assert stopped.consecutive_failures == 1
    assert stopped.attempted_windows == 2
    parent = await checkpoint(checkpoints, START, end)
    assert parent is not None and parent.status == "split"
    child = await checkpoint(checkpoints, START, midpoint)
    assert child is not None and child.status == "failed"
    assert await checkpoint(checkpoints, midpoint + timedelta(days=1), end) is None
    assert await checkpoint(checkpoints, START, end, agency=DOD) is None

    fail = False
    source.requests.clear()
    resumed = await service.collect(start_date=START, end_date=end, window_days=4)
    assert resumed.stopped_early is False
    assert resumed.status == "completed"
    assert Request(NASA, START, end, 1) not in source.requests
    assert {request.agency for request in source.requests} == {NASA, DOD}


async def test_window_pacing_occurs_between_attempts_not_between_pages_or_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, str | float, int | None]] = []
    end = START + timedelta(days=2)
    skipped = START + timedelta(days=1)
    checkpoints = InMemoryCollectionRepository()
    await checkpoints.save_window(
        CollectionWindow(
            window_id="already-completed",
            agency=NASA,
            month_start=START.replace(day=1),
            start_date=skipped,
            end_date=skipped,
            status="completed",
            attempts=1,
            has_more=False,
        )
    )

    async def sleep(seconds: float) -> None:
        events.append(("sleep", seconds, None))

    def respond(request: Request) -> AwardSearchPage:
        events.append(("request", request.start.isoformat(), request.page))
        return empty_page(has_more=request.page == 1)

    monkeypatch.setattr("orbital_signal.collection.asyncio.sleep", sleep)
    source = Source(respond)
    result = await MonthlyCollectionService(
        source=source,
        repository=InMemorySignalRepository(),
        checkpoints=checkpoints,
        window_delay_seconds=2.5,
        max_consecutive_failures=3,
    ).collect(start_date=START, end_date=end, window_days=1, agencies=(NASA,))

    assert result.status == "completed"
    assert result.attempted_windows == 2
    assert result.skipped_windows == 1
    assert events == [
        ("request", START.isoformat(), 1),
        ("request", START.isoformat(), 2),
        ("sleep", 2.5, None),
        ("request", end.isoformat(), 1),
        ("request", end.isoformat(), 2),
    ]


async def test_successful_split_probe_resets_prior_failure_before_a_child_fails() -> None:
    first = START
    parent_start, parent_end = START + timedelta(days=2), START + timedelta(days=3)
    final_start, final_end = START + timedelta(days=4), START + timedelta(days=5)

    def respond(request: Request) -> AwardSearchPage:
        if request.start == first or request.start == final_start:
            raise httpx.ReadTimeout("intermittent source failure")
        if request.start == request.end == parent_start:
            raise httpx.ReadTimeout("left child source failure")
        return empty_page(has_more=True)

    source = Source(respond)
    result = await MonthlyCollectionService(
        source=source,
        repository=InMemorySignalRepository(),
        checkpoints=InMemoryCollectionRepository(),
        page_limit=1,
        max_consecutive_failures=2,
    ).collect(start_date=first, end_date=final_end, window_days=2, agencies=(NASA,))

    assert Request(NASA, parent_start, parent_end, 1) in source.requests
    assert Request(NASA, final_start, final_end, 1) in source.requests
    assert result.stopped_early is False
    assert result.status == "incomplete"
    assert result.consecutive_failures == 1
    assert any(window.status == "incomplete" for window in result.windows)


async def test_window_pacing_also_applies_to_split_children_and_agency_transitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    end = START + timedelta(days=1)
    events: list[str] = []

    async def sleep(seconds: float) -> None:
        assert seconds == 1.5
        events.append("pause")

    def respond(request: Request) -> AwardSearchPage:
        events.append(f"{request.agency}:{request.start}:{request.end}")
        return empty_page(
            has_more=request.agency == NASA and request.start == START and request.end == end
        )

    monkeypatch.setattr("orbital_signal.collection.asyncio.sleep", sleep)
    result = await MonthlyCollectionService(
        source=Source(respond),
        repository=InMemorySignalRepository(),
        checkpoints=InMemoryCollectionRepository(),
        page_limit=1,
        window_delay_seconds=1.5,
    ).collect(start_date=START, end_date=end, window_days=2)

    assert result.status == "completed"
    assert result.attempted_windows == 4
    assert events == [
        f"{NASA}:{START}:{end}",
        "pause",
        f"{NASA}:{START}:{START}",
        "pause",
        f"{NASA}:{end}:{end}",
        "pause",
        f"{DOD}:{START}:{end}",
    ]


async def test_single_day_incomplete_window_resets_prior_failure_without_claiming_coverage() -> (
    None
):
    middle = START + timedelta(days=1)

    def respond(request: Request) -> AwardSearchPage:
        if request.start == middle:
            return empty_page(has_more=True)
        raise httpx.ReadTimeout("intermittent source failure")

    result = await MonthlyCollectionService(
        source=Source(respond),
        repository=InMemorySignalRepository(),
        checkpoints=InMemoryCollectionRepository(),
        page_limit=1,
        max_consecutive_failures=2,
    ).collect(
        start_date=START,
        end_date=START + timedelta(days=2),
        window_days=1,
        agencies=(NASA,),
    )

    assert result.attempted_windows == 3
    assert result.stopped_early is False
    assert result.status == "incomplete"
    assert result.consecutive_failures == 1
    assert (
        next(window for window in result.windows if window.start_date == middle).status
        == "incomplete"
    )


async def test_default_policy_stops_after_three_new_failed_windows() -> None:
    def respond(request: Request) -> AwardSearchPage:
        raise httpx.ReadTimeout("persistent source failure")

    source = Source(respond)
    result = await MonthlyCollectionService(
        source=source,
        repository=InMemorySignalRepository(),
        checkpoints=InMemoryCollectionRepository(),
    ).collect(
        start_date=START,
        end_date=START + timedelta(days=3),
        window_days=1,
        agencies=(NASA,),
    )

    assert len(source.requests) == result.attempted_windows == result.consecutive_failures == 3
    assert result.stopped_early is True
    assert len(result.windows) == 3


@pytest.mark.parametrize("delay", [-1.0, float("nan"), float("inf"), -float("inf")])
def test_invalid_window_delay_is_rejected(delay: float) -> None:
    with pytest.raises(ValueError):
        MonthlyCollectionService(
            source=Source(lambda _: empty_page()),
            repository=InMemorySignalRepository(),
            checkpoints=InMemoryCollectionRepository(),
            window_delay_seconds=delay,
        )


@pytest.mark.parametrize("limit", [0, -1])
def test_invalid_consecutive_failure_limit_is_rejected(limit: int) -> None:
    with pytest.raises(ValueError):
        MonthlyCollectionService(
            source=Source(lambda _: empty_page()),
            repository=InMemorySignalRepository(),
            checkpoints=InMemoryCollectionRepository(),
            max_consecutive_failures=limit,
        )
