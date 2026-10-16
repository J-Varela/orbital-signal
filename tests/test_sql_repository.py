from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from orbital_signal.database import (
    AsyncSessionFactory,
    build_async_engine,
    build_session_factory,
)
from orbital_signal.db_models import (
    Award,
    Base,
    Company,
    CompanyAlias,
    IngestionRun,
    Signal,
)
from orbital_signal.domain import (
    AwardRecord,
    CompanySignal,
    IngestionResult,
    OrganizationType,
)
from orbital_signal.services import SignalReclassificationService
from orbital_signal.sql_repository import SqlAlchemySignalRepository


def make_award(
    *,
    award_id: str,
    company_name: str,
    uei: str | None,
    amount: float,
) -> AwardRecord:
    return AwardRecord(
        source="usaspending",
        source_award_id=award_id,
        generated_internal_id=f"CONT_AWD_{award_id}",
        recipient_name=company_name,
        recipient_uei=uei,
        amount=amount,
        awarding_agency=("National Aeronautics and Space Administration"),
        description="Lunar spacecraft prototype",
        action_date=date(2026, 8, 15),
        start_date=date(2026, 9, 1),
        end_date=date(2027, 9, 1),
        naics_code="336414",
        psc_code="AR11",
        source_url=(f"https://www.usaspending.gov/award/CONT_AWD_{award_id}"),
    )


def make_signal(
    award: AwardRecord,
    *,
    signal_id: str,
    score: int,
    candidate: bool,
    priority_score: int,
) -> CompanySignal:
    return CompanySignal(
        signal_id=signal_id,
        company_name=award.recipient_name,
        company_uei=award.recipient_uei,
        occurred_on=award.action_date or award.start_date,
        amount=award.amount,
        agency=award.awarding_agency,
        summary=award.description,
        relevance_score=score,
        matched_terms=["lunar", "spacecraft"],
        reasons=["matched space evidence"],
        priority_score=priority_score,
        priority_reasons=["test priority"],
        organization_type=OrganizationType.COMPANY,
        is_startup_candidate=candidate,
        quality_flags=[] if candidate else ["excluded"],
        source=award.source,
        source_award_id=award.source_award_id,
        evidence_url=award.source_url,
    )


@pytest.fixture
async def persisted_repository() -> AsyncIterator[
    tuple[SqlAlchemySignalRepository, AsyncSessionFactory]
]:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = build_session_factory(engine)
        yield SqlAlchemySignalRepository(session_factory), session_factory
    finally:
        await engine.dispose()


async def store_test_award(
    repository: SqlAlchemySignalRepository,
    *,
    award_id: str,
    company_name: str,
    uei: str | None,
    source: str = "usaspending",
) -> None:
    award = make_award(
        award_id=award_id,
        company_name=company_name,
        uei=uei,
        amount=100_000,
    ).model_copy(update={"source": source})
    await repository.upsert(
        award,
        make_signal(
            award,
            signal_id=f"signal-{award_id}",
            score=8,
            candidate=True,
            priority_score=73,
        ),
    )


async def test_repository_upserts_complete_signal_graph() -> None:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        session_factory = build_session_factory(engine)
        repository = SqlAlchemySignalRepository(session_factory)
        award = make_award(
            award_id="AWARD-001",
            company_name="Acme Space, Inc.",
            uei="ACME001",
            amount=250_000,
        )
        signal = make_signal(
            award,
            signal_id="signal-001",
            score=11,
            candidate=True,
            priority_score=73,
        )

        assert await repository.upsert(award, signal) is True

        updated_award = award.model_copy(update={"amount": 300_000})
        updated_signal = signal.model_copy(update={"amount": 300_000})

        assert (
            await repository.upsert(
                updated_award,
                updated_signal,
            )
            is False
        )
        assert await repository.count() == 1

        [stored] = await repository.list()

        assert stored.company_name == "Acme Space, Inc."
        assert stored.amount == 300_000
        assert stored.relevance_score == 11
        assert stored.priority_score == 73
        assert stored.priority_reasons == ["test priority"]
        assert stored.is_startup_candidate is True
        assert stored.occurred_on == date(2026, 8, 15)

        async with session_factory() as session:
            counts = {
                "companies": await session.scalar(select(func.count()).select_from(Company)),
                "aliases": await session.scalar(select(func.count()).select_from(CompanyAlias)),
                "awards": await session.scalar(select(func.count()).select_from(Award)),
                "signals": await session.scalar(select(func.count()).select_from(Signal)),
            }

            stored_award = await session.scalar(
                select(Award).where(Award.source_award_id == "AWARD-001")
            )

        assert stored_award is not None
        assert stored_award.action_date == date(2026, 8, 15)
        assert stored_award.award_start_date == date(2026, 9, 1)

        assert counts == {
            "companies": 1,
            "aliases": 1,
            "awards": 1,
            "signals": 1,
        }
    finally:
        await engine.dispose()


