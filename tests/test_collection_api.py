from datetime import date
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

import orbital_signal.api as api_module
from orbital_signal.api import create_app
from orbital_signal.collection_models import CollectionWindow
from orbital_signal.collection_repository import InMemoryCollectionRepository
from orbital_signal.domain import AwardRecord
from orbital_signal.repository import InMemorySignalRepository
from orbital_signal.sources.usaspending import (
    DEFAULT_AGENCIES,
    AwardSearchPage,
    SourceDataError,
    USAspendingClient,
)

MONTHLY_PATH = "/api/v1/collections/monthly"
JANUARY_PARAMS = {"start_month": "2026-01", "end_month": "2026-01", "window_days": "31"}


@pytest.fixture(autouse=True)
def no_real_policy_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORBITAL_SIGNAL_COLLECTION_WINDOW_DELAY_SECONDS", "0")
    monkeypatch.setenv("ORBITAL_SIGNAL_HTTP_REQUEST_INTERVAL_SECONDS", "0")


def historical_award(award: AwardRecord, agency: str, month: date) -> AwardRecord:
    identifier = f"{'NASA' if agency == DEFAULT_AGENCIES[0] else 'DOD'}-{month:%Y-%m}"
    return award.model_copy(
        update={
            "source_award_id": identifier,
            "generated_internal_id": f"CONT_AWD_{identifier}",
            "recipient_name": "Historical Orbit LLC",
            "awarding_agency": agency,
            "action_date": month,
        }
    )


