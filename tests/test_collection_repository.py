from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import create_engine, func, insert, inspect, select
from sqlalchemy.exc import IntegrityError

from orbital_signal.collection import MonthlyCollectionService
from orbital_signal.collection_models import CollectionRepository, CollectionWindow
from orbital_signal.collection_repository import (
    InMemoryCollectionRepository,
    SqlAlchemyCollectionRepository,
)
from orbital_signal.database import Base, build_async_engine, build_session_factory
from orbital_signal.db_models import Award, Company, CompanyAlias, Signal
from orbital_signal.db_models import CollectionWindow as CollectionWindowModel
from orbital_signal.domain import AwardRecord
from orbital_signal.sources.usaspending import DEFAULT_AGENCIES, AwardSearchPage
from orbital_signal.sql_repository import SqlAlchemySignalRepository


def make_window(**updates) -> CollectionWindow:
    values = {
        "window_id": "january-nasa",
        "agency": "NASA",
        "month_start": date(2026, 1, 1),
        "start_date": date(2026, 1, 1),
        "end_date": date(2026, 1, 31),
    }
    return CollectionWindow.model_validate(values | updates)


@pytest.fixture(params=["memory", "sql"])
async def checkpoint_repository(request) -> AsyncIterator[CollectionRepository]:
    if request.param == "memory":
        yield InMemoryCollectionRepository()
        return

    engine = build_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield SqlAlchemyCollectionRepository(build_session_factory(engine))
    finally:
        await engine.dispose()


async def test_checkpoint_updates_preserve_partial_counts(checkpoint_repository) -> None:
    started_at = datetime(2026, 11, 1, tzinfo=UTC)
    window = make_window(status="running", attempts=1, started_at=started_at)
    await checkpoint_repository.save_window(window)
    window = window.model_copy(
        update={
            "status": "failed",
            "pages_fetched": 2,
            "fetched_count": 100,
            "relevant_count": 30,
            "stored_count": 20,
            "duplicate_count": 10,
            "has_more": True,
            "error_message": "third page timed out",
            "completed_at": started_at,
        }
    )
    await checkpoint_repository.save_window(window)

    saved = await checkpoint_repository.get_window(
        source="usaspending", agency="NASA", start_date=window.start_date, end_date=window.end_date
    )
    assert saved == window
    assert (
        len(
            await checkpoint_repository.list_windows(
                start_month=date(2026, 1, 1), end_month=date(2026, 1, 1)
            )
        )
        == 1
    )


async def test_checkpoint_identity_includes_source_agency_and_dates(checkpoint_repository) -> None:
    nasa = make_window()
    defense = make_window(window_id="january-defense", agency="DoD")
    another_source = make_window(window_id="january-other-source", source="future-source")
    narrower = make_window(window_id="january-nasa-first-half", end_date=date(2026, 1, 15))
    for window in (nasa, defense, another_source, narrower):
        await checkpoint_repository.save_window(window)
        assert (
            await checkpoint_repository.get_window(
                source=window.source,
                agency=window.agency,
                start_date=window.start_date,
                end_date=window.end_date,
            )
            == window
        )

    assert (
        await checkpoint_repository.get_window(
            source="usaspending",
            agency="unknown-agency",
            start_date=nasa.start_date,
            end_date=nasa.end_date,
        )
        is None
    )


async def test_checkpoint_lists_split_and_incomplete_windows_in_period(
    checkpoint_repository,
) -> None:
    parent = make_window(status="split", has_more=True)
    child = make_window(
        window_id="january-nasa-first-half", end_date=date(2026, 1, 15), status="completed"
    )
    incomplete = make_window(
        window_id="january-nasa-day-31",
        start_date=date(2026, 1, 31),
        status="incomplete",
        has_more=True,
    )
    february = make_window(
        window_id="february-nasa",
        month_start=date(2026, 2, 1),
        start_date=date(2026, 2, 1),
        end_date=date(2026, 2, 28),
    )
    for window in (parent, child, incomplete, february):
        await checkpoint_repository.save_window(window)

    january = await checkpoint_repository.list_windows(
        start_month=date(2026, 1, 1), end_month=date(2026, 1, 1)
    )
    assert {window.window_id for window in january} == {
        parent.window_id,
        child.window_id,
        incomplete.window_id,
    }
    assert {window.status for window in january} == {"split", "completed", "incomplete"}