async def test_repository_filters_and_orders_candidates() -> None:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        session_factory = build_session_factory(engine)
        repository = SqlAlchemySignalRepository(session_factory)

        records = [
            (
                make_award(
                    award_id="AWARD-LOW",
                    company_name="Low Orbit, Inc.",
                    uei="LOW001",
                    amount=400_000,
                ),
                "signal-low",
                5,
                True,
                90,
            ),
            (
                make_award(
                    award_id="AWARD-HIGH",
                    company_name="High Orbit, Inc.",
                    uei="HIGH001",
                    amount=100_000,
                ),
                "signal-high",
                9,
                True,
                70,
            ),
            (
                make_award(
                    award_id="AWARD-NOISE",
                    company_name="Noise Research, Inc.",
                    uei="NOISE001",
                    amount=900_000,
                ),
                "signal-noise",
                12,
                False,
                99,
            ),
        ]

        for award, signal_id, score, candidate, priority_score in records:
            signal = make_signal(
                award,
                signal_id=signal_id,
                score=score,
                candidate=candidate,
                priority_score=priority_score,
            )
            await repository.upsert(award, signal)

        candidates = await repository.list(
            minimum_score=4,
            startup_candidates_only=True,
        )

        assert [signal.signal_id for signal in candidates] == [
            "signal-low",
            "signal-high",
        ]
    finally:
        await engine.dispose()


async def test_repository_records_ingestion_run_lifecycle() -> None:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        session_factory = build_session_factory(engine)
        repository = SqlAlchemySignalRepository(session_factory)

        completed_id = await repository.start_ingestion(
            source="usaspending",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 8, 25),
        )
        await repository.finish_ingestion(
            completed_id,
            IngestionResult(
                source="usaspending",
                fetched_count=100,
                relevant_count=7,
                stored_count=6,
                duplicate_count=1,
            ),
        )

        failed_id = await repository.start_ingestion(
            source="usaspending",
            start_date=date(2026, 8, 26),
            end_date=date(2026, 8, 27),
        )
        await repository.fail_ingestion(
            failed_id,
            "upstream timeout",
        )

        async with session_factory() as session:
            completed = await session.get(
                IngestionRun,
                completed_id,
            )
            failed = await session.get(
                IngestionRun,
                failed_id,
            )

        assert completed is not None
        assert completed.status == "completed"
        assert completed.fetched_count == 100
        assert completed.relevant_count == 7
        assert completed.stored_count == 6
        assert completed.duplicate_count == 1
        assert completed.completed_at is not None

        assert failed is not None
        assert failed.status == "failed"
        assert failed.error_message == "upstream timeout"
        assert failed.completed_at is not None
    finally:
        await engine.dispose()


async def test_repository_builds_company_profiles_from_persisted_signals() -> None:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        session_factory = build_session_factory(engine)
        repository = SqlAlchemySignalRepository(session_factory)

        first_award = make_award(
            award_id="PROFILE-001",
            company_name="Acme Space, Inc.",
            uei="ACME-PROFILE",
            amount=1_000_000,
        )
        second_award = make_award(
            award_id="PROFILE-002",
            company_name="Acme Space, Inc.",
            uei="ACME-PROFILE",
            amount=3_000_000,
        )

        await repository.upsert(
            first_award,
            make_signal(
                first_award,
                signal_id="profile-signal-001",
                score=8,
                candidate=True,
                priority_score=65,
            ),
        )
        await repository.upsert(
            second_award,
            make_signal(
                second_award,
                signal_id="profile-signal-002",
                score=10,
                candidate=True,
                priority_score=92,
            ),
        )

        [profile] = await repository.list_company_profiles()

        assert profile.company_name == "Acme Space, Inc."
        assert profile.company_uei == "ACME-PROFILE"
        assert profile.signal_count == 2
        assert profile.total_amount == 4_000_000
        assert profile.average_relevance_score == 9
        assert profile.max_priority_score == 92
        assert profile.startup_candidate is True
        assert profile.established_contractor is False

    finally:
        await engine.dispose()


