from datetime import date
from decimal import Decimal

from sqlalchemy import inspect, text

from orbital_signal.config import Settings
from orbital_signal.database import (
    Base,
    build_async_engine,
    build_session_factory,
)
from orbital_signal.db_models import (
    Award,
    Company,
    CompanyAlias,
    IngestionRun,
    Signal,
)

EXPECTED_TABLES = {
    "awards",
    "companies",
    "company_aliases",
    "ingestion_runs",
    "signals",
}


def test_settings_load_database_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "ORBITAL_SIGNAL_DATABASE_URL",
        "sqlite+aiosqlite:///:memory:",
    )
    monkeypatch.setenv("ORBITAL_SIGNAL_DATABASE_ECHO", "true")

    settings = Settings(_env_file=None)

    assert settings.database_url == "sqlite+aiosqlite:///:memory:"
    assert settings.database_echo is True


async def test_async_session_factory_executes_query() -> None:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")

    try:
        session_factory = build_session_factory(engine)

        async with session_factory() as session:
            result = await session.execute(text("SELECT 1"))

        assert result.scalar_one() == 1
    finally:
        await engine.dispose()


def test_metadata_declares_persistence_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


async def test_models_persist_across_sessions() -> None:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            table_names = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )

        assert table_names == EXPECTED_TABLES

        session_factory = build_session_factory(engine)

        company = Company(
            id="company-acme",
            canonical_name="Acme Space, Inc.",
            normalized_name="acme space",
            uei="ACME123",
            organization_type="company",
        )
        alias = CompanyAlias(
            company=company,
            alias="ACME SPACE INC",
            normalized_alias="acme space",
            source="usaspending",
        )
        award = Award(
            id="award-123",
            company=company,
            source="usaspending",
            source_award_id="AWARD-123",
            generated_internal_id="CONT_AWD_123",
            recipient_name="ACME SPACE INC",
            recipient_uei="ACME123",
            amount=Decimal("250000.00"),
            awarding_agency=("National Aeronautics and Space Administration"),
            description="Lunar spacecraft prototype",
            award_start_date=date(2026, 8, 1),
            award_end_date=date(2027, 8, 1),
            naics_code="336414",
            psc_code="AR11",
            evidence_url=("https://www.usaspending.gov/award/CONT_AWD_123"),
        )
        signal = Signal(
            id="signal-123",
            company=company,
            award=award,
            relevance_score=12,
            matched_terms=["lunar", "spacecraft"],
            reasons=["matched space evidence"],
            organization_type="company",
            is_startup_candidate=True,
            quality_flags=[],
        )
        ingestion_run = IngestionRun(
            id="run-123",
            source="usaspending",
            status="completed",
            requested_start_date=date(2026, 1, 1),
            requested_end_date=date(2026, 8, 25),
            fetched_count=1,
            relevant_count=1,
            stored_count=1,
            duplicate_count=0,
        )

        async with session_factory.begin() as session:
            session.add_all(
                [
                    company,
                    alias,
                    award,
                    signal,
                    ingestion_run,
                ]
            )

        async with session_factory() as session:
            stored_company = await session.get(Company, "company-acme")
            stored_award = await session.get(Award, "award-123")
            stored_signal = await session.get(Signal, "signal-123")
            stored_run = await session.get(IngestionRun, "run-123")

        assert stored_company is not None
        assert stored_company.canonical_name == "Acme Space, Inc."
        assert stored_award is not None
        assert stored_award.amount == Decimal("250000.00")
        assert stored_signal is not None
        assert stored_signal.is_startup_candidate is True
        assert stored_run is not None
        assert stored_run.status == "completed"
    finally:
        await engine.dispose()
