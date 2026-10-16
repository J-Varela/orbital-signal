from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from orbital_signal.api import create_app
from orbital_signal.domain import AwardRecord, CompanySignal
from orbital_signal.repository import InMemorySignalRepository
from orbital_signal.services import build_company_signal
from orbital_signal.sources.usaspending import SourceDataError, USAspendingClient


class RecordingRepository(InMemorySignalRepository):
    def __init__(self) -> None:
        super().__init__()
        self.list_kwargs: dict[str, object] = {}

    async def list(
        self,
        *,
        minimum_score: int = 0,
        minimum_priority_score: int = 0,
        limit: int = 100,
        startup_candidates_only: bool = False,
    ) -> list[CompanySignal]:
        self.list_kwargs = {
            "minimum_score": minimum_score,
            "minimum_priority_score": minimum_priority_score,
            "limit": limit,
            "startup_candidates_only": startup_candidates_only,
        }
        return []


def test_health_reports_release_version() -> None:
    client = TestClient(create_app(repository=InMemorySignalRepository()))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0-alpha.2"}


def test_signals_start_empty() -> None:
    client = TestClient(create_app(repository=InMemorySignalRepository()))

    response = client.get("/api/v1/signals")

    assert response.status_code == 200
    assert response.json() == []


def test_ingestion_rejects_reversed_date_range() -> None:
    client = TestClient(create_app(repository=InMemorySignalRepository()))

    response = client.post(
        "/api/v1/ingestions/usaspending",
        params={"start_date": "2026-08-24", "end_date": "2026-01-01"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "end_date must be on or after start_date"


def test_signals_accept_priority_filter() -> None:
    repository = RecordingRepository()
    client = TestClient(create_app(repository=repository))

    response = client.get(
        "/api/v1/signals",
        params={
            "minimum_score": 4,
            "minimum_priority_score": 70,
            "limit": 10,
            "startup_candidates_only": True,
        },
    )

    assert response.status_code == 200
    assert repository.list_kwargs == {
        "minimum_score": 4,
        "minimum_priority_score": 70,
        "limit": 10,
        "startup_candidates_only": True,
    }


def test_companies_start_empty() -> None:
    client = TestClient(create_app(repository=InMemorySignalRepository()))

    response = client.get("/api/v1/companies")

    assert response.status_code == 200
    assert response.json() == []


def test_companies_accept_profile_filters() -> None:
    repository = InMemorySignalRepository()
    client = TestClient(create_app(repository=repository))

    response = client.get(
        "/api/v1/companies",
        params={
            "limit": 10,
            "startup_candidates_only": True,
        },
    )

    assert response.status_code == 200
    assert response.json() == []


def test_reclassification_endpoint_recomputes_stored_signals() -> None:
    repository = InMemorySignalRepository()
    client = TestClient(create_app(repository=repository))

    response = client.post(
        "/api/v1/reclassifications/signals",
        params={"as_of": "2026-09-16"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "scanned_count": 0,
        "reclassified_count": 0,
        "removed_count": 0,
    }


@pytest.mark.parametrize(
    "error",
    [SourceDataError("invalid upstream payload"), httpx.ReadTimeout("timed out")],
)
def test_ingestion_reports_upstream_failures_as_bad_gateway(monkeypatch, error) -> None:
    async def fail_search(self, **kwargs):
        raise error

    monkeypatch.setattr(USAspendingClient, "search_awards", fail_search)
    with TestClient(create_app(repository=InMemorySignalRepository())) as client:
        response = client.post(
            "/api/v1/ingestions/usaspending",
            params={"start_date": "2026-08-01", "end_date": "2026-08-31"},
        )

    assert response.status_code == 502
    assert response.json() == {"detail": "USAspending request failed"}


async def test_reclassification_endpoint_updates_candidate_feed(
    satellite_award: AwardRecord,
) -> None:
    award = satellite_award.model_copy(update={"recipient_name": "Mosaic Space LLC"})
    signal = build_company_signal(award, as_of=date(2026, 9, 16))
    assert signal is not None
    stale_signal = signal.model_copy(
        update={"is_startup_candidate": False, "quality_flags": ["established_contractor"]}
    )
    repository = InMemorySignalRepository()
    await repository.upsert(award, stale_signal)

    with TestClient(create_app(repository=repository)) as client:
        response = client.post("/api/v1/reclassifications/signals", params={"as_of": "2026-09-16"})
        candidates = client.get("/api/v1/signals", params={"startup_candidates_only": True})

    assert response.status_code == 200
    assert response.json() == {
        "scanned_count": 1,
        "reclassified_count": 1,
        "removed_count": 0,
    }
    assert [item["signal_id"] for item in candidates.json()] == [signal.signal_id]
