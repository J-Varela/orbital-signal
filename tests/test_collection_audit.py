from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.exc import IntegrityError

from orbital_signal.collection_models import (
    CollectionAttempt,
    CollectionRepository,
    CollectionWindow,
)
from orbital_signal.collection_repository import (
    InMemoryCollectionRepository,
    SqlAlchemyCollectionRepository,
)
from orbital_signal.database import Base, build_async_engine, build_session_factory
from orbital_signal.db_models import CollectionAttempt as CollectionAttemptModel
from orbital_signal.db_models import CollectionWindow as CollectionWindowModel
from orbital_signal.db_models import Company

JANUARY = date(2026, 1, 1)
JANUARY_END = date(2026, 1, 31)
STARTED = datetime(2026, 11, 2, 12, tzinfo=UTC)


def window(**updates) -> CollectionWindow:
    return CollectionWindow.model_validate(
        {
            "window_id": "nasa-january",
            "agency": "NASA",
            "month_start": JANUARY,
            "start_date": JANUARY,
            "end_date": JANUARY_END,
            "attempts": 1,
            "status": "running",
            "started_at": STARTED,
        }
        | updates
    )


@pytest.fixture(params=["memory", "sql"])
async def audit_repository(request) -> AsyncIterator[CollectionRepository]:
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


async def test_attempts_retain_prior_failure_counts_across_retries(audit_repository) -> None:
    first = window()
    await audit_repository.save_window(first, counts_complete=False)
    first = window(
        status="failed",
        pages_fetched=1,
        fetched_count=3,
        relevant_count=2,
        stored_count=1,
        duplicate_count=1,
        error_message="page two failed",
        completed_at=STARTED + timedelta(minutes=1),
        has_more=True,
    )
    await audit_repository.save_window(first)
    await audit_repository.save_window(first)
    await audit_repository.save_window(window(attempts=2), counts_complete=False)
    second = window(
        attempts=2,
        status="completed",
        pages_fetched=2,
        fetched_count=4,
        relevant_count=3,
        stored_count=2,
        duplicate_count=1,
        completed_at=STARTED + timedelta(minutes=2),
        has_more=False,
    )
    await audit_repository.save_window(second)

    attempts = await audit_repository.list_attempts(start_month=JANUARY, end_month=JANUARY)
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]
    assert [attempt.status for attempt in attempts] == ["failed", "completed"]
    assert [attempt.fetched_count for attempt in attempts] == [3, 4]
    assert [attempt.stored_count for attempt in attempts] == [1, 2]
    assert [attempt.duplicate_count for attempt in attempts] == [1, 1]
    assert attempts[0].error_message == "page two failed"
    assert all(attempt.origin == "recorded" and attempt.counts_complete for attempt in attempts)
    assert sum(attempt.stored_count for attempt in attempts) == 3
    [latest] = await audit_repository.list_windows(start_month=JANUARY, end_month=JANUARY)
    assert latest == second
    assert latest.stored_count == 2


async def test_incomplete_counter_history_survives_later_success(audit_repository) -> None:
    await audit_repository.save_window(window(), counts_complete=False)
    await audit_repository.save_window(
        window(status="failed", stored_count=1, error_message="interrupted"), counts_complete=False
    )
    await audit_repository.save_window(
        window(attempts=2, status="completed", stored_count=1), counts_complete=True
    )

    attempts = await audit_repository.list_attempts(start_month=JANUARY, end_month=JANUARY)
    assert [attempt.counts_complete for attempt in attempts] == [False, True]
    assert [attempt.stored_count for attempt in attempts] == [1, 1]


async def test_attempts_are_filtered_by_month_and_return_copies(audit_repository) -> None:
    await audit_repository.save_window(window(status="completed"))
    await audit_repository.save_window(
        window(
            window_id="nasa-february",
            month_start=date(2026, 2, 1),
            start_date=date(2026, 2, 1),
            end_date=date(2026, 2, 28),
            status="failed",
        )
    )
    [attempt] = await audit_repository.list_attempts(start_month=JANUARY, end_month=JANUARY)
    assert attempt.window_id == "nasa-january"
    attempt.fetched_count = 999
    [reloaded] = await audit_repository.list_attempts(start_month=JANUARY, end_month=JANUARY)
    assert reloaded.fetched_count == 0


@pytest.mark.parametrize("status", ["completed", "failed", "incomplete", "running"])
def test_window_and_attempt_timestamps_handle_reversed_clocks(status) -> None:
    earlier = STARTED - timedelta(hours=1)
    checkpoint = window(status=status, completed_at=earlier)
    expected = None if status == "running" else STARTED
    assert checkpoint.completed_at == expected
    attempt = CollectionAttempt(
        attempt_id="attempt",
        window_id=checkpoint.window_id,
        attempt_number=1,
        status=status,
        started_at=STARTED.replace(tzinfo=None),
        completed_at=earlier,
    )
    assert attempt.started_at == STARTED
    assert attempt.completed_at == expected


