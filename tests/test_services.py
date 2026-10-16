from datetime import date

import pytest

from orbital_signal.domain import AwardRecord, CompanySignal, RelevanceAssessment
from orbital_signal.repository import InMemorySignalRepository
from orbital_signal.services import (
    AwardIngestionService,
    SignalReclassificationService,
    build_company_signal,
)


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
    [first_signal] = await repository.list()
    second = await service.ingest(start_date=date(2026, 1, 1), end_date=date(2026, 8, 24))

    assert first.stored_count == 1
    assert first.duplicate_count == 0
    assert second.stored_count == 0
    assert second.duplicate_count == 1
    assert await repository.count() == 1

    [signal] = await repository.list()
    assert signal.company_name == "Example Orbital Systems, Inc."
    assert signal.relevance_score == 10
    assert signal.is_startup_candidate is True
    assert signal.detected_at == first_signal.detected_at


async def test_repository_can_filter_to_startup_candidates(
    satellite_award: AwardRecord,
) -> None:
    academic_award = satellite_award.model_copy(
        update={
            "source_award_id": "ACADEMIC-001",
            "generated_internal_id": "CONT_AWD_ACADEMIC-001",
            "recipient_name": "California Institute of Technology",
        }
    )
    repository = InMemorySignalRepository()
    service = AwardIngestionService(
        source=StubAwardSource([satellite_award, academic_award]),
        repository=repository,
    )

    await service.ingest(start_date=date(2026, 1, 1), end_date=date(2026, 8, 25))

    assert await repository.count() == 2
    candidates = await repository.list(startup_candidates_only=True)
    assert [signal.company_name for signal in candidates] == ["Example Orbital Systems, Inc."]


async def test_established_contractor_is_not_startup_candidate() -> None:
    award = AwardRecord(
        source="usaspending",
        source_award_id="AMENTUM-001",
        recipient_name="Amentum Technology, Inc.",
        amount=6_000_000,
        awarding_agency="National Aeronautics and Space Administration",
        description="Aerospace testing and facilities operations.",
        action_date=date(2026, 8, 20),
        source_url="https://www.usaspending.gov/award/AMENTUM-001",
    )

    repository = InMemorySignalRepository()
    service = AwardIngestionService(
        source=StubAwardSource([award]),
        repository=repository,
    )

    await service.ingest(
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 25),
    )

    [signal] = await repository.list()

    assert signal.is_startup_candidate is False
    assert "established_contractor" in signal.quality_flags


async def test_reclassification_updates_stale_established_contractor() -> None:
    award = AwardRecord(
        source="usaspending",
        source_award_id="80ARC026FA012",
        recipient_name="AMENTUM TECHNOLOGY, INC.",
        recipient_uei="K8QPQLGTRM33",
        amount=6_012_605.65,
        awarding_agency="National Aeronautics and Space Administration",
        description=(
            "AEROSPACE TESTING AND FACILITIES OPERATIONS AND MAINTENANCE "
            "(ATOM-5) THE PRINCIPAL PURPOSE OF THIS CONTRACT IS TO ACQUIRE "
            "SUPPORT SERVICES FOR THE ARC GROUNDS-BASED AEROSPACE TEST FACILITIES."
        ),
        action_date=date(2026, 8, 20),
        source_url=(
            "https://www.usaspending.gov/award/CONT_AWD_80ARC026FA012_8000_80ARC022DA011_8000"
        ),
    )

    stale_signal = CompanySignal(
        signal_id="0aa9dba5c1bc62499ba5",
        company_name="AMENTUM TECHNOLOGY, INC.",
        company_uei="K8QPQLGTRM33",
        occurred_on=date(2026, 8, 20),
        amount=6_012_605.65,
        agency="National Aeronautics and Space Administration",
        summary=award.description,
        relevance_score=5,
        priority_score=61,
        priority_reasons=[
            "space relevance (+20)",
            "recent transaction within 30 days (+18)",
            "startup candidate (+15)",
            "award value at least $1M (+8)",
        ],
        matched_terms=["aerospace"],
        reasons=[
            ("space-focused awarding organization: national aeronautics and space administration"),
            "matched term 'aerospace' (+2)",
        ],
        organization_type="company",
        is_startup_candidate=True,
        quality_flags=[],
        source="usaspending",
        source_award_id="80ARC026FA012",
        evidence_url=award.source_url,
    )

    original_detected_at = stale_signal.detected_at

    repository = InMemorySignalRepository()
    await repository.upsert(award, stale_signal)

    service = SignalReclassificationService(
        repository=repository,
    )

    result = await service.reclassify(
        as_of=date(2026, 9, 16),
    )

    assert result.scanned_count == 1
    assert result.reclassified_count == 1
    assert result.removed_count == 0

    [updated] = await repository.list()

    assert updated.signal_id == stale_signal.signal_id
    assert updated.is_startup_candidate is False
    assert updated.quality_flags == ["established_contractor"]
    assert updated.priority_score == 46
    assert "startup candidate (+15)" not in updated.priority_reasons
    assert updated.detected_at == original_detected_at