async def test_sql_reclassification_updates_persisted_amentum_signal() -> None:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        session_factory = build_session_factory(engine)
        repository = SqlAlchemySignalRepository(session_factory)

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
            reasons=["stale classification"],
            organization_type=OrganizationType.COMPANY,
            is_startup_candidate=True,
            quality_flags=[],
            source="usaspending",
            source_award_id="80ARC026FA012",
            evidence_url=award.source_url,
        )

        original_detected_at = stale_signal.detected_at

        await repository.upsert(
            award,
            stale_signal,
        )

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

        [profile] = await repository.list_company_profiles()

        assert profile.company_name == "AMENTUM TECHNOLOGY, INC."
        assert profile.startup_candidate is False
        assert profile.established_contractor is True

    finally:
        await engine.dispose()


async def test_company_identity_keeps_distinct_ueis_and_ambiguous_names_separate(
    persisted_repository,
) -> None:
    repository, session_factory = persisted_repository
    for award_id, uei in [("A", "UEI-A"), ("B", "UEI-B"), ("C", None), ("D", None)]:
        await store_test_award(
            repository,
            award_id=award_id,
            company_name="Shared Space LLC",
            uei=uei,
        )

    async with session_factory() as session:
        companies = (await session.scalars(select(Company))).all()
    assert len(companies) == 3
    assert {company.uei for company in companies} == {"UEI-A", "UEI-B", None}
    profiles = await repository.list_company_profiles()
    assert {profile.company_uei: profile.signal_count for profile in profiles} == {
        "UEI-A": 1,
        "UEI-B": 1,
        None: 2,
    }


async def test_company_identity_resolves_renamed_source_alias_without_uei(
    persisted_repository,
) -> None:
    repository, session_factory = persisted_repository
    for award_id, company_name, uei in [
        ("A", "Original Space LLC", "UEI-A"),
        ("B", "Renamed Space LLC", "UEI-A"),
        ("C", "Renamed Space LLC", None),
    ]:
        await store_test_award(
            repository,
            award_id=award_id,
            company_name=company_name,
            uei=uei,
        )

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Company)) == 1
    [profile] = await repository.list_company_profiles()
    assert profile.company_uei == "UEI-A"
    assert profile.signal_count == 3


async def test_company_identity_does_not_resolve_alias_from_another_source(
    persisted_repository,
) -> None:
    repository, session_factory = persisted_repository
    await store_test_award(repository, award_id="A", company_name="Original Space LLC", uei="UEI-A")
    await store_test_award(repository, award_id="B", company_name="Renamed Space LLC", uei="UEI-A")
    await store_test_award(
        repository,
        award_id="C",
        company_name="Renamed Space LLC",
        uei=None,
        source="other-source",
    )

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Company)) == 2


async def test_company_identity_does_not_resolve_ambiguous_source_alias(
    persisted_repository,
) -> None:
    repository, session_factory = persisted_repository
    for award_id, company_name, uei in [
        ("A", "Original A LLC", "UEI-A"),
        ("B", "Original B LLC", "UEI-B"),
        ("C", "Shared Alias LLC", "UEI-A"),
        ("D", "Shared Alias LLC", "UEI-B"),
        ("E", "Shared Alias LLC", None),
    ]:
        await store_test_award(
            repository,
            award_id=award_id,
            company_name=company_name,
            uei=uei,
        )

    async with session_factory() as session:
        companies = (await session.scalars(select(Company))).all()
    assert len(companies) == 3
    assert {company.uei for company in companies} == {"UEI-A", "UEI-B", None}


async def test_company_identity_normalizes_uei_case_and_whitespace(
    persisted_repository,
) -> None:
    repository, session_factory = persisted_repository
    await store_test_award(
        repository, award_id="A", company_name="Original Space LLC", uei="  uei-a  "
    )
    await store_test_award(repository, award_id="B", company_name="Renamed Space LLC", uei="UEI-A")

    async with session_factory() as session:
        [company] = (await session.scalars(select(Company))).all()
    assert company.uei == "UEI-A"


