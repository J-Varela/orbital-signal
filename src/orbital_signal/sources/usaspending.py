"""Typed adapter for the public USAspending award search API."""

import asyncio
from datetime import date
from typing import Any
from urllib.parse import quote

import httpx

from orbital_signal.domain import AwardRecord

SEARCH_PATH = "/api/v2/search/spending_by_award/"
TRANSACTIONS_PATH = "/api/v2/transactions/"
CONTRACT_AWARD_TYPE_CODES = ["A", "B", "C", "D"]
DEFAULT_AGENCIES = (
    "National Aeronautics and Space Administration",
    "Department of Defense",
)


class USAspendingClient:
    """Fetch paginated prime contract awards from USAspending."""

    def __init__(self, client: httpx.AsyncClient, *, base_url: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")

    async def search_awards(
        self,
        *,
        start_date: date,
        end_date: date,
        agencies: tuple[str, ...] = DEFAULT_AGENCIES,
        page_limit: int = 5,
        records_per_page: int = 100,
    ) -> list[AwardRecord]:
        if end_date < start_date:
            raise ValueError("end_date must be on or after start_date")
        if not 1 <= records_per_page <= 100:
            raise ValueError("records_per_page must be between 1 and 100")
        if page_limit < 1:
            raise ValueError("page_limit must be at least 1")

        awards_by_id: dict[str, AwardRecord] = {}
        # Search each agency independently so the much larger defense result set
        # cannot crowd NASA awards out of the bounded alpha ingestion window.
        for agency in agencies:
            for page in range(1, page_limit + 1):
                response = await self._client.post(
                    f"{self._base_url}{SEARCH_PATH}",
                    json=self._build_payload(
                        start_date=start_date,
                        end_date=end_date,
                        agencies=(agency,),
                        page=page,
                        limit=records_per_page,
                    ),
                )
                response.raise_for_status()
                payload = response.json()
                page_awards = [self._map_award(item) for item in payload.get("results", [])]

                enriched_awards = await self._enrich_action_dates(page_awards)

                for award in enriched_awards:
                    awards_by_id[award.source_award_id] = award

                page_metadata = payload.get("page_metadata", {})
                if not page_metadata.get("hasNext", False):
                    break

        return list(awards_by_id.values())

    @staticmethod
    def _build_payload(
        *,
        start_date: date,
        end_date: date,
        agencies: tuple[str, ...],
        page: int,
        limit: int,
    ) -> dict[str, Any]:
        return {
            "filters": {
                "time_period": [
                    {
                        "start_date": start_date.isoformat(),
                        "end_date": end_date.isoformat(),
                        "date_type": "action_date",
                    }
                ],
                "award_type_codes": CONTRACT_AWARD_TYPE_CODES,
                "agencies": [
                    {"type": "awarding", "tier": "toptier", "name": agency} for agency in agencies
                ],
            },
            "fields": [
                "Award ID",
                "Recipient Name",
                "Recipient UEI",
                "Award Amount",
                "Start Date",
                "End Date",
                "Awarding Agency",
                "Description",
                "generated_internal_id",
            ],
            "sort": "Start Date",
            "order": "desc",
            "page": page,
            "limit": limit,
        }

    @staticmethod
    def _map_award(item: dict[str, Any]) -> AwardRecord:
        generated_id = item.get("generated_internal_id")
        award_id = str(item.get("Award ID") or generated_id or "").strip()
        if not award_id:
            raise ValueError("USAspending result is missing an award identifier")

        recipient_name = str(item.get("Recipient Name") or "").strip()
        awarding_agency = str(item.get("Awarding Agency") or "").strip()
        evidence_id = generated_id or award_id
        evidence_url = f"https://www.usaspending.gov/award/{quote(str(evidence_id), safe='_-')}"

        return AwardRecord(
            source="usaspending",
            source_award_id=award_id,
            generated_internal_id=generated_id,
            recipient_name=recipient_name,
            recipient_uei=item.get("Recipient UEI") or None,
            amount=float(item.get("Award Amount") or 0),
            awarding_agency=awarding_agency,
            description=str(item.get("Description") or "").strip(),
            start_date=item.get("Start Date"),
            end_date=item.get("End Date"),
            source_url=evidence_url,
        )

    async def _fetch_latest_action_date(
        self,
        *,
        generated_internal_id: str | None,
    ) -> date | None:
        if not generated_internal_id:
            return None

        response = await self._client.post(
            f"{self._base_url}{TRANSACTIONS_PATH}",
            json={
                "award_id": generated_internal_id,
                "page": 1,
                "limit": 1,
                "sort": "action_date",
                "order": "desc",
            },
        )
        response.raise_for_status()

        results = response.json().get("results", [])
        if not results:
            return None

        value = results[0].get("action_date")
        return date.fromisoformat(value) if value else None

    async def _enrich_action_dates(
        self,
        awards: list[AwardRecord],
        *,
        concurrency: int = 10,
    ) -> list[AwardRecord]:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")

        semaphore = asyncio.Semaphore(concurrency)

        async def enrich(award: AwardRecord) -> AwardRecord:
            async with semaphore:
                action_date = await self._fetch_latest_action_date(
                    generated_internal_id=award.generated_internal_id,
                )

            return award.model_copy(
                update={"action_date": action_date},
            )

        return list(await asyncio.gather(*(enrich(award) for award in awards)))
