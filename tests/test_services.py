from datetime import date

from orbital_signal.domain import AwardRecord
from orbital_signal.repository import InMemorySignalRepository
from orbital_signal.services import AwardIngestionService


class StubAwardSource:
    def __init__(self, awards: list[AwardRecord]) -> None:
        self._awards = awards

    async def search_awards(self, *, start_date: date, end_date: date) -> list[AwardRecord]:
        return self._awards


async def test_ingestion_stores_relevant_awards_once(satellite_award: AwardRecord) -> None:
    repository = InMemorySignalRepository()
    service = AwardIngestionService(
        source=StubAwardSource([satellite_award]),
        repository=repository,
    )

    first = await service.ingest(start_date=date(2026, 1, 1), end_date=date(2026, 8, 24))
    second = await service.ingest(start_date=date(2026, 1, 1), end_date=date(2026, 8, 24))

    assert first.stored_count == 1
    assert first.duplicate_count == 0
    assert second.stored_count == 0
    assert second.duplicate_count == 1
    assert repository.count() == 1

    [signal] = repository.list()
    assert signal.company_name == "Example Orbital Systems, Inc."
    assert signal.relevance_score == 12
    assert signal.is_startup_candidate is True


async def test_repository_can_filter_to_startup_candidates(
    satellite_award: AwardRecord,
) -> None:
    academic_award = satellite_award.model_copy(
        update={
            "source_award_id": "ACADEMIC-001",
            "recipient_name": "California Institute of Technology",
        }
    )
    repository = InMemorySignalRepository()
    service = AwardIngestionService(
        source=StubAwardSource([satellite_award, academic_award]),
        repository=repository,
    )

    await service.ingest(start_date=date(2026, 1, 1), end_date=date(2026, 8, 25))

    assert repository.count() == 2
    candidates = repository.list(startup_candidates_only=True)
    assert [signal.company_name for signal in candidates] == ["Example Orbital Systems, Inc."]
