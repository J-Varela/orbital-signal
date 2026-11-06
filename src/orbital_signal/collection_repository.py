"""In-memory and SQL storage for resumable collection windows."""

import asyncio
import fcntl
import hashlib
import os
import tempfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from orbital_signal.collection_models import (
    AttemptOrigin,
    CollectionAlreadyRunningError,
    CollectionAttempt,
    CollectionStatus,
    CollectionWindow,
)
from orbital_signal.database import AsyncSessionFactory
from orbital_signal.db_models import CollectionAttempt as CollectionAttemptModel
from orbital_signal.db_models import CollectionWindow as CollectionWindowModel


def _utc_or_none(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _window_identity(window: CollectionWindow) -> tuple[str, str, date, date]:
    return window.source, window.agency, window.start_date, window.end_date


def _current_attempt(window: CollectionWindow, *, counts_complete: bool) -> CollectionAttempt:
    identity = f"{window.window_id}:{window.attempts}"
    return CollectionAttempt(
        attempt_id=identity,
        window_id=window.window_id,
        attempt_number=window.attempts,
        status=window.status,
        pages_fetched=window.pages_fetched,
        fetched_count=window.fetched_count,
        relevant_count=window.relevant_count,
        stored_count=window.stored_count,
        duplicate_count=window.duplicate_count,
        has_more=window.has_more,
        error_message=window.error_message,
        started_at=window.started_at,
        completed_at=window.completed_at,
        counts_complete=counts_complete,
    )


class InMemoryCollectionRepository:
    """Copying checkpoint storage for isolated collection and API tests."""

    def __init__(self) -> None:
        self._windows: dict[str, CollectionWindow] = {}
        self._attempts: dict[str, CollectionAttempt] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get_window(
        self,
        *,
        source: str,
        agency: str,
        start_date: date,
        end_date: date,
    ) -> CollectionWindow | None:
        identity = source, agency, start_date, end_date
        for window in self._windows.values():
            if _window_identity(window) == identity:
                return window.model_copy(deep=True)
        return None

    async def save_window(self, window: CollectionWindow, *, counts_complete: bool = True) -> None:
        window = CollectionWindow.model_validate(window.model_dump())
        if any(
            stored.window_id != window.window_id
            and _window_identity(stored) == _window_identity(window)
            for stored in self._windows.values()
        ):
            raise ValueError("a collection window already exists for this source, agency and dates")
        self._windows[window.window_id] = window.model_copy(deep=True)
        if window.attempts:
            attempt = _current_attempt(window, counts_complete=counts_complete)
            previous = self._attempts.get(attempt.attempt_id)
            if previous is not None:
                attempt.origin = previous.origin
                if previous.origin == "legacy_snapshot":
                    attempt.counts_complete = False
            self._attempts[attempt.attempt_id] = attempt

    async def list_windows(
        self,
        *,
        start_month: date,
        end_month: date,
    ) -> list[CollectionWindow]:
        windows = (
            window
            for window in self._windows.values()
            if start_month <= window.month_start <= end_month
        )
        return [
            window.model_copy(deep=True)
            for window in sorted(
                windows,
                key=lambda window: (
                    window.month_start,
                    window.agency,
                    window.start_date,
                    window.end_date,
                ),
            )
        ]

    async def list_attempts(self, *, start_month: date, end_month: date) -> list[CollectionAttempt]:
        windows = await self.list_windows(start_month=start_month, end_month=end_month)
        window_ids = {window.window_id for window in windows}
        return [
            attempt.model_copy(deep=True)
            for attempt in sorted(
                self._attempts.values(),
                key=lambda attempt: (attempt.window_id, attempt.attempt_number),
            )
            if attempt.window_id in window_ids
        ]

    @asynccontextmanager
    async def collection_lock(self, *, source: str = "usaspending") -> AsyncGenerator[None, None]:
        lock = self._locks.setdefault(source, asyncio.Lock())
        if lock.locked():
            raise CollectionAlreadyRunningError(
                "Another collection is already running for this source"
            )
        async with lock:
            yield


class SqlAlchemyCollectionRepository:
    """Persist checkpoints without changing existing evidence or ingestion tables."""

    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory
        bind = session_factory.kw.get("bind")
        if not isinstance(bind, AsyncEngine):
            raise TypeError(
                "Collection checkpoints require a session factory bound to an AsyncEngine"
            )
        self._engine = bind

    async def get_window(
        self,
        *,
        source: str,
        agency: str,
        start_date: date,
        end_date: date,
    ) -> CollectionWindow | None:
        statement = select(CollectionWindowModel).where(
            CollectionWindowModel.source == source,
            CollectionWindowModel.agency == agency,
            CollectionWindowModel.start_date == start_date,
            CollectionWindowModel.end_date == end_date,
        )
        async with self._session_factory() as session:
            window = await session.scalar(statement)
            return self._to_domain(window) if window is not None else None

    async def save_window(self, window: CollectionWindow, *, counts_complete: bool = True) -> None:
        window = CollectionWindow.model_validate(window.model_dump())
        values = window.model_dump(exclude={"window_id"})
        async with self._session_factory.begin() as session:
            stored = await session.get(CollectionWindowModel, window.window_id)
            if stored is None:
                session.add(CollectionWindowModel(id=window.window_id, **values))
            else:
                for name, value in values.items():
                    setattr(stored, name, value)
            if window.attempts:
                await session.flush()
                attempt = _current_attempt(window, counts_complete=counts_complete)
                stored_attempt = await session.get(CollectionAttemptModel, attempt.attempt_id)
                values = attempt.model_dump(exclude={"attempt_id", "origin"})
                if stored_attempt is None:
                    session.add(
                        CollectionAttemptModel(id=attempt.attempt_id, origin="recorded", **values)
                    )
                else:
                    if stored_attempt.origin == "legacy_snapshot":
                        values["counts_complete"] = False
                    for name, value in values.items():
                        setattr(stored_attempt, name, value)

    async def list_windows(
        self,
        *,
        start_month: date,
        end_month: date,
    ) -> list[CollectionWindow]:
        statement = (
            select(CollectionWindowModel)
            .where(CollectionWindowModel.month_start.between(start_month, end_month))
            .order_by(
                CollectionWindowModel.month_start,
                CollectionWindowModel.agency,
                CollectionWindowModel.start_date,
                CollectionWindowModel.end_date,
            )
        )
        async with self._session_factory() as session:
            windows = (await session.scalars(statement)).all()
            return [self._to_domain(window) for window in windows]

    async def list_attempts(self, *, start_month: date, end_month: date) -> list[CollectionAttempt]:
        statement = (
            select(CollectionAttemptModel)
            .join(
                CollectionWindowModel, CollectionAttemptModel.window_id == CollectionWindowModel.id
            )
            .where(CollectionWindowModel.month_start.between(start_month, end_month))
            .order_by(CollectionAttemptModel.window_id, CollectionAttemptModel.attempt_number)
        )
        async with self._session_factory() as session:
            attempts = (await session.scalars(statement)).all()
            return [
                CollectionAttempt(
                    attempt_id=attempt.id,
                    window_id=attempt.window_id,
                    attempt_number=attempt.attempt_number,
                    status=cast(CollectionStatus, attempt.status),
                    pages_fetched=attempt.pages_fetched,
                    fetched_count=attempt.fetched_count,
                    relevant_count=attempt.relevant_count,
                    stored_count=attempt.stored_count,
                    duplicate_count=attempt.duplicate_count,
                    has_more=attempt.has_more,
                    error_message=attempt.error_message,
                    started_at=_utc_or_none(attempt.started_at),
                    completed_at=_utc_or_none(attempt.completed_at),
                    origin=cast(AttemptOrigin, attempt.origin),
                    counts_complete=attempt.counts_complete,
                )
                for attempt in attempts
            ]

    @asynccontextmanager
    async def collection_lock(self, *, source: str = "usaspending") -> AsyncGenerator[None, None]:
        if self._engine.dialect.name == "postgresql":
            identity = hashlib.sha256(f"orbital-signal:collection:{source}".encode()).digest()
            lock_key = int.from_bytes(identity[:8], byteorder="big", signed=True)
            async with self._engine.connect() as connection, connection.begin():
                acquired = await connection.scalar(
                    text("SELECT pg_try_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key}
                )
                if not acquired:
                    raise CollectionAlreadyRunningError(
                        "Another collection is already running for this database and source"
                    )
                yield
            return
        if self._engine.dialect.name != "sqlite":
            raise RuntimeError("Collection locking supports PostgreSQL and local SQLite")

        database = self._engine.url.database
        database_identity = (
            str(Path(database).expanduser().resolve())
            if database and database != ":memory:"
            else f"memory:{id(self._engine)}"
        )
        identity = hashlib.sha256(f"{database_identity}:{source}".encode()).hexdigest()
        lock_path = (
            Path(tempfile.gettempdir()) / f"orbital-collection-{os.getuid()}-{identity}.lock"
        )
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CollectionAlreadyRunningError(
                    "Another collection is already running for this database and source"
                ) from exc
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _to_domain(window: CollectionWindowModel) -> CollectionWindow:
        return CollectionWindow(
            window_id=window.id,
            source=window.source,
            agency=window.agency,
            month_start=window.month_start,
            start_date=window.start_date,
            end_date=window.end_date,
            status=cast(CollectionStatus, window.status),
            attempts=window.attempts,
            pages_fetched=window.pages_fetched,
            fetched_count=window.fetched_count,
            relevant_count=window.relevant_count,
            stored_count=window.stored_count,
            duplicate_count=window.duplicate_count,
            has_more=window.has_more,
            error_message=window.error_message,
            started_at=_utc_or_none(window.started_at),
            completed_at=_utc_or_none(window.completed_at),
        )
