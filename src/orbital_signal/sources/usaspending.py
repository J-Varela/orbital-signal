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


class SourceDataError(Exception):
    """USAspending returned a response that does not match the expected data format."""


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
                response = await self._post_with_retry(
                    SEARCH_PATH,
                    json=self._build_payload(
                        start_date=start_date,
                        end_date=end_date,
                        agencies=(agency,),
                        page=page,
                        limit=records_per_page,
                    ),
                )
                response.raise_for_status()
                payload = self._parse_response(response)
                page_metadata = payload.get("page_metadata")
                if not isinstance(page_metadata, dict) or not isinstance(
                    page_metadata.get("hasNext"), bool
                ):
                    raise SourceDataError("USAspending search response has invalid page metadata")

                page_awards = [self._map_award(item) for item in payload["results"]]

                enriched_awards = await self._enrich_action_dates(page_awards)

                for award in enriched_awards:
                    awards_by_id[award.source_award_id] = award

                if not page_metadata["hasNext"]:
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
        if not isinstance(item, dict):
            raise SourceDataError("USAspending award result must be a JSON object")

        try:
            generated_id = item.get("generated_internal_id")
            if isinstance(generated_id, str):
                generated_id = generated_id.strip() or None

            # Display award identifiers can repeat across agencies and parent contracts.
            # The generated identifier includes those components and is globally stable.
            award_id = str(generated_id or item.get("Award ID") or "").strip()
            if not award_id:
                raise ValueError("USAspending result is missing an award identifier")

            evidence_url = f"https://www.usaspending.gov/award/{quote(award_id, safe='_-')}"

            return AwardRecord(
                source="usaspending",
                source_award_id=award_id,
                generated_internal_id=generated_id,
                recipient_name=item.get("Recipient Name") or "",
                recipient_uei=item.get("Recipient UEI") or None,
                amount=float(item.get("Award Amount") or 0),
                awarding_agency=item.get("Awarding Agency") or "",
                description=item.get("Description") or "",
                start_date=item.get("Start Date"),
                end_date=item.get("End Date"),
                source_url=evidence_url,
            )
        except (TypeError, ValueError) as exc:
            raise SourceDataError("USAspending returned an invalid award record") from exc

    @staticmethod
    def _parse_response(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceDataError("USAspending returned invalid JSON") from exc

        if not isinstance(payload, dict):
            raise SourceDataError("USAspending response must be a JSON object")

        results = payload.get("results")
        if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
            raise SourceDataError("USAspending response results must be a list of JSON objects")

        return payload

    async def _fetch_latest_action_date(
        self,
        *,
        generated_internal_id: str | None,
    ) -> date | None:
        if not generated_internal_id:
            return None

        response = await self._post_with_retry(
            TRANSACTIONS_PATH,
            json={
                "award_id": generated_internal_id,
                "page": 1,
                "limit": 1,
                "sort": "action_date",
                "order": "desc",
            },
        )
        response.raise_for_status()

        results = self._parse_response(response)["results"]
        if not results:
            return None

        value = results[0].get("action_date")
        if value is None:
            return None

        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise SourceDataError(
                "USAspending returned an invalid transaction action date"
            ) from exc

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

    async def _post_with_retry(
        self,
        path: str,
        *,
        json: dict[str, Any],
        attempts: int = 3,
    ) -> httpx.Response:
        last_error: httpx.HTTPError | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.post(
                    f"{self._base_url}{path}",
                    json=json,
                )
                response.raise_for_status()
                return response

            except (httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
                last_error = exc

            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {429, 500, 502, 503, 504}:
                    raise
                last_error = exc

            if attempt < attempts:
                await asyncio.sleep(2 ** (attempt - 1))

        assert last_error is not None
        raise last_error
