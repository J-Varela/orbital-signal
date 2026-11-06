"""Durable collection checkpoints, separate from award classification."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field, model_validator

CollectionStatus = Literal["pending", "running", "completed", "failed", "incomplete", "split"]
AttemptOrigin = Literal["recorded", "legacy_snapshot"]


class CollectionAlreadyRunningError(RuntimeError):
    """Another process already owns this database's source collection lock."""


def _normalized_timestamps(
    started_at: datetime | None, completed_at: datetime | None, status: CollectionStatus
) -> tuple[datetime | None, datetime | None]:
    def utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    started_at, completed_at = utc(started_at), utc(completed_at)
    if status == "running":
        completed_at = None
    elif started_at is not None and completed_at is not None:
        completed_at = max(started_at, completed_at)
    return started_at, completed_at


class CollectionWindow(BaseModel):
    """The latest attempt for one agency and bounded source date window.

    Counts describe this window's latest attempt, rather than unique awards
    across the backfill. Split parents remain available for coverage inspection.
    """

    window_id: str
    source: str = "usaspending"
    agency: str
    month_start: date
    start_date: date
    end_date: date
    status: CollectionStatus = "pending"
    attempts: int = Field(default=0, ge=0)
    pages_fetched: int = Field(default=0, ge=0)
    fetched_count: int = Field(default=0, ge=0)
    relevant_count: int = Field(default=0, ge=0)
    stored_count: int = Field(default=0, ge=0)
    duplicate_count: int = Field(default=0, ge=0)
    has_more: bool | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> CollectionWindow:
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if self.month_start.day != 1:
            raise ValueError("month_start must be the first day of a month")
        expected_month = (self.month_start.year, self.month_start.month)
        if any(
            (value.year, value.month) != expected_month
            for value in (self.start_date, self.end_date)
        ):
            raise ValueError("collection window must stay within month_start's month")
        self.started_at, self.completed_at = _normalized_timestamps(
            self.started_at, self.completed_at, self.status
        )
        return self


class CollectionAttempt(BaseModel):
    """One retained attempt's latest checkpoint, including partial outcomes."""

    attempt_id: str
    window_id: str
    attempt_number: int = Field(ge=1)
    status: CollectionStatus
    pages_fetched: int = Field(default=0, ge=0)
    fetched_count: int = Field(default=0, ge=0)
    relevant_count: int = Field(default=0, ge=0)
    stored_count: int = Field(default=0, ge=0)
    duplicate_count: int = Field(default=0, ge=0)
    has_more: bool | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    origin: AttemptOrigin = "recorded"
    counts_complete: bool = True

    @model_validator(mode="after")
    def normalize_timestamps(self) -> CollectionAttempt:
        self.started_at, self.completed_at = _normalized_timestamps(
            self.started_at, self.completed_at, self.status
        )
        return self


class CollectionRepository(Protocol):
    """Checkpoint storage independent of evidence and signal persistence."""

    async def get_window(
        self,
        *,
        source: str,
        agency: str,
        start_date: date,
        end_date: date,
    ) -> CollectionWindow | None: ...

    async def save_window(
        self, window: CollectionWindow, *, counts_complete: bool = True
    ) -> None: ...

    async def list_windows(
        self,
        *,
        start_month: date,
        end_month: date,
    ) -> list[CollectionWindow]: ...

    async def list_attempts(
        self,
        *,
        start_month: date,
        end_month: date,
    ) -> list[CollectionAttempt]: ...

    def collection_lock(
        self, *, source: str = "usaspending"
    ) -> AbstractAsyncContextManager[None]: ...
