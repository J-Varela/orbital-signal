import asyncio
import subprocess
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from orbital_signal.collection_models import CollectionAlreadyRunningError
from orbital_signal.collection_repository import (
    InMemoryCollectionRepository,
    SqlAlchemyCollectionRepository,
)
from orbital_signal.database import build_async_engine, build_session_factory


async def test_memory_lock_rejects_overlap_and_releases_after_failure() -> None:
    repository = InMemoryCollectionRepository()
    async with repository.collection_lock():
        with pytest.raises(CollectionAlreadyRunningError):
            async with repository.collection_lock():
                pytest.fail("an overlapping collector acquired the lock")
        async with repository.collection_lock(source="another-source"):
            pass
    with pytest.raises(RuntimeError, match="simulated interruption"):
        async with repository.collection_lock():
            raise RuntimeError("simulated interruption")
    async with repository.collection_lock():
        pass


async def test_memory_lock_releases_when_collection_task_is_cancelled() -> None:
    repository = InMemoryCollectionRepository()
    acquired = asyncio.Event()

    async def collect() -> None:
        async with repository.collection_lock():
            acquired.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(collect())
    await acquired.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with repository.collection_lock():
        pass


_SQLITE_LOCK_PROBE = """
import asyncio
import sys
from orbital_signal.collection_models import CollectionAlreadyRunningError
from orbital_signal.collection_repository import SqlAlchemyCollectionRepository
from orbital_signal.database import build_async_engine, build_session_factory

async def main():
    engine = build_async_engine(sys.argv[1])
    repository = SqlAlchemyCollectionRepository(build_session_factory(engine))
    try:
        async with repository.collection_lock():
            print('acquired')
    except CollectionAlreadyRunningError:
        print('busy')
    finally:
        await engine.dispose()

asyncio.run(main())
"""


async def test_sqlite_lock_coordinates_separate_processes_and_canonical_paths(
    tmp_path: Path,
) -> None:
    database = tmp_path / "collection.sqlite"
    database.touch()
    alias = tmp_path / "database-alias.sqlite"
    alias.symlink_to(database)
    engine = build_async_engine(f"sqlite+aiosqlite:///{database}")
    repository = SqlAlchemyCollectionRepository(build_session_factory(engine))

    def probe() -> str:
        completed = subprocess.run(
            [sys.executable, "-c", _SQLITE_LOCK_PROBE, f"sqlite+aiosqlite:///{alias}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return completed.stdout.strip()

    try:
        async with repository.collection_lock():
            assert probe() == "busy"
        assert probe() == "acquired"
        with pytest.raises(RuntimeError, match="simulated interruption"):
            async with repository.collection_lock():
                raise RuntimeError("simulated interruption")
        assert probe() == "acquired"
    finally:
        await engine.dispose()


class AdvisoryConnection:
    def __init__(self, *, available: bool) -> None:
        self.available = available
        self.transaction_open = False
        self.connection_open = False
        self.calls: list[tuple[str, dict[str, int]]] = []

    @asynccontextmanager
    async def begin(self) -> AsyncGenerator[None, None]:
        self.transaction_open = True
        try:
            yield
        finally:
            self.transaction_open = False

    async def scalar(self, statement, parameters: dict[str, int]) -> bool:
        assert self.transaction_open
        self.calls.append((str(statement), parameters))
        return self.available


class AdvisoryEngine:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, *, available: bool) -> None:
        self.connection = AdvisoryConnection(available=available)

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[AdvisoryConnection, None]:
        self.connection.connection_open = True
        try:
            yield self.connection
        finally:
            self.connection.connection_open = False


@pytest.mark.parametrize("available", [True, False])
async def test_postgresql_advisory_lock_holds_dedicated_transaction_until_exit(available) -> None:
    # Replace a local test engine with a connection stub; never contact PostgreSQL.
    sqlite_engine = build_async_engine("sqlite+aiosqlite:///:memory:")
    repository = SqlAlchemyCollectionRepository(build_session_factory(sqlite_engine))
    advisory_engine = AdvisoryEngine(available=available)
    repository._engine = cast(AsyncEngine, advisory_engine)
    try:
        if available:
            with pytest.raises(RuntimeError, match="simulated collection failure"):
                async with repository.collection_lock():
                    assert advisory_engine.connection.connection_open
                    assert advisory_engine.connection.transaction_open
                    raise RuntimeError("simulated collection failure")
        else:
            with pytest.raises(CollectionAlreadyRunningError):
                async with repository.collection_lock():
                    pytest.fail("a busy PostgreSQL advisory lock was acquired")
        assert not advisory_engine.connection.connection_open
        assert not advisory_engine.connection.transaction_open
        [(statement, parameters)] = advisory_engine.connection.calls
        assert statement == "SELECT pg_try_advisory_xact_lock(:lock_key)"
        assert -(2**63) <= parameters["lock_key"] < 2**63
    finally:
        await sqlite_engine.dispose()