async def test_progress_starts_empty_without_contacting_source(monkeypatch) -> None:
    async def unexpected_source_call(self, **kwargs: Any) -> AwardSearchPage:
        raise AssertionError("progress inspection must not fetch upstream data")

    monkeypatch.setattr(USAspendingClient, "search_awards_page", unexpected_source_call)
    monkeypatch.setattr(
        api_module,
        "build_http_client",
        lambda _: pytest.fail("Progress inspection must not create an upstream HTTP client"),
    )
    app = create_app(repository=InMemorySignalRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(MONTHLY_PATH, params=JANUARY_PARAMS)

    assert response.status_code == 200
    assert response.json()["status"] == "incomplete"
    assert response.json()["windows"] == []
    assert response.json()["start_month"] == "2026-01-01"
    assert response.json()["end_month"] == "2026-01-01"
    assert "not guaranteed source completeness" in response.json()["coverage_note"]


async def test_selected_months_collect_both_agencies_and_populate_existing_dashboard_feeds(
    monkeypatch, satellite_award: AwardRecord
) -> None:
    calls: list[tuple[str, date, date, int]] = []

    async def search(self, **kwargs: Any) -> AwardSearchPage:
        agency = kwargs["agency"]
        month = kwargs["start_date"]
        calls.append((agency, month, kwargs["end_date"], kwargs["page"]))
        return AwardSearchPage(
            awards=[historical_award(satellite_award, agency, month)],
            fetched_count=1,
            has_more=False,
        )

    monkeypatch.setattr(USAspendingClient, "search_awards_page", search)
    app = create_app(
        repository=InMemorySignalRepository(),
        collection_repository=InMemoryCollectionRepository(),
    )
    params = {"start_month": "2026-01", "end_month": "2026-02", "window_days": "31"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(MONTHLY_PATH, params=params)
        companies = await client.get("/api/v1/companies")
        signals = await client.get("/api/v1/signals")
        progress = await client.get(MONTHLY_PATH, params=params)

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert len(response.json()["windows"]) == 4
    assert set(calls) == {
        (agency, date(2026, 1, 1), date(2026, 1, 31), 1) for agency in DEFAULT_AGENCIES
    } | {(agency, date(2026, 2, 1), date(2026, 2, 28), 1) for agency in DEFAULT_AGENCIES}
    assert len(calls) == 4
    assert all(window["fetched_count"] == 1 for window in response.json()["windows"])
    assert all(window["stored_count"] == 1 for window in response.json()["windows"])
    assert companies.status_code == signals.status_code == progress.status_code == 200
    assert len(signals.json()) == 4
    assert len(companies.json()) == 1
    assert companies.json()[0]["company_name"] == "Historical Orbit LLC"
    assert companies.json()[0]["signal_count"] == 4
    assert progress.json()["windows"] == response.json()["windows"]


async def test_completed_collection_resumes_without_fetching_and_refresh_deduplicates(
    monkeypatch, satellite_award: AwardRecord
) -> None:
    calls: list[str] = []

    async def search(self, **kwargs: Any) -> AwardSearchPage:
        agency = kwargs["agency"]
        calls.append(agency)
        return AwardSearchPage(
            awards=[historical_award(satellite_award, agency, kwargs["start_date"])],
            fetched_count=1,
            has_more=False,
        )

    monkeypatch.setattr(USAspendingClient, "search_awards_page", search)
    app = create_app(repository=InMemorySignalRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(MONTHLY_PATH, params=JANUARY_PARAMS)
        first_signals = (await client.get("/api/v1/signals")).json()
        resumed = await client.post(MONTHLY_PATH, params=JANUARY_PARAMS)
        assert len(calls) == 2
        refreshed = await client.post(MONTHLY_PATH, params=JANUARY_PARAMS | {"refresh": "true"})
        refreshed_signals = (await client.get("/api/v1/signals")).json()

    assert first.status_code == resumed.status_code == refreshed.status_code == 200
    assert first.json()["status"] == resumed.json()["status"] == "completed"
    assert resumed.json()["skipped_windows"] == 2
    assert len(calls) == 4
    assert refreshed.json()["status"] == "completed"
    assert all(window["attempts"] == 2 for window in refreshed.json()["windows"])
    assert all(window["stored_count"] == 0 for window in refreshed.json()["windows"])
    assert all(window["duplicate_count"] == 1 for window in refreshed.json()["windows"])
    assert len(first_signals) == len(refreshed_signals) == 2
    assert {item["signal_id"]: item["detected_at"] for item in first_signals} == {
        item["signal_id"]: item["detected_at"] for item in refreshed_signals
    }


@pytest.mark.parametrize(
    "error", [SourceDataError("invalid payload"), httpx.ReadTimeout("timeout")]
)
async def test_partial_failure_is_reported_and_resume_retries_only_failed_agency(
    monkeypatch, satellite_award: AwardRecord, error: Exception
) -> None:
    calls: list[str] = []
    fail = True

    async def search(self, **kwargs: Any) -> AwardSearchPage:
        agency = kwargs["agency"]
        calls.append(agency)
        if fail and agency == DEFAULT_AGENCIES[1]:
            raise error
        return AwardSearchPage(
            awards=[historical_award(satellite_award, agency, kwargs["start_date"])],
            fetched_count=1,
            has_more=False,
        )

    monkeypatch.setattr(USAspendingClient, "search_awards_page", search)
    app = create_app(repository=InMemorySignalRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        failed = await client.post(MONTHLY_PATH, params=JANUARY_PARAMS)
        progress = await client.get(MONTHLY_PATH, params=JANUARY_PARAMS)
        partial_signals = await client.get("/api/v1/signals")
        assert calls == list(DEFAULT_AGENCIES)
        fail = False
        resumed = await client.post(MONTHLY_PATH, params=JANUARY_PARAMS)
        signals = await client.get("/api/v1/signals")

    assert failed.status_code == progress.status_code == resumed.status_code == 200
    assert failed.json()["status"] == progress.json()["status"] == "incomplete"
    assert {window["status"] for window in failed.json()["windows"]} == {"completed", "failed"}
    [failed_window] = [
        window for window in progress.json()["windows"] if window["status"] == "failed"
    ]
    assert failed_window["error_message"]
    assert len(partial_signals.json()) == 1
    assert calls == [*DEFAULT_AGENCIES, DEFAULT_AGENCIES[1]]
    assert resumed.json()["status"] == "completed"
    assert resumed.json()["skipped_windows"] == 1
    attempts = {window["agency"]: window["attempts"] for window in resumed.json()["windows"]}
    assert attempts == {DEFAULT_AGENCIES[0]: 1, DEFAULT_AGENCIES[1]: 2}
    assert len(signals.json()) == 2


@pytest.mark.parametrize(
    "params",
    [
        {"start_month": "2026-1", "end_month": "2026-02"},
        {"start_month": "2026-01-01", "end_month": "2026-02"},
        {"start_month": "2026-01", "end_month": "2026-13"},
        {"start_month": "2026-02", "end_month": "2026-01"},
        {"start_month": "2026-01"},
        {
            "start_month": date.today().strftime("%Y-%m"),
            "end_month": date.today().strftime("%Y-%m"),
        },
        {"start_month": "9999-12", "end_month": "9999-12"},
    ],
)
async def test_invalid_monthly_requests_are_rejected_before_source_access(
    monkeypatch, params: dict[str, str]
) -> None:
    calls = 0

    async def unexpected_source_call(self, **kwargs: Any) -> AwardSearchPage:
        nonlocal calls
        calls += 1
        raise AssertionError("invalid selection must not access the source")

    monkeypatch.setattr(USAspendingClient, "search_awards_page", unexpected_source_call)
    app = create_app(repository=InMemorySignalRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(MONTHLY_PATH, params=params)

    assert response.status_code == 422
    assert calls == 0


async def test_overlapping_monthly_request_returns_conflict_without_source_access(
    monkeypatch,
) -> None:
    async def unexpected_source_call(self, **kwargs: Any) -> AwardSearchPage:
        raise AssertionError("overlapping collection must not fetch")

    monkeypatch.setattr(USAspendingClient, "search_awards_page", unexpected_source_call)
    app = create_app(repository=InMemorySignalRepository())
    async with (
        app.state.collection_lock,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(MONTHLY_PATH, params=JANUARY_PARAMS)
        progress = await client.get(MONTHLY_PATH, params=JANUARY_PARAMS)

    assert response.status_code == 409
    assert progress.status_code == 200


async def test_default_collection_searches_three_day_windows_for_both_agencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, date, date, int]] = []

    async def search(self, **kwargs: Any) -> AwardSearchPage:
        calls.append((kwargs["agency"], kwargs["start_date"], kwargs["end_date"], kwargs["page"]))
        return AwardSearchPage(awards=[], fetched_count=0, has_more=False)

    monkeypatch.setattr(USAspendingClient, "search_awards_page", search)
    app = create_app(repository=InMemorySignalRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            MONTHLY_PATH, params={"start_month": "2026-09", "end_month": "2026-09"}
        )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "completed"
    assert result["window_days"] == 3
    assert result["agencies"] == list(DEFAULT_AGENCIES)
    assert len(result["windows"]) == len(calls) == 20
    assert set(calls) == {
        (agency, date(2026, 9, day), date(2026, 9, day + 2), 1)
        for day in range(1, 31, 3)
        for agency in DEFAULT_AGENCIES
    }
    assert all(window["pages_fetched"] == 1 for window in result["windows"])


async def test_single_day_nasa_selection_populates_feeds_and_filters_progress_by_agency(
    monkeypatch: pytest.MonkeyPatch, satellite_award: AwardRecord
) -> None:
    calls: list[tuple[str, date, date]] = []
    selected_day = date(2026, 9, 15)

    async def search(self, **kwargs: Any) -> AwardSearchPage:
        agency, start, end = kwargs["agency"], kwargs["start_date"], kwargs["end_date"]
        calls.append((agency, start, end))
        return AwardSearchPage(
            awards=[historical_award(satellite_award, agency, start)],
            fetched_count=1,
            has_more=False,
        )

    monkeypatch.setattr(USAspendingClient, "search_awards_page", search)
    app = create_app(repository=InMemorySignalRepository())
    params = {"start_date": "2026-09-15", "end_date": "2026-09-15", "agency": "nasa"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(MONTHLY_PATH, params=params)
        nasa_progress = await client.get(MONTHLY_PATH, params=params)
        dod_progress = await client.get(MONTHLY_PATH, params=params | {"agency": "dod"})
        all_progress = await client.get(MONTHLY_PATH, params=params | {"agency": "all"})
        signals = await client.get("/api/v1/signals")
        companies = await client.get("/api/v1/companies")

    assert response.status_code == nasa_progress.status_code == dod_progress.status_code == 200
    assert calls == [(DEFAULT_AGENCIES[0], selected_day, selected_day)]
    result = response.json()
    assert result["status"] == nasa_progress.json()["status"] == "completed"
    assert result["start_date"] == result["end_date"] == "2026-09-15"
    assert result["agencies"] == [DEFAULT_AGENCIES[0]]
    [window] = result["windows"]
    assert window["agency"] == DEFAULT_AGENCIES[0]
    assert window["start_date"] == window["end_date"] == "2026-09-15"
    assert dod_progress.json()["windows"] == []
    assert dod_progress.json()["status"] == all_progress.json()["status"] == "incomplete"
    assert {window["agency"] for window in all_progress.json()["windows"]} == {DEFAULT_AGENCIES[0]}
    assert len(signals.json()) == len(companies.json()) == 1
    assert signals.json()[0]["agency"] == DEFAULT_AGENCIES[0]


async def test_explicit_date_range_keeps_windows_inside_calendar_months(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[date, date]] = []

    async def search(self, **kwargs: Any) -> AwardSearchPage:
        assert kwargs["agency"] == DEFAULT_AGENCIES[1]
        calls.append((kwargs["start_date"], kwargs["end_date"]))
        return AwardSearchPage(awards=[], fetched_count=0, has_more=False)

    monkeypatch.setattr(USAspendingClient, "search_awards_page", search)
    app = create_app(repository=InMemorySignalRepository())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            MONTHLY_PATH,
            params={"start_date": "2026-01-30", "end_date": "2026-02-05", "agency": "dod"},
        )

    assert response.status_code == 200
    assert calls == [
        (date(2026, 1, 30), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 3)),
        (date(2026, 2, 4), date(2026, 2, 5)),
    ]
    assert response.json()["status"] == "completed"
    assert response.json()["start_month"] == "2026-01-01"
    assert response.json()["end_month"] == "2026-02-01"
    assert all(
        window["start_date"][:7] == window["end_date"][:7] == window["month_start"][:7]
        for window in response.json()["windows"]
    )


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"start_date": "2026-01-01"},
        {"end_date": "2026-01-02"},
        {"start_month": "2026-01", "end_date": "2026-01-02"},
        JANUARY_PARAMS | {"start_date": "2026-01-01", "end_date": "2026-01-02"},
        {"start_date": "2026-01-03", "end_date": "2026-01-01"},
        {"start_date": "2026-02-30", "end_date": "2026-03-01"},
        {"start_date": date.today().isoformat(), "end_date": date.today().isoformat()},
        JANUARY_PARAMS | {"window_days": "0"},
        JANUARY_PARAMS | {"window_days": "-1"},
        JANUARY_PARAMS | {"window_days": "1.5"},
        JANUARY_PARAMS | {"agency": "doe"},
    ],
)
async def test_invalid_collection_selection_is_rejected_before_checkpoint_or_source_access(
    method: str, params: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class UnusedCheckpoints(InMemoryCollectionRepository):
        async def list_windows(
            self, *, start_month: date, end_month: date
        ) -> list[CollectionWindow]:
            raise AssertionError("Invalid requests must not inspect checkpoints")

    async def unexpected_source_call(self, **kwargs: Any) -> AwardSearchPage:
        raise AssertionError("Invalid requests must not fetch upstream data")

    monkeypatch.setattr(USAspendingClient, "search_awards_page", unexpected_source_call)
    app = create_app(
        repository=InMemorySignalRepository(), collection_repository=UnusedCheckpoints()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.request(method, MONTHLY_PATH, params=params)

    assert response.status_code == 422


async def test_shared_collection_lock_conflict_returns_409_while_progress_remains_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_source_call(self, **kwargs: Any) -> AwardSearchPage:
        raise AssertionError("A collector holding the shared lock must prevent source requests")

    monkeypatch.setattr(USAspendingClient, "search_awards_page", unexpected_source_call)
    checkpoints = InMemoryCollectionRepository()
    app = create_app(repository=InMemorySignalRepository(), collection_repository=checkpoints)
    async with (
        checkpoints.collection_lock(),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(MONTHLY_PATH, params=JANUARY_PARAMS)
        progress = await client.get(MONTHLY_PATH, params=JANUARY_PARAMS)

    assert response.status_code == 409
    assert progress.status_code == 200
    assert progress.json()["status"] == "incomplete"


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_unavailable_collection_storage_returns_503_without_changing_existing_feeds(
    method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class UnavailableCheckpoints(InMemoryCollectionRepository):
        async def list_windows(
            self, *, start_month: date, end_month: date
        ) -> list[CollectionWindow]:
            raise SQLAlchemyError("private database diagnostic")

    async def unexpected_source_call(self, **kwargs: Any) -> AwardSearchPage:
        raise AssertionError("Unavailable checkpoints must prevent source requests")

    monkeypatch.setattr(USAspendingClient, "search_awards_page", unexpected_source_call)
    app = create_app(
        repository=InMemorySignalRepository(), collection_repository=UnavailableCheckpoints()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.request(method, MONTHLY_PATH, params=JANUARY_PARAMS)
        signals = await client.get("/api/v1/signals")
        health = await client.get("/health")

    assert response.status_code == 503
    assert "private database diagnostic" not in response.text
    assert signals.status_code == health.status_code == 200
    assert signals.json() == []
