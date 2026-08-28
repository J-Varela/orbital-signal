from datetime import date

from sqlalchemy import func, select

from orbital_signal.database import (
    Base,
    build_async_engine,
    build_session_factory,
)
from orbital_signal.db_models import (
    Award,
    Company,
    CompanyAlias,
    Signal,
)
from orbital_signal.domain import (
    AwardRecord,
    CompanySignal,
    OrganizationType,
)
from orbital_signal.sql_repository import (
    SqlAlchemySignalRepository,
)


def make_award(
    *,
    award_id: str,
    company_name: str,
    uei: str,
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
        start_date=date(2026, 8, 1),
        end_date=date(2027, 8, 1),
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
) -> CompanySignal:
    return CompanySignal(
        signal_id=signal_id,
        company_name=award.recipient_name,
        company_uei=award.recipient_uei,
        occurred_on=award.start_date,
        amount=award.amount,
        agency=award.awarding_agency,
        summary=award.description,
        relevance_score=score,
        matched_terms=["lunar", "spacecraft"],
        reasons=["matched space evidence"],
        organization_type=OrganizationType.COMPANY,
        is_startup_candidate=candidate,
        quality_flags=[] if candidate else ["excluded"],
        source=award.source,
        source_award_id=award.source_award_id,
        evidence_url=award.source_url,
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
        assert stored.is_startup_candidate is True

        async with session_factory() as session:
            counts = {
                "companies": await session.scalar(select(func.count()).select_from(Company)),
                "aliases": await session.scalar(select(func.count()).select_from(CompanyAlias)),
                "awards": await session.scalar(select(func.count()).select_from(Award)),
                "signals": await session.scalar(select(func.count()).select_from(Signal)),
            }

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
            ),
        ]

        for award, signal_id, score, candidate in records:
            signal = make_signal(
                award,
                signal_id=signal_id,
                score=score,
                candidate=candidate,
            )
            await repository.upsert(award, signal)

        candidates = await repository.list(
            minimum_score=4,
            startup_candidates_only=True,
        )

        assert [signal.signal_id for signal in candidates] == [
            "signal-high",
            "signal-low",
        ]
    finally:
        await engine.dispose()
