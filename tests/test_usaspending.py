import json
from datetime import date

import httpx

from orbital_signal.sources.usaspending import USAspendingClient


async def test_client_maps_and_paginates_awards() -> None:
    requested_pages: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
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
                        "Action Date": "2026-08-15",
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
        client = USAspendingClient(http_client, base_url="https://api.usaspending.test")
        awards = await client.search_awards(
            start_date=date(2026, 1, 1),
            end_date=date(2026, 8, 24),
            agencies=("National Aeronautics and Space Administration",),
        )

    assert requested_pages == [1, 2]
    assert [award.source_award_id for award in awards] == ["AWARD-1", "AWARD-2"]
    assert awards[0].recipient_name == "Example Space Company"
    assert awards[0].action_date == date(2026, 8, 15)
    assert awards[0].start_date == date(2026, 9, 1)
    assert awards[0].end_date == date(2027, 9, 1)
    assert str(awards[0].source_url).endswith("CONT_AWD_1")


async def test_client_rejects_invalid_date_range() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={}))
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = USAspendingClient(http_client, base_url="https://api.usaspending.test")

        try:
            await client.search_awards(
                start_date=date(2026, 8, 24),
                end_date=date(2026, 1, 1),
            )
        except ValueError as exc:
            assert str(exc) == "end_date must be on or after start_date"
        else:
            raise AssertionError("expected ValueError")


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
