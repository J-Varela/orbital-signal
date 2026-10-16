"""Application services for converting source records into intelligence signals."""

import hashlib
from datetime import date
from typing import Protocol

from orbital_signal.domain import (
    AwardRecord,
    CompanySignal,
    IngestionResult,
    SignalReclassificationResult,
)
from orbital_signal.priority import assess_opportunity_priority
from orbital_signal.quality import assess_signal_quality
from orbital_signal.relevance import assess_space_relevance
from orbital_signal.repository import SignalRepository


class AwardSource(Protocol):
    async def search_awards(
        self,
        *,
        start_date: date,
        end_date: date,
    ) -> list[AwardRecord]: ...


def build_company_signal(
    award: AwardRecord,
    *,
    as_of: date,
) -> CompanySignal | None:
    """Build current intelligence for an award using today's rule set."""

    relevance = assess_space_relevance(award)
    if not relevance.is_space_relevant:
        return None

    stable_key = f"{award.source}:{award.source_award_id}".encode()
    signal_id = hashlib.sha256(stable_key).hexdigest()[:20]

    quality = assess_signal_quality(award)
    priority = assess_opportunity_priority(
        award,
        relevance_score=relevance.score,
        is_startup_candidate=quality.is_startup_candidate,
        as_of=as_of,
    )

    return CompanySignal(
        signal_id=signal_id,
        company_name=award.recipient_name,
        company_uei=award.recipient_uei,
        occurred_on=award.action_date or award.start_date,
        amount=award.amount,
        agency=award.awarding_agency,
        summary=award.description,
        relevance_score=relevance.score,
        matched_terms=relevance.matched_terms,
        reasons=relevance.reasons,
        priority_score=priority.score,
        priority_reasons=priority.reasons,
        organization_type=quality.organization_type,
        is_startup_candidate=quality.is_startup_candidate,
        quality_flags=quality.quality_flags,
        source=award.source,
        source_award_id=award.source_award_id,
        evidence_url=award.source_url,
    )


class AwardIngestionService:
    def __init__(
        self,
        *,
        source: AwardSource,
        repository: SignalRepository,
        source_name: str = "usaspending",
    ) -> None:
        self._source = source
        self._repository = repository
        self._source_name = source_name

    async def ingest(
        self,
        *,
        start_date: date,
        end_date: date,
    ) -> IngestionResult:
        if end_date < start_date:
            raise ValueError("end_date must be on or after start_date")

        run_id = await self._repository.start_ingestion(
            source=self._source_name,
            start_date=start_date,
            end_date=end_date,
        )

        try:
            awards = await self._source.search_awards(
                start_date=start_date,
                end_date=end_date,
            )
            relevant_count = 0
            stored_count = 0
            as_of = date.today()

            for award in awards:
                signal = build_company_signal(
                    award,
                    as_of=as_of,
                )
                if signal is None:
                    continue

                relevant_count += 1

                if await self._repository.upsert(
                    award,
                    signal,
                ):
                    stored_count += 1

            result = IngestionResult(
                source=self._source_name,
                fetched_count=len(awards),
                relevant_count=relevant_count,
                stored_count=stored_count,
                duplicate_count=(relevant_count - stored_count),
            )

            await self._repository.finish_ingestion(
                run_id,
                result,
            )
            return result

        except Exception as exc:
            error_message = str(exc) or exc.__class__.__name__

            await self._repository.fail_ingestion(
                run_id,
                error_message,
            )
            raise


class SignalReclassificationService:
    """Recompute stored signals from locally persisted award evidence."""

    def __init__(
        self,
        *,
        repository: SignalRepository,
    ) -> None:
        self._repository = repository

    async def reclassify(
        self,
        *,
        as_of: date,
    ) -> SignalReclassificationResult:
        records = await self._repository.list_reclassification_inputs()

        reclassified_count = 0
        removed_count = 0

        for record in records:
            award = record.award
            existing_signal = record.signal
            rebuilt_signal = build_company_signal(
                award,
                as_of=as_of,
            )

            if rebuilt_signal is None:
                if existing_signal is not None and await self._repository.delete_signal(
                    existing_signal.signal_id
                ):
                    removed_count += 1
                continue

            rebuilt_signal = rebuilt_signal.model_copy(
                update={
                    "detected_at": record.detected_at,
                    "signal_id": (
                        existing_signal.signal_id
                        if existing_signal is not None
                        else rebuilt_signal.signal_id
                    ),
                }
            )

            await self._repository.upsert(
                award,
                rebuilt_signal,
            )
            reclassified_count += 1

        return SignalReclassificationResult(
            scanned_count=len(records),
            reclassified_count=reclassified_count,
            removed_count=removed_count,
        )