async def test_sql_window_and_attempt_updates_are_atomic() -> None:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(
                text(
                    "CREATE TRIGGER reject_audit BEFORE INSERT ON collection_attempts "
                    "BEGIN SELECT RAISE(ABORT, 'simulated audit persistence failure'); END"
                )
            )
        repository = SqlAlchemyCollectionRepository(build_session_factory(engine))
        with pytest.raises(IntegrityError):
            await repository.save_window(window())
        assert (
            await repository.get_window(
                source="usaspending", agency="NASA", start_date=JANUARY, end_date=JANUARY_END
            )
            is None
        )
        assert await repository.list_attempts(start_month=JANUARY, end_month=JANUARY) == []
    finally:
        await engine.dispose()


async def test_legacy_reads_normalize_time_without_rewriting_checkpoint_rows() -> None:
    engine = build_async_engine("sqlite+aiosqlite:///:memory:")
    earlier = STARTED - timedelta(hours=1)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(
                insert(CollectionWindowModel).values(
                    id="nasa-january",
                    source="usaspending",
                    agency="NASA",
                    month_start=JANUARY,
                    start_date=JANUARY,
                    end_date=JANUARY_END,
                    attempts=3,
                    status="failed",
                    started_at=STARTED,
                    completed_at=earlier,
                )
            )
        factory = build_session_factory(engine)
        repository = SqlAlchemyCollectionRepository(factory)
        loaded = await repository.get_window(
            source="usaspending", agency="NASA", start_date=JANUARY, end_date=JANUARY_END
        )
        assert loaded is not None and loaded.completed_at == STARTED
        async with factory() as session:
            raw = await session.scalar(select(CollectionWindowModel.completed_at))
        assert raw is not None and raw.replace(tzinfo=UTC) == earlier
    finally:
        await engine.dispose()


def load_audit_migration() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "migrations/versions/d84fc8a61b35_add_collection_attempt_audit.py"
    )
    specification = spec_from_file_location("collection_attempt_audit_migration", path)
    assert specification is not None and specification.loader is not None
    migration = module_from_spec(specification)
    specification.loader.exec_module(migration)
    return migration


def test_audit_migration_preserves_existing_data_and_only_seeds_known_last_attempt() -> None:
    migration = load_audit_migration()
    assert migration.down_revision == "c961fd3e5a22"
    engine = create_engine("sqlite:///:memory:")
    earlier = STARTED - timedelta(hours=1)
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(
                connection,
                tables=[
                    table
                    for table in Base.metadata.sorted_tables
                    if table.name != "collection_attempts"
                ],
            )
            connection.execute(
                insert(Company).values(
                    id="existing", canonical_name="Existing", normalized_name="existing"
                )
            )
            connection.execute(
                insert(CollectionWindowModel),
                [
                    {
                        "id": identity,
                        "source": "usaspending",
                        "agency": agency,
                        "month_start": JANUARY,
                        "start_date": JANUARY,
                        "end_date": JANUARY_END,
                        "attempts": count,
                        "status": status,
                        "fetched_count": 10,
                        "stored_count": 2,
                        "started_at": STARTED,
                        "completed_at": earlier,
                    }
                    for identity, agency, count, status in (
                        ("legacy-failed", "NASA", 3, "failed"),
                        ("legacy-running", "DoD", 1, "running"),
                        ("not-attempted", "Other", 0, "pending"),
                    )
                ],
            )
            old_windows = connection.execute(select(CollectionWindowModel)).all()
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            rows = connection.execute(
                select(
                    CollectionAttemptModel.window_id,
                    CollectionAttemptModel.attempt_number,
                    CollectionAttemptModel.origin,
                    CollectionAttemptModel.counts_complete,
                    CollectionAttemptModel.fetched_count,
                    CollectionAttemptModel.stored_count,
                    CollectionAttemptModel.completed_at,
                ).order_by(CollectionAttemptModel.window_id)
            ).all()
            assert rows == [
                ("legacy-failed", 3, "legacy_snapshot", False, 10, 2, STARTED.replace(tzinfo=None)),
                ("legacy-running", 1, "legacy_snapshot", False, 10, 2, None),
            ]
            assert connection.execute(select(CollectionWindowModel)).all() == old_windows
            assert connection.scalar(select(Company.id)) == "existing"
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert connection.execute(select(CollectionWindowModel)).all() == old_windows
            assert connection.scalar(select(Company.id)) == "existing"
    finally:
        engine.dispose()


def test_audit_migration_renders_postgresql_sql_without_connecting() -> None:
    migration = load_audit_migration()
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "literal_binds": True, "output_buffer": output},
    )
    with Operations.context(context):
        migration.upgrade()
    sql = output.getvalue()
    assert "CREATE TABLE collection_attempts" in sql
    assert "INSERT INTO collection_attempts" in sql
    assert "legacy_snapshot" in sql
    assert "UPDATE collection_windows" not in sql
