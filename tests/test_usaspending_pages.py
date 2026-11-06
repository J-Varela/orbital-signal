import json
from datetime import date
from typing import Any

import httpx
import pytest

from orbital_signal.sources.usaspending import (
    SEARCH_PATH,
    TRANSACTIONS_PATH,
    SourceDataError,
    USAspendingClient,
)

NASA = "National Aeronautics and Space Administration"


def award_result(identifier: str) -> dict[str, Any]:
    return {
        "Award ID": identifier,
        "generated_internal_id": identifier,
        "Recipient Name": "Example Space LLC",
        "Award Amount": 100_000,
        "Awarding Agency": NASA,
        "Description": "Satellite development.",
    }


async def test_page_reports_raw_row_count_and_upstream_continuation() -> None:
    search_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == TRANSACTIONS_PATH:
            assert body["award_id"] == "AWARD-1"
            return httpx.Response(200, json={"results": [{"action_date": "2026-01-15"}]})
        assert request.url.path == SEARCH_PATH
        search_bodies.append(body)
        return httpx.Response(
            200,
            json={
                "results": [award_result("AWARD-1"), award_result("AWARD-1")],
                "page_metadata": {"hasNext": True},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        source = USAspendingClient(http_client, base_url="https://api.usaspending.test")
        result = await source.search_awards_page(
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            agency=NASA,
            page=2,
            records_per_page=2,
        )

    assert result.fetched_count == 2
    assert result.has_more is True
    assert [award.source_award_id for award in result.awards] == ["AWARD-1", "AWARD-1"]
    assert all(award.action_date == date(2026, 1, 15) for award in result.awards)
    assert len(search_bodies) == 1
    body = search_bodies[0]
    assert body["page"] == 2
    assert body["limit"] == 2
    assert body["filters"]["agencies"] == [{"type": "awarding", "tier": "toptier", "name": NASA}]
    assert body["filters"]["time_period"] == [
        {"start_date": "2026-01-01", "end_date": "2026-01-31", "date_type": "action_date"}
    ]


async def test_successful_page_is_available_before_a_later_page_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TRANSACTIONS_PATH:
            return httpx.Response(200, json={"results": []})
        page = json.loads(request.content)["page"]
        if page == 2:
            return httpx.Response(400, json={"detail": "failed second page"})
        return httpx.Response(
            200,
            json={
                "results": [award_result("AWARD-1")],
                "page_metadata": {"hasNext": True},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        source = USAspendingClient(http_client, base_url="https://api.usaspending.test")
        first = await source.search_awards_page(
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31), agency=NASA
        )
        with pytest.raises(httpx.HTTPStatusError):
            await source.search_awards_page(
                start_date=date(2026, 1, 1), end_date=date(2026, 1, 31), agency=NASA, page=2
            )

    assert first.fetched_count == 1
    assert first.has_more is True
    assert first.awards[0].source_award_id == "AWARD-1"


@pytest.mark.parametrize("metadata", [None, {}, {"hasNext": "false"}])
async def test_page_requires_boolean_continuation_metadata(metadata: Any) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"results": [], "page_metadata": metadata})
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        source = USAspendingClient(http_client, base_url="https://api.usaspending.test")
        with pytest.raises(SourceDataError, match="invalid page metadata"):
            await source.search_awards_page(
                start_date=date(2026, 1, 1), end_date=date(2026, 1, 31), agency=NASA
            )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"start_date": date(2026, 2, 1)}, "end_date must be on or after start_date"),
        ({"agency": " "}, "agency must not be empty"),
        ({"page": 0}, "page must be at least 1"),
        ({"records_per_page": 0}, "records_per_page must be between 1 and 100"),
        ({"records_per_page": 101}, "records_per_page must be between 1 and 100"),
    ],
)
async def test_page_validates_inputs_before_network_access(
    overrides: dict[str, Any], message: str
) -> None:
    def unexpected_request(_: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid page must not issue requests")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as http_client:
        source = USAspendingClient(http_client, base_url="https://api.usaspending.test")
        parameters: dict[str, Any] = {
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 1, 31),
            "agency": NASA,
        }
        with pytest.raises(ValueError, match=message):
            await source.search_awards_page(**(parameters | overrides))


async def test_legacy_search_still_deduplicates_and_respects_its_page_cap() -> None:
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TRANSACTIONS_PATH:
            return httpx.Response(200, json={"results": []})
        requested_pages.append(json.loads(request.content)["page"])
        return httpx.Response(
            200,
            json={
                "results": [award_result("SAME-AWARD")],
                "page_metadata": {"hasNext": True},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        source = USAspendingClient(http_client, base_url="https://api.usaspending.test")
        awards = await source.search_awards(
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            agencies=(NASA,),
            page_limit=2,
        )

    assert requested_pages == [1, 2]
    assert [award.source_award_id for award in awards] == ["SAME-AWARD"]
