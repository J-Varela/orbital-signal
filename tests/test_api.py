from fastapi.testclient import TestClient

from orbital_signal.api import create_app
from orbital_signal.domain import CompanySignal
from orbital_signal.repository import InMemorySignalRepository


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
