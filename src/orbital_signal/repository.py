"""Signal persistence contracts and in-memory implementation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import uuid4

from orbital_signal.domain import (
    AwardRecord,
    CompanyIntelligenceProfile,
    CompanySignal,
    IngestionResult,
)
from orbital_signal.profiles import build_company_profiles


@dataclass(frozen=True)
class SignalReclassificationInput:
    """Stored award evidence, including awards whose signal was removed."""

    award: AwardRecord
    signal: CompanySignal | None
    detected_at: datetime


class SignalRepository(Protocol):
    """Storage behavior required by ingestion and API services."""

    async def upsert(
        self,
        award: AwardRecord,
        signal: CompanySignal,
    ) -> bool:
        """Store an award graph and return True only when newly inserted."""

    async def list(
        self,
        *,
        minimum_score: int = 0,
        minimum_priority_score: int = 0,
        limit: int = 100,
        startup_candidates_only: bool = False,
    ) -> list[CompanySignal]:
        """Return matching signals in descending intelligence order."""

    async def list_company_profiles(
        self,
        *,
        limit: int = 100,
        startup_candidates_only: bool = False,
    ) -> list[CompanyIntelligenceProfile]:
        """Return aggregated company intelligence in priority order."""

    async def list_reclassification_inputs(
        self,
    ) -> list[SignalReclassificationInput]:
        """Return all retained evidence and any current derived signal."""

    async def delete_signal(self, signal_id: str) -> bool:
        """Delete a derived signal while preserving its source evidence."""

    async def count(self) -> int:
        """Return the number of stored signals."""

    async def start_ingestion(
        self,
        *,
        source: str,
        start_date: date,
        end_date: date,
    ) -> str:
        """Create a running ingestion record and return its identifier."""

    async def finish_ingestion(
        self,
        run_id: str,
        result: IngestionResult,
    ) -> None:
        """Mark an ingestion run completed with its final counts."""

    async def fail_ingestion(
        self,
        run_id: str,
        error_message: str,
    ) -> None:
        """Mark an ingestion run failed with diagnostic evidence."""


class InMemorySignalRepository:
    """Deterministic storage used by isolated unit and API tests."""

    def __init__(self) -> None:
        self._signals: dict[str, CompanySignal] = {}
        self._awards: dict[str, AwardRecord] = {}
        self._detected_at: dict[str, datetime] = {}

    async def upsert(
        self,
        award: AwardRecord,
        signal: CompanySignal,
    ) -> bool:
        """Store a signal and return True only when newly inserted."""

        existing_id = next(
            (
                signal_id
                for signal_id, stored_award in self._awards.items()
                if stored_award.source == award.source
                and (
                    stored_award.source_award_id == award.source_award_id
                    or (
                        award.generated_internal_id is not None
                        and stored_award.generated_internal_id == award.generated_internal_id
                    )
                )
            ),
            signal.signal_id,
        )
        inserted = existing_id not in self._signals
        signal_id = signal.signal_id if inserted else existing_id
        detected_at = self._detected_at.get(existing_id, signal.detected_at)

        # Evidence survives deletion. Move its key when restoration uses a new
        # canonical ID rather than retaining two copies of the same award.
        if existing_id != signal_id:
            self._awards.pop(existing_id, None)
            self._detected_at.pop(existing_id, None)

        self._signals[signal_id] = signal.model_copy(
            update={"signal_id": signal_id, "detected_at": detected_at}
        )
        self._awards[signal_id] = award
        self._detected_at[signal_id] = detected_at
        return inserted

    async def list(
        self,
        *,
        minimum_score: int = 0,
        minimum_priority_score: int = 0,
        limit: int = 100,
        startup_candidates_only: bool = False,
    ) -> list[CompanySignal]:
        matching = [
            signal
            for signal in self._signals.values()
            if signal.relevance_score >= minimum_score
            and signal.priority_score >= minimum_priority_score
            and (not startup_candidates_only or signal.is_startup_candidate)
        ]
        return sorted(
            matching,
            key=lambda signal: (
                signal.priority_score,
                signal.relevance_score,
                signal.amount,
            ),
            reverse=True,
        )[:limit]

    async def list_company_profiles(
        self,
        *,
        limit: int = 100,
        startup_candidates_only: bool = False,
    ) -> list[CompanyIntelligenceProfile]:
        profiles = build_company_profiles(self._signals.values())

        if startup_candidates_only:
            profiles = [profile for profile in profiles if profile.startup_candidate]

        return profiles[:limit]

    async def list_reclassification_inputs(
        self,
    ) -> list[SignalReclassificationInput]:
        return [
            SignalReclassificationInput(
                award=award,
                signal=self._signals.get(signal_id),
                detected_at=self._detected_at[signal_id],
            )
            for signal_id, award in self._awards.items()
        ]

    async def delete_signal(self, signal_id: str) -> bool:
        existed = signal_id in self._signals

        self._signals.pop(signal_id, None)

        return existed

    async def count(self) -> int:
        return len(self._signals)

    async def start_ingestion(
        self,
        *,
        source: str,
        start_date: date,
        end_date: date,
    ) -> str:
        del source, start_date, end_date
        return str(uuid4())

    async def finish_ingestion(
        self,
        run_id: str,
        result: IngestionResult,
    ) -> None:
        del run_id, result

    async def fail_ingestion(
        self,
        run_id: str,
        error_message: str,
    ) -> None:
        del run_id, error_message
