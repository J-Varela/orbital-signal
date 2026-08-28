"""Application services for converting source records into intelligence signals."""

import hashlib
from datetime import date
from typing import Protocol

from orbital_signal.domain import AwardRecord, CompanySignal, IngestionResult
from orbital_signal.quality import assess_signal_quality
from orbital_signal.relevance import assess_space_relevance
from orbital_signal.repository import SignalRepository


class AwardSource(Protocol):
    async def search_awards(self, *, start_date: date, end_date: date) -> list[AwardRecord]: ...


class AwardIngestionService:
    def __init__(
        self,
        *,
        source: AwardSource,
        repository: SignalRepository,
    ) -> None:
        self._source = source
        self._repository = repository

    async def ingest(self, *, start_date: date, end_date: date) -> IngestionResult:
        awards = await self._source.search_awards(start_date=start_date, end_date=end_date)
        relevant_count = 0
        stored_count = 0

        for award in awards:
            assessment = assess_space_relevance(award)
            if not assessment.is_space_relevant:
                continue

            relevant_count += 1
            signal = self._to_signal(
                award,
                assessment.score,
                assessment.matched_terms,
                assessment.reasons,
            )
            if await self._repository.upsert(award, signal):
                stored_count += 1

        return IngestionResult(
            source="usaspending",
            fetched_count=len(awards),
            relevant_count=relevant_count,
            stored_count=stored_count,
            duplicate_count=relevant_count - stored_count,
        )

    @staticmethod
    def _to_signal(
        award: AwardRecord,
        score: int,
        matched_terms: list[str],
        reasons: list[str],
    ) -> CompanySignal:
        stable_key = f"{award.source}:{award.source_award_id}".encode()
        signal_id = hashlib.sha256(stable_key).hexdigest()[:20]
        quality = assess_signal_quality(award)
        return CompanySignal(
            signal_id=signal_id,
            company_name=award.recipient_name,
            company_uei=award.recipient_uei,
            occurred_on=award.start_date,
            amount=award.amount,
            agency=award.awarding_agency,
            summary=award.description,
            relevance_score=score,
            matched_terms=matched_terms,
            reasons=reasons,
            organization_type=quality.organization_type,
            is_startup_candidate=quality.is_startup_candidate,
            quality_flags=quality.quality_flags,
            source=award.source,
            source_award_id=award.source_award_id,
            evidence_url=award.source_url,
        )