async def test_reclassification_removes_signal_that_is_no_longer_relevant() -> None:
    award = AwardRecord(
        source="usaspending",
        source_award_id="STALE-001",
        recipient_name="Example Services, Inc.",
        amount=100_000,
        awarding_agency="Department of Agriculture",
        description="Office furniture purchase.",
        action_date=date(2026, 8, 20),
        source_url="https://www.usaspending.gov/award/STALE-001",
    )

    stale_signal = CompanySignal(
        signal_id="stale-signal",
        company_name="Example Services, Inc.",
        occurred_on=date(2026, 8, 20),
        amount=100_000,
        agency="Department of Agriculture",
        summary="Office furniture purchase.",
        relevance_score=4,
        priority_score=30,
        is_startup_candidate=True,
        source="usaspending",
        source_award_id="STALE-001",
        evidence_url=award.source_url,
    )

    repository = InMemorySignalRepository()
    await repository.upsert(award, stale_signal)

    service = SignalReclassificationService(
        repository=repository,
    )

    result = await service.reclassify(
        as_of=date(2026, 9, 16),
    )

    assert result.scanned_count == 1
    assert result.reclassified_count == 0
    assert result.removed_count == 1
    assert await repository.count() == 0
    assert repository._awards[stale_signal.signal_id] == award
    assert await repository.delete_signal(stale_signal.signal_id) is False
    assert repository._awards[stale_signal.signal_id] == award


@pytest.mark.parametrize("legacy_id", [None, "legacy-signal"])
async def test_reclassification_can_restore_removed_evidence(
    satellite_award: AwardRecord, monkeypatch, legacy_id: str | None
) -> None:
    repository = InMemorySignalRepository()
    signal = build_company_signal(satellite_award, as_of=date(2026, 9, 16))
    assert signal is not None
    original_signal_id = signal.signal_id
    if legacy_id is not None:
        signal = signal.model_copy(update={"signal_id": legacy_id})
    await repository.upsert(satellite_award, signal)
    service = SignalReclassificationService(repository=repository)

    with monkeypatch.context() as rules:
        rules.setattr(
            "orbital_signal.services.assess_space_relevance",
            lambda award: RelevanceAssessment(score=0, is_space_relevant=False),
        )
        removed = await service.reclassify(as_of=date(2026, 9, 16))

    assert removed.removed_count == 1
    assert await repository.count() == 0
    restored = await service.reclassify(as_of=date(2026, 9, 17))

    assert restored.scanned_count == 1
    assert restored.reclassified_count == 1
    [updated] = await repository.list()
    assert updated.signal_id == original_signal_id
    assert updated.detected_at == signal.detected_at
    repeated = await service.reclassify(as_of=date(2026, 9, 18))
    assert repeated.scanned_count == 1
    assert repeated.reclassified_count == 1


async def test_reclassification_preserves_existing_signal_identifier(
    satellite_award: AwardRecord,
) -> None:
    repository = InMemorySignalRepository()
    signal = build_company_signal(satellite_award, as_of=date(2026, 9, 16))
    assert signal is not None
    signal = signal.model_copy(update={"signal_id": "legacy-signal-id"})
    await repository.upsert(satellite_award, signal)

    await SignalReclassificationService(repository=repository).reclassify(as_of=date(2026, 9, 16))

    [updated] = await repository.list()
    assert updated.signal_id == "legacy-signal-id"


async def test_ingestion_validates_date_range_before_starting(
    satellite_award: AwardRecord,
) -> None:
    repository = InMemorySignalRepository()
    service = AwardIngestionService(
        source=StubAwardSource([satellite_award]), repository=repository
    )

    with pytest.raises(ValueError, match="end_date must be on or after start_date"):
        await service.ingest(start_date=date(2026, 9, 1), end_date=date(2026, 8, 1))

    assert await repository.count() == 0
