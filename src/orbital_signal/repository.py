"""Signal persistence abstractions."""

from orbital_signal.domain import CompanySignal


class InMemorySignalRepository:
    """Deterministic alpha storage, replaced by PostgreSQL in the next release."""

    def __init__(self) -> None:
        self._signals: dict[str, CompanySignal] = {}

    def upsert(self, signal: CompanySignal) -> bool:
        """Store a signal and return True only when it is newly inserted."""

        inserted = signal.signal_id not in self._signals
        self._signals[signal.signal_id] = signal
        return inserted

    def list(
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
            key=lambda signal: (signal.relevance_score, signal.amount),
            reverse=True,
        )[:limit]

    def count(self) -> int:
        return len(self._signals)
