"""FastAPI application for Orbital Signal."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date

import httpx
from fastapi import FastAPI, HTTPException, Query

from orbital_signal import __version__
from orbital_signal.config import Settings
from orbital_signal.database import build_async_engine, build_session_factory
from orbital_signal.domain import CompanySignal, IngestionResult
from orbital_signal.repository import SignalRepository
from orbital_signal.services import AwardIngestionService
from orbital_signal.sources.usaspending import USAspendingClient
from orbital_signal.sql_repository import SqlAlchemySignalRepository


def create_app(*, repository: SignalRepository | None = None) -> FastAPI:
    settings = Settings()
    engine = None

    if repository is None:
        engine = build_async_engine(
            settings.database_url,
            echo=settings.database_echo,
        )
        session_factory = build_session_factory(engine)
        signal_repository: SignalRepository = SqlAlchemySignalRepository(
            session_factory,
        )
    else:
        signal_repository = repository

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield

        if engine is not None:
            await engine.dispose()

    application = FastAPI(
        title="Orbital Signal API",
        description="Early-warning intelligence for emerging space companies.",
        version=__version__,
        lifespan=lifespan,
    )
    application.state.repository = signal_repository
    application.state.settings = settings
    application.state.database_engine = engine

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @application.get("/api/v1/signals", response_model=list[CompanySignal])
    async def list_signals(
        minimum_score: int = Query(default=4, ge=0, le=100),
        limit: int = Query(default=100, ge=1, le=500),
        startup_candidates_only: bool = Query(default=False),
    ) -> list[CompanySignal]:
        return await signal_repository.list(
            minimum_score=minimum_score,
            limit=limit,
            startup_candidates_only=startup_candidates_only,
        )

    @application.post(
        "/api/v1/ingestions/usaspending",
        response_model=IngestionResult,
    )
    async def ingest_usaspending(start_date: date, end_date: date) -> IngestionResult:
        if end_date < start_date:
            raise HTTPException(
                status_code=422,
                detail="end_date must be on or after start_date",
            )

        try:
            async with httpx.AsyncClient(
                timeout=settings.http_timeout_seconds,
                headers={"User-Agent": f"orbital-signal/{__version__}"},
            ) as client:
                source = USAspendingClient(
                    client,
                    base_url=settings.usaspending_base_url,
                )
                service = AwardIngestionService(
                    source=source,
                    repository=signal_repository,
                )
                return await service.ingest(
                    start_date=start_date,
                    end_date=end_date,
                )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail="USAspending request failed",
            ) from exc

    return application


app = create_app()
