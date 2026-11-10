import re

import pytest
from fastapi.testclient import TestClient

from orbital_signal.api import create_app
from orbital_signal.config import Settings
from orbital_signal.repository import InMemorySignalRepository


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/ingestions/usaspending",
        "/api/v1/collections/monthly",
        "/api/v1/reclassifications/signals",
    ],
)
def test_read_only_mode_blocks_writes(monkeypatch, path):
    monkeypatch.setenv("ORBITAL_SIGNAL_API_WRITES_ENABLED", "false")

    app = create_app(repository=InMemorySignalRepository())

    with TestClient(app) as client:
        response = client.post(path)

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("false", False),
        ("true", True),
    ],
)
def test_capabilities_reflect_write_policy(
    monkeypatch,
    raw_value,
    expected,
):
    monkeypatch.setenv(
        "ORBITAL_SIGNAL_API_WRITES_ENABLED",
        raw_value,
    )

    app = create_app(repository=InMemorySignalRepository())

    with TestClient(app) as client:
        response = client.get("/api/v1/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "api_writes_enabled": expected,
    }


def test_dashboard_hides_admin_controls_by_default() -> None:
    client = TestClient(create_app(repository=InMemorySignalRepository()))

    response = client.get("/")

    assert response.status_code == 200

    html = response.text

    for tag, attribute in [
        ("a", 'data-view="coverage"'),
        ("button", 'id="open-import"'),
    ]:
        match = re.search(
            rf"<{tag}\b[^>]*{re.escape(attribute)}[^>]*>",
            html,
        )
        assert match is not None
        assert re.search(r"\bhidden(?:\s|>|=)", match.group())

    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "/api/v1/capabilities" in script.text


def test_api_writes_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "ORBITAL_SIGNAL_API_WRITES_ENABLED",
        raising=False,
    )

    settings = Settings(_env_file=None)

    assert settings.api_writes_enabled is False
