from datetime import date
from pathlib import Path

import httpx

from orbital_signal.collection import MonthlyCollectionService
from orbital_signal.collection_repository import InMemoryCollectionRepository
from orbital_signal.domain import AwardRecord
from orbital_signal.repository import InMemorySignalRepository
from orbital_signal.services import build_company_signal
from orbital_signal.sources.usaspending import DEFAULT_AGENCIES, USAspendingClient


async def test_new_source_resumes_only_failed_enrichment_without_repeating_award_search(
    tmp_path: Path, satellite_award: AwardRecord
) -> None:
    nasa = DEFAULT_AGENCIES[0]
    day = date(2026, 9, 23)
    fail = True
    searches = 0
    lookups: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal searches
        import json

        if request.url.path == "/api/v2/transactions/":
            identifier = json.loads(request.content)["award_id"]
            lookups.append(identifier)
            if fail and identifier == "CONT_AWD_RECOVERY-B":
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            return httpx.Response(200, json={"results": [{"action_date": "2026-09-23"}]})
        searches += 1
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "Award ID": name,
                        "generated_internal_id": f"CONT_AWD_{name}",
                        "Recipient Name": f"Satellite Recovery {name} LLC",
                        "Award Amount": 500000,
                        "Awarding Agency": nasa,
                        "Description": "Satellite payload spacecraft prototype",
                    }
                    for name in ["RECOVERY-A", "RECOVERY-B"]
                ],
                "page_metadata": {"hasNext": False},
            },
        )

    repository = InMemorySignalRepository()
    existing = build_company_signal(satellite_award, as_of=day)
    assert existing is not None
    await repository.upsert(satellite_award, existing)
    checkpoints = InMemoryCollectionRepository()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        first = await MonthlyCollectionService(
            source=USAspendingClient(
                client,
                base_url="https://source.test",
                enrichment_concurrency=1,
                retry_attempts=1,
                enrichment_cache_directory=tmp_path,
            ),
            repository=repository,
            checkpoints=checkpoints,
            max_consecutive_failures=1,
        ).collect(start_date=day, end_date=day, agencies=(nasa,))
    assert first.stopped_early and first.status == "incomplete"
    assert first.windows[0].status == "failed"
    assert first.windows[0].fetched_count == 2
    assert first.windows[0].stored_count == 0
    assert await repository.count() == 1
    assert len(list(tmp_path.glob("*.json"))) == 1

    # New HTTP/source/service instances model a later CLI invocation; cache state is on disk.
    fail = False
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        resumed = await MonthlyCollectionService(
            source=USAspendingClient(
                client,
                base_url="https://source.test",
                enrichment_concurrency=1,
                retry_attempts=1,
                enrichment_cache_directory=tmp_path,
            ),
            repository=repository,
            checkpoints=checkpoints,
        ).collect(start_date=day, end_date=day, agencies=(nasa,))
    assert resumed.status == "completed" and not resumed.stopped_early
    assert resumed.cached_pages_reused == 1
    assert searches == 1
    assert lookups == ["CONT_AWD_RECOVERY-A", "CONT_AWD_RECOVERY-B", "CONT_AWD_RECOVERY-B"]
    assert not list(tmp_path.glob("*.json"))
    assert await repository.count() == 3
    assert resumed.cumulative.attempts == 2
    assert (
        resumed.cumulative.fetched_count == 4
    )  # Search-row observations, including retained replay.
    assert resumed.cumulative.stored_count == 2
    assert (
        next(signal for signal in await repository.list() if signal.signal_id == existing.signal_id)
        == existing
    )
