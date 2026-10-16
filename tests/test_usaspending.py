import json
from datetime import date
from typing import Any

import httpx
import pytest

from orbital_signal.sources.usaspending import SourceDataError, USAspendingClient


def award_result(**overrides: Any) -> dict[str, Any]:
    return {
        "Award ID": "DISPLAY-ID",
        "generated_internal_id": "CONT_AWD_DISPLAY-ID_8000_-NONE-_-NONE-",
        "Recipient Name": "Example Space LLC",
        "Award Amount": 100_000,
        "Awarding Agency": "National Aeronautics and Space Administration",
        "Description": "Satellite development.",
        **overrides,
    }


async def test_client_maps_and_paginates_awards() -> None:
    requested_pages: list[int] = []
    requested_transaction_awards: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)

        if request.url.path == "/api/v2/transactions/":
            requested_transaction_awards.append(body["award_id"])

            assert body["page"] == 1
            assert body["limit"] == 1
            assert body["sort"] == "action_date"
            assert body["order"] == "desc"

            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "action_date": "2026-08-15",
                        }
                    ]
                },
            )

        page = body["page"]
        requested_pages.append(page)

        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "Award ID": f"AWARD-{page}",
                        "Recipient Name": "Example Space Company",
                        "Recipient UEI": "UEI123",
                        "Award Amount": 2_500_000,
                        "Start Date": "2026-09-01",
                        "End Date": "2027-09-01",
                        "Awarding Agency": "Department of Defense",
                        "Description": "Satellite payload prototype",
                        "generated_internal_id": f"CONT_AWD_{page}",
                    }
                ],
                "page_metadata": {"hasNext": page == 1},
            },
        )

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = USAspendingClient(
            http_client,
            base_url="https://api.usaspending.test",
        )
        awards = await client.search_awards(
            start_date=date(2026, 1, 1),
            end_date=date(2026, 8, 24),
            agencies=("National Aeronautics and Space Administration",),
        )

    assert requested_pages == [1, 2]

    assert requested_transaction_awards == [
        "CONT_AWD_1",
        "CONT_AWD_2",
    ]

    assert [award.source_award_id for award in awards] == [
        "CONT_AWD_1",
        "CONT_AWD_2",
    ]

    assert awards[0].recipient_name == "Example Space Company"
    assert awards[0].action_date == date(2026, 8, 15)
    assert awards[0].start_date == date(2026, 9, 1)
    assert awards[0].end_date == date(2027, 9, 1)
    assert str(awards[0].source_url).endswith("CONT_AWD_1")


async def test_client_rejects_invalid_date_range() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={}),
    )

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = USAspendingClient(
            http_client,
            base_url="https://api.usaspending.test",
        )

        try:
            await client.search_awards(
                start_date=date(2026, 8, 24),
                end_date=date(2026, 1, 1),
            )
        except ValueError as exc:
            assert str(exc) == "end_date must be on or after start_date"
        else:
            raise AssertionError("expected ValueError")


async def test_latest_action_date_requires_generated_internal_id() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("transaction request should not be sent")

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = USAspendingClient(
            http_client,
            base_url="https://api.usaspending.test",
        )

        result = await client._fetch_latest_action_date(
            generated_internal_id=None,
        )

    assert result is None


def test_search_payload_filters_by_action_date() -> None:
    payload = USAspendingClient._build_payload(
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 25),
        agencies=("National Aeronautics and Space Administration",),
        page=1,
        limit=100,
    )

    assert payload["filters"]["time_period"] == [
        {
            "start_date": "2026-08-01",
            "end_date": "2026-08-25",
            "date_type": "action_date",
        }
    ]


async def test_action_date_enrichment_rejects_invalid_concurrency() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={}),
    )

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = USAspendingClient(
            http_client,
            base_url="https://api.usaspending.test",
        )

        try:
            await client._enrich_action_dates([], concurrency=0)
        except ValueError as exc:
            assert str(exc) == "concurrency must be at least 1"
        else:
            raise AssertionError("expected ValueError")


