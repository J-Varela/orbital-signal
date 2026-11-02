from datetime import date
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from orbital_signal.api import create_app
from orbital_signal.domain import AwardRecord
from orbital_signal.repository import InMemorySignalRepository
from orbital_signal.services import build_company_signal


class DashboardAssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script" and (source := attributes.get("src")):
            self.assets.append(source)
        if (
            tag == "link"
            and attributes.get("rel") in {"stylesheet", "icon"}
            and (href := attributes.get("href"))
        ):
            self.assets.append(href)


def test_dashboard_serves_html_and_its_local_assets() -> None:
    with TestClient(create_app(repository=InMemorySignalRepository())) as client:
        response = client.get("/")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["cache-control"] == "no-cache"
        assert "Orbital Signal" in response.text

        parser = DashboardAssetParser()
        parser.feed(response.text)
        assert parser.assets
        for asset in parser.assets:
            assert asset.startswith("/static/")
            asset_response = client.get(asset)
            assert asset_response.status_code == 200, asset
            assert asset_response.content, asset


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/static/app.js", "javascript"),
        ("/static/styles.css", "text/css"),
        ("/static/favicon.svg", "image/svg+xml"),
    ],
)
def test_dashboard_asset_content_types(path: str, content_type: str) -> None:
    with TestClient(create_app(repository=InMemorySignalRepository())) as client:
        response = client.get(path)

    assert response.status_code == 200
    assert content_type in response.headers["content-type"]


def test_swagger_and_existing_api_schema_remain_available() -> None:
    with TestClient(create_app(repository=InMemorySignalRepository())) as client:
        docs = client.get("/docs")
        schema = client.get("/openapi.json")

    assert docs.status_code == 200
    assert "SwaggerUIBundle" in docs.text
    assert "/openapi.json" in docs.text
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    assert "/" not in paths
    assert {
        "/health",
        "/api/v1/companies",
        "/api/v1/signals",
        "/api/v1/ingestions/usaspending",
        "/api/v1/reclassifications/signals",
    } <= paths.keys()


@pytest.mark.parametrize("path", ["/static/missing.js", "/api/v1/missing"])
def test_dashboard_does_not_hide_missing_assets_or_api_routes(path: str) -> None:
    with TestClient(create_app(repository=InMemorySignalRepository())) as client:
        response = client.get(path)

    assert response.status_code == 404


async def test_dashboard_feeds_reflect_repository_changes(satellite_award: AwardRecord) -> None:
    repository = InMemorySignalRepository()
    award = satellite_award.model_copy(update={"recipient_name": "Mosaic Space LLC"})
    signal = build_company_signal(award, as_of=date(2026, 9, 16))
    assert signal is not None
    params = {"startup_candidates_only": "false", "limit": "500"}

    with TestClient(create_app(repository=repository)) as client:
        assert client.get("/api/v1/companies", params=params).json() == []
        assert client.get("/api/v1/signals", params=params).json() == []

        await repository.upsert(award, signal)
        companies = client.get("/api/v1/companies", params=params)
        signals = client.get("/api/v1/signals", params=params)

        assert companies.status_code == signals.status_code == 200
        assert len(companies.json()) == len(signals.json()) == 1
        assert companies.json()[0]["company_name"] == "Mosaic Space LLC"
        assert companies.json()[0]["total_amount"] == award.amount
        assert signals.json()[0]["signal_id"] == signal.signal_id
        assert signals.json()[0]["amount"] == award.amount
        assert signals.json()[0]["evidence_url"] == str(award.source_url)

        updated_amount = award.amount + 100_000
        await repository.upsert(
            award.model_copy(update={"amount": updated_amount}),
            signal.model_copy(update={"amount": updated_amount}),
        )

        assert (
            client.get("/api/v1/companies", params=params).json()[0]["total_amount"]
            == updated_amount
        )
        assert client.get("/api/v1/signals", params=params).json()[0]["amount"] == updated_amount