async def test_duplicate_upsert_preserves_first_detection_times(persisted_repository) -> None:
    repository, session_factory = persisted_repository
    award = make_award(award_id="A", company_name="Example Space LLC", uei="UEI-A", amount=100_000)
    detected_at = datetime(2026, 8, 1, 12, 30, tzinfo=UTC)
    signal = make_signal(
        award, signal_id="signal-A", score=8, candidate=True, priority_score=73
    ).model_copy(update={"detected_at": detected_at})
    await repository.upsert(award, signal)
    assert (
        await repository.upsert(
            award,
            signal.model_copy(update={"detected_at": detected_at + timedelta(days=1)}),
        )
        is False
    )

    [stored] = await repository.list()
    assert stored.detected_at == detected_at
    async with session_factory() as session:
        [stored_award] = (await session.scalars(select(Award))).all()
    assert stored_award.detected_at.replace(tzinfo=UTC) == detected_at


async def test_upsert_reuses_legacy_display_id_award_and_preserves_signal_id(
    persisted_repository,
) -> None:
    repository, session_factory = persisted_repository
    award = make_award(
        award_id="DISPLAY-ID", company_name="Example Space LLC", uei="UEI-A", amount=100_000
    )
    signal = make_signal(
        award, signal_id="legacy-signal", score=8, candidate=True, priority_score=73
    )
    await repository.upsert(award, signal)
    canonical_award = award.model_copy(
        update={"source_award_id": award.generated_internal_id, "amount": 200_000}
    )
    canonical_signal = make_signal(
        canonical_award,
        signal_id="canonical-signal",
        score=9,
        candidate=True,
        priority_score=80,
    )
    assert await repository.upsert(canonical_award, canonical_signal) is False

    [stored] = await repository.list()
    assert stored.signal_id == "legacy-signal"
    assert stored.source_award_id == award.generated_internal_id
    assert stored.amount == 200_000
    assert stored.detected_at == signal.detected_at
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Award)) == 1
        assert await session.scalar(select(func.count()).select_from(Signal)) == 1


async def test_upsert_keeps_different_generated_award_ids_separate(persisted_repository) -> None:
    repository, session_factory = persisted_repository
    for index in [1, 2]:
        generated_id = f"CONT_AWD_DISPLAY-ID_AGENCY-{index}"
        award = make_award(
            award_id=generated_id,
            company_name="Example Space LLC",
            uei="UEI-A",
            amount=100_000,
        ).model_copy(update={"generated_internal_id": generated_id})
        signal = make_signal(
            award,
            signal_id=f"signal-{index}",
            score=8,
            candidate=True,
            priority_score=73,
        )
        assert await repository.upsert(award, signal) is True

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Award)) == 2
    assert await repository.count() == 2


async def test_sql_reclassification_retains_evidence_and_restores_removed_signal(
    persisted_repository,
) -> None:
    repository, session_factory = persisted_repository
    award = make_award(
        award_id="A", company_name="Example Space LLC", uei="UEI-A", amount=100_000
    ).model_copy(update={"description": "Office supplies", "naics_code": None, "psc_code": None})
    signal = make_signal(award, signal_id="signal-A", score=8, candidate=True, priority_score=73)
    await repository.upsert(award, signal)
    service = SignalReclassificationService(repository=repository)
    result = await service.reclassify(as_of=date(2026, 9, 16))
    assert result.scanned_count == 1
    assert result.removed_count == 1
    assert await repository.count() == 0
    [retained] = await repository.list_reclassification_inputs()
    assert retained.signal is None
    assert retained.award.description == "Office supplies"
    assert retained.detected_at == signal.detected_at

    async with session_factory.begin() as session:
        [stored_award] = (await session.scalars(select(Award))).all()
        stored_award.description = "Lunar spacecraft prototype"

    result = await service.reclassify(as_of=date(2026, 9, 16))
    assert result.scanned_count == 1
    assert result.reclassified_count == 1
    assert result.removed_count == 0
    [restored] = await repository.list()
    assert restored.summary == "Lunar spacecraft prototype"
    assert restored.detected_at == signal.detected_at
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Award)) == 1
