"""Signal persistence contracts and in-memory implementation."""

from typing import Protocol

from orbital_signal.domain import AwardRecord, CompanySignal


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
