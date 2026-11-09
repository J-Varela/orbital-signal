from datetime import UTC, date, datetime, timedelta

import pytest

from orbital_signal.database import Base, build_async_engine, build_session_factory
from orbital_signal.db_models import IngestionRun
from orbital_signal.domain import IngestionResult
from orbital_signal.sql_repository import SqlAlchemySignalRepository


@pytest.mark.parametrize("failed", [False, True])
async def test_ingestion_completion_cannot_precede_start_even_with_clock_skew(failed: bool) -> None:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")
    started = datetime.now(UTC) + timedelta(hours=1)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.tables["ingestion_runs"].create)
        sessions = build_session_factory(engine)
        async with sessions.begin() as session:
            session.add(
                IngestionRun(
                    id="clock-skew",
                    source="usaspending",
                    status="running",
                    started_at=started,
                    requested_start_date=date(2026, 9, 1),
                    requested_end_date=date(2026, 9, 1),
                )
            )
        repository = SqlAlchemySignalRepository(sessions)
        if failed:
            await repository.fail_ingestion("clock-skew", "RemoteProtocolError")
        else:
            await repository.finish_ingestion(
                "clock-skew",
                IngestionResult(
                    source="usaspending",
                    fetched_count=1,
                    relevant_count=1,
                    stored_count=1,
                    duplicate_count=0,
                ),
            )
        async with sessions() as session:
            run = await session.get(IngestionRun, "clock-skew")
            assert run is not None and run.completed_at is not None
            assert run.completed_at.replace(tzinfo=UTC) >= run.started_at.replace(tzinfo=UTC)
    finally:
        await engine.dispose()
