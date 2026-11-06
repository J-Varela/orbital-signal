"""FastAPI application for Orbital Signal."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from orbital_signal import __version__
from orbital_signal.collection import (
    CollectionSummary,
    MonthlyCollectionService,
    collection_progress,
    parse_month,
    selection_windows,
)
from orbital_signal.collection_models import CollectionAlreadyRunningError, CollectionRepository
from orbital_signal.collection_repository import (
    InMemoryCollectionRepository,
    SqlAlchemyCollectionRepository,
)
from orbital_signal.config import Settings
from orbital_signal.database import build_async_engine, build_session_factory
from orbital_signal.domain import (
    CompanyIntelligenceProfile,
    CompanySignal,
    IngestionResult,
    SignalReclassificationResult,
)
from orbital_signal.http_client import build_http_client, build_usaspending_source
from orbital_signal.repository import SignalRepository
from orbital_signal.services import (
    AwardIngestionService,
    SignalReclassificationService,
)
from orbital_signal.sources.usaspending import DEFAULT_AGENCIES, SourceDataError
from orbital_signal.sql_repository import SqlAlchemySignalRepository


def create_app(
    *,
    repository: SignalRepository | None = None,
    collection_repository: CollectionRepository | None = None,
) -> FastAPI:
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
        checkpoints: CollectionRepository = collection_repository or SqlAlchemyCollectionRepository(
            session_factory
        )
    else:
        signal_repository = repository
        checkpoints = collection_repository or InMemoryCollectionRepository()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
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
    application.state.collection_repository = checkpoints
    collection_lock = asyncio.Lock()
    application.state.collection_lock = collection_lock

    static_directory = Path(__file__).parent / "static"
    application.mount("/static", StaticFiles(directory=static_directory), name="static")

    @application.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(
            static_directory / "index.html",
            headers={"Cache-Control": "no-cache"},
        )

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @application.get("/api/v1/signals", response_model=list[CompanySignal])
    async def list_signals(
        minimum_score: int = Query(default=4, ge=0, le=100),
        minimum_priority_score: int = Query(default=0, ge=0, le=100),
        limit: int = Query(default=100, ge=1, le=500),
        startup_candidates_only: bool = Query(default=False),
    ) -> list[CompanySignal]:
        return await signal_repository.list(
            minimum_score=minimum_score,
            minimum_priority_score=minimum_priority_score,
            limit=limit,
            startup_candidates_only=startup_candidates_only,
        )

    @application.get(
        "/api/v1/companies",
        response_model=list[CompanyIntelligenceProfile],
    )
    async def list_companies(
        limit: int = Query(default=100, ge=1, le=500),
        startup_candidates_only: bool = Query(default=False),
    ) -> list[CompanyIntelligenceProfile]:
        return await signal_repository.list_company_profiles(
            limit=limit,
            startup_candidates_only=startup_candidates_only,
        )

    def collection_selection(
        start_month: str | None,
        end_month: str | None,
        start_date: date | None,
        end_date: date | None,
        window_days: int,
        agency: Literal["nasa", "dod", "all"],
    ) -> tuple[date | None, date | None, tuple[str, ...]]:
        try:
            start = parse_month(start_month) if start_month is not None else None
            end = parse_month(end_month) if end_month is not None else None
            selection_windows(
                start_month=start,
                end_month=end,
                start_date=start_date,
                end_date=end_date,
                window_days=window_days,
            )
            if end is not None and end >= date.today().replace(day=1):
                raise ValueError("Only completed calendar months can be collected")
            if end_date is not None and end_date >= date.today():
                raise ValueError("Only dates before today can be collected")
            agencies = {
                "nasa": (DEFAULT_AGENCIES[0],),
                "dod": (DEFAULT_AGENCIES[1],),
                "all": DEFAULT_AGENCIES,
            }[agency]
            return start, end, agencies
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.get("/api/v1/collections/monthly", response_model=CollectionSummary)
    async def monthly_collection_progress(
        start_month: str | None = None,
        end_month: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        window_days: int = Query(default=3, ge=1),
        agency: Literal["nasa", "dod", "all"] = "all",
    ) -> CollectionSummary:
        start, end, agencies = collection_selection(
            start_month, end_month, start_date, end_date, window_days, agency
        )
        try:
            return await collection_progress(
                checkpoints=checkpoints,
                start_month=start,
                end_month=end,
                start_date=start_date,
                end_date=end_date,
                window_days=window_days,
                agencies=agencies,
            )
        except SQLAlchemyError as exc:
            raise HTTPException(
                status_code=503,
                detail="Collection storage unavailable; review settings and migrations.",
            ) from exc

    @application.post("/api/v1/collections/monthly", response_model=CollectionSummary)
    async def collect_monthly(
        start_month: str | None = None,
        end_month: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        window_days: int = Query(default=3, ge=1),
        agency: Literal["nasa", "dod", "all"] = "all",
        refresh: bool = False,
    ) -> CollectionSummary:
        start, end, agencies = collection_selection(
            start_month, end_month, start_date, end_date, window_days, agency
        )
        if collection_lock.locked():
            raise HTTPException(status_code=409, detail="A monthly collection is already running")
        try:
            async with collection_lock, build_http_client(settings) as client:
                return await MonthlyCollectionService(
                    source=build_usaspending_source(client, settings),
                    repository=signal_repository,
                    checkpoints=checkpoints,
                    window_delay_seconds=settings.collection_window_delay_seconds,
                    max_consecutive_failures=settings.collection_max_consecutive_failures,
                ).collect(
                    start_month=start,
                    end_month=end,
                    start_date=start_date,
                    end_date=end_date,
                    window_days=window_days,
                    agencies=agencies,
                    refresh=refresh,
                )
        except CollectionAlreadyRunningError as exc:
            raise HTTPException(
                status_code=409, detail="Another collector is already running for this database"
            ) from exc
        except SQLAlchemyError as exc:
            raise HTTPException(
                status_code=503,
                detail="Collection storage unavailable; review settings and migrations.",
            ) from exc

    @application.post(
        "/api/v1/reclassifications/signals",
        response_model=SignalReclassificationResult,
    )
    async def reclassify_signals(
        as_of: Annotated[date | None, Query()] = None,
    ) -> SignalReclassificationResult:
        service = SignalReclassificationService(
            repository=signal_repository,
        )
        return await service.reclassify(
            as_of=as_of or date.today(),
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
            async with build_http_client(settings) as client:
                source = build_usaspending_source(client, settings)
                service = AwardIngestionService(
                    source=source,
                    repository=signal_repository,
                )
                return await service.ingest(
                    start_date=start_date,
                    end_date=end_date,
                )
        except (httpx.HTTPError, SourceDataError) as exc:
            raise HTTPException(
                status_code=502,
                detail="USAspending request failed",
            ) from exc

    return application


app = create_app()
