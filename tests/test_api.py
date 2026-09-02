from fastapi.testclient import TestClient

from orbital_signal.api import create_app
from orbital_signal.repository import InMemorySignalRepository


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
