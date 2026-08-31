"""Signal persistence contracts and in-memory implementation."""

from datetime import date
from typing import Protocol
from uuid import uuid4

from orbital_signal.domain import (
    AwardRecord,
    CompanySignal,
    IngestionResult,
)


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
        limit: int = 100,
        startup_candidates_only: bool = False,
    ) -> list[CompanySignal]:
        """Return matching signals in descending intelligence order."""

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

    async def upsert(
        self,
        _award: AwardRecord,
        signal: CompanySignal,
    ) -> bool:
        """Store a signal and return True only when newly inserted."""

        inserted = signal.signal_id not in self._signals
        self._signals[signal.signal_id] = signal
        return inserted

    async def list(
        self,
        *,
        minimum_score: int = 0,
        limit: int = 100,
        startup_candidates_only: bool = False,
    ) -> list[CompanySignal]:
        matching = [
            signal
            for signal in self._signals.values()
            if signal.relevance_score >= minimum_score
            and (not startup_candidates_only or signal.is_startup_candidate)
        ]
        return sorted(
            matching,
            key=lambda signal: (
                signal.relevance_score,
                signal.amount,
            ),
            reverse=True,
        )[:limit]

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