async def test_checkpoint_objects_require_explicit_save(checkpoint_repository) -> None:
    original = make_window()
    await checkpoint_repository.save_window(original)
    original.status = "failed"
    original.fetched_count = 999

    loaded = await checkpoint_repository.get_window(
        source="usaspending",
        agency="NASA",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
    )
    assert loaded is not None
    assert loaded.status == "pending"
    assert loaded.fetched_count == 0
    loaded.status = "running"

    listed = await checkpoint_repository.list_windows(
        start_month=date(2026, 1, 1), end_month=date(2026, 1, 1)
    )
    assert listed[0].status == "pending"
    listed[0].status = "incomplete"
    assert (
        await checkpoint_repository.list_windows(
            start_month=date(2026, 1, 1), end_month=date(2026, 1, 1)
        )
    )[0].status == "pending"


async def test_duplicate_checkpoint_identity_is_rejected(checkpoint_repository) -> None:
    await checkpoint_repository.save_window(make_window())
    with pytest.raises((ValueError, IntegrityError)):
        await checkpoint_repository.save_window(make_window(window_id="another-id"))


async def test_checkpoints_survive_new_repository_instance() -> None:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = build_session_factory(engine)
        first_repository = SqlAlchemyCollectionRepository(factory)
        window = make_window(
            status="incomplete", attempts=2, pages_fetched=5, has_more=True, fetched_count=500
        )
        await first_repository.save_window(window)

        resumed_repository = SqlAlchemyCollectionRepository(factory)
        assert (
            await resumed_repository.get_window(
                source="usaspending",
                agency=window.agency,
                start_date=window.start_date,
                end_date=window.end_date,
            )
            == window
        )
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "updates",
    [
        {"end_date": date(2025, 12, 31)},
        {"month_start": date(2026, 1, 2)},
        {"end_date": date(2026, 2, 1)},
        {"start_date": date(2027, 1, 1), "end_date": date(2027, 1, 31)},
        {"attempts": -1},
        {"pages_fetched": -1},
        {"fetched_count": -1},
        {"status": "complete-source-coverage"},
    ],
)
def test_invalid_checkpoint_is_rejected(updates) -> None:
    with pytest.raises(ValidationError):
        make_window(**updates)


def test_collection_migration_preserves_existing_company_data() -> None:
    migration_path = (
        Path(__file__).parents[1] / "migrations/versions/c961fd3e5a22_add_collection_windows.py"
    )
    specification = spec_from_file_location("collection_checkpoint_migration", migration_path)
    assert specification is not None and specification.loader is not None
    migration = module_from_spec(specification)
    specification.loader.exec_module(migration)
    assert migration.down_revision == "a48c1f2d9630"

    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(
                connection,
                tables=[
                    table
                    for table in Base.metadata.sorted_tables
                    if table.name not in {"collection_windows", "collection_attempts"}
                ],
            )
            connection.execute(
                insert(Company).values(
                    id="existing-company",
                    canonical_name="Existing Space Company",
                    normalized_name="existing space company",
                )
            )
            existing_tables = set(inspect(connection).get_table_names())
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                assert set(inspect(connection).get_table_names()) == existing_tables | {
                    "collection_windows"
                }
                connection.execute(
                    insert(CollectionWindowModel).values(
                        id="checkpoint-after-upgrade",
                        source="usaspending",
                        agency="NASA",
                        month_start=date(2026, 1, 1),
                        start_date=date(2026, 1, 1),
                        end_date=date(2026, 1, 31),
                    )
                )
                migration.downgrade()
            assert set(inspect(connection).get_table_names()) == existing_tables
            assert connection.scalar(select(Company.canonical_name)) == "Existing Space Company"
    finally:
        engine.dispose()