async def test_client_retries_read_timeout() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1

        if attempts == 1:
            raise httpx.ReadTimeout(
                "timed out",
                request=request,
            )

        return httpx.Response(
            200,
            json={
                "results": [],
                "page_metadata": {"hasNext": False},
            },
        )

    transport = httpx.MockTransport(handler)

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = USAspendingClient(
            http_client,
            base_url="https://api.usaspending.test",
        )

        awards = await client.search_awards(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 25),
            agencies=("National Aeronautics and Space Administration",),
        )

    assert attempts == 2
    assert awards == []


async def test_client_preserves_distinct_awards_with_the_same_display_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/transactions/":
            return httpx.Response(200, json={"results": []})

        return httpx.Response(
            200,
            json={
                "results": [
                    award_result(generated_internal_id="CONT_AWD_DISPLAY-ID_8000_PARENT-1_8000"),
                    award_result(generated_internal_id="CONT_AWD_DISPLAY-ID_8000_PARENT-2_8000"),
                ],
                "page_metadata": {"hasNext": False},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = USAspendingClient(http_client, base_url="https://api.usaspending.test")
        awards = await client.search_awards(
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 25),
            agencies=("National Aeronautics and Space Administration",),
        )

    assert [award.source_award_id for award in awards] == [
        "CONT_AWD_DISPLAY-ID_8000_PARENT-1_8000",
        "CONT_AWD_DISPLAY-ID_8000_PARENT-2_8000",
    ]
    assert str(awards[0].source_url).endswith(awards[0].source_award_id)


@pytest.mark.parametrize("generated_id", [None, "", "  "])
def test_mapping_falls_back_to_display_id_when_generated_id_is_missing(
    generated_id: str | None,
) -> None:
    award = USAspendingClient._map_award(award_result(generated_internal_id=generated_id))

    assert award.source_award_id == "DISPLAY-ID"
    assert award.generated_internal_id in {None, ""}
    assert str(award.source_url).endswith("DISPLAY-ID")


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"results": None, "page_metadata": {"hasNext": False}},
        {"results": {}, "page_metadata": {"hasNext": False}},
        {"results": [None], "page_metadata": {"hasNext": False}},
        {"results": []},
        {"results": [], "page_metadata": None},
        {"results": [], "page_metadata": {}},
        {"results": [], "page_metadata": {"hasNext": "false"}},
    ],
)
async def test_client_rejects_malformed_search_responses(payload: Any) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = USAspendingClient(http_client, base_url="https://api.usaspending.test")

        with pytest.raises(SourceDataError):
            await client.search_awards(
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 25),
            )


@pytest.mark.parametrize(
    "overrides",
    [
        {"Award ID": None, "generated_internal_id": None},
        {"Award Amount": "not-a-number"},
        {"Award Amount": -1},
        {"Recipient Name": None},
        {"Start Date": "invalid-date"},
    ],
)
def test_mapping_wraps_invalid_source_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(SourceDataError, match="invalid award record"):
        USAspendingClient._map_award(award_result(**overrides))


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"results": None},
        {"results": [None]},
        {"results": [{"action_date": "invalid-date"}]},
        {"results": [{"action_date": 20260825}]},
    ],
)
async def test_client_rejects_malformed_transaction_responses(payload: Any) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = USAspendingClient(http_client, base_url="https://api.usaspending.test")

        with pytest.raises(SourceDataError):
            await client._fetch_latest_action_date(generated_internal_id="CONT_AWD_1")


async def test_client_rejects_invalid_json() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"not JSON"))

    async with httpx.AsyncClient(transport=transport) as http_client:
        client = USAspendingClient(http_client, base_url="https://api.usaspending.test")

        with pytest.raises(SourceDataError, match="invalid JSON"):
            await client.search_awards(
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 25),
            )

        with pytest.raises(SourceDataError, match="invalid JSON"):
            await client._fetch_latest_action_date(generated_internal_id="CONT_AWD_1")