async def test_monthly_collection_resumes_after_database_restart_without_duplicates(
    tmp_path: Path, satellite_award: AwardRecord
) -> None:
    nasa, defense = DEFAULT_AGENCIES
    start, end = date(2026, 1, 1), date(2026, 1, 31)
    awards = {
        agency: AwardRecord.model_validate(
            satellite_award.model_dump()
            | {
                "source_award_id": award_id,
                "generated_internal_id": f"CONT_AWD_{award_id}",
                "recipient_name": company,
                "recipient_uei": uei,
                "awarding_agency": agency,
                "start_date": date(2026, 1, 10),
                "action_date": date(2026, 1, 15),
                "source_url": f"https://www.usaspending.gov/award/CONT_AWD_{award_id}",
            }
        )
        for agency, award_id, company, uei in (
            (nasa, "NASA-DURABLE", "Nova Lunar LLC", "NOVA123"),
            (defense, "DOD-DURABLE", "Sentinel Orbital LLC", "SENTINEL123"),
        )
    }

    class RestartablePageSource:
        def __init__(self, *, fail_defense: bool) -> None:
            self.fail_defense = fail_defense
            self.requests: list[str] = []

        async def search_awards_page(
            self,
            *,
            start_date: date,
            end_date: date,
            agency: str,
            page: int = 1,
            records_per_page: int = 100,
        ) -> AwardSearchPage:
            assert (start_date, end_date, page, records_per_page) == (start, end, 1, 100)
            self.requests.append(agency)
            if self.fail_defense and agency == defense:
                raise httpx.ReadTimeout("simulated source interruption")
            return AwardSearchPage(awards=[awards[agency]], fetched_count=1, has_more=False)

    database_url = f"sqlite+aiosqlite:///{tmp_path / 'collection.sqlite'}"
    first_engine = build_async_engine(database_url)
    try:
        async with first_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        first_factory = build_session_factory(first_engine)
        first_repository = SqlAlchemySignalRepository(first_factory)
        first_source = RestartablePageSource(fail_defense=True)
        first = await MonthlyCollectionService(
            source=first_source,
            repository=first_repository,
            checkpoints=SqlAlchemyCollectionRepository(first_factory),
        ).collect(start_month=start, end_month=start, window_days=31)

        assert first.status == "incomplete"
        assert {window.agency: window.status for window in first.windows} == {
            nasa: "completed",
            defense: "failed",
        }
        assert first_source.requests == [nasa, defense]
        [original_signal] = await first_repository.list()
        assert original_signal.company_uei == "NOVA123"
        assert await first_repository.count() == 1
    finally:
        await first_engine.dispose()

    restarted_engine = build_async_engine(database_url)
    try:
        restarted_factory = build_session_factory(restarted_engine)
        restarted_repository = SqlAlchemySignalRepository(restarted_factory)
        restarted_source = RestartablePageSource(fail_defense=False)
        service = MonthlyCollectionService(
            source=restarted_source,
            repository=restarted_repository,
            checkpoints=SqlAlchemyCollectionRepository(restarted_factory),
        )
        resumed = await service.collect(start_month=start, end_month=start, window_days=31)

        assert resumed.status == "completed"
        assert resumed.skipped_windows == 1
        assert restarted_source.requests == [defense]
        assert {window.agency: window.attempts for window in resumed.windows} == {
            nasa: 1,
            defense: 2,
        }
        signals_after_resume = await restarted_repository.list()
        assert len(signals_after_resume) == 2
        assert (
            next(signal for signal in signals_after_resume if signal.company_uei == "NOVA123")
            == original_signal
        )

        restarted_source.requests.clear()
        refreshed = await service.collect(
            start_month=start, end_month=start, window_days=31, refresh=True
        )

        assert refreshed.status == "completed"
        assert restarted_source.requests == [nasa, defense]
        assert all(window.stored_count == 0 for window in refreshed.windows)
        assert all(window.duplicate_count == 1 for window in refreshed.windows)
        assert await restarted_repository.list() == signals_after_resume
        profiles = await restarted_repository.list_company_profiles()
        assert {profile.company_uei for profile in profiles} == {"NOVA123", "SENTINEL123"}
        assert all(profile.signal_count == 1 for profile in profiles)

        async with restarted_factory() as session:
            for model in (Company, CompanyAlias, Award, Signal):
                assert await session.scalar(select(func.count()).select_from(model)) == 2
            graph = (
                await session.execute(
                    select(Company.uei, Award.source_award_id, Signal.id)
                    .join(Award, Award.company_id == Company.id)
                    .join(Signal, Signal.award_id == Award.id)
                )
            ).all()
        assert {(uei, award_id) for uei, award_id, _ in graph} == {
            ("NOVA123", "NASA-DURABLE"),
            ("SENTINEL123", "DOD-DURABLE"),
        }
        assert {signal_id for _, _, signal_id in graph} == {
            signal.signal_id for signal in signals_after_resume
        }
    finally:
        await restarted_engine.dispose()
