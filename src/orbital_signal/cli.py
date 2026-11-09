"""Local API serving and explicit historical collection commands."""

import argparse
import asyncio
import json
import math
from datetime import date
from typing import TypedDict

import uvicorn
from sqlalchemy.exc import SQLAlchemyError

from orbital_signal.collection import (
    COVERAGE_NOTE,
    MonthlyCollectionService,
    collection_progress,
    parse_month,
    selection_windows,
)
from orbital_signal.collection_models import CollectionAlreadyRunningError
from orbital_signal.collection_repository import SqlAlchemyCollectionRepository
from orbital_signal.config import Settings
from orbital_signal.database import build_async_engine, build_session_factory
from orbital_signal.http_client import build_http_client, build_usaspending_source
from orbital_signal.sources.usaspending import DEFAULT_AGENCIES
from orbital_signal.sql_repository import SqlAlchemySignalRepository


class CollectionPolicyOverrides(TypedDict, total=False):
    collection_window_delay_seconds: float
    http_request_interval_seconds: float
    enrichment_concurrency: int
    collection_max_consecutive_failures: int


def _positive_integer(value: str) -> int:
    try:
        days = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Must be a positive whole number") from exc
    if days < 1:
        raise argparse.ArgumentTypeError("Must be a positive whole number")
    return days


def _nonnegative_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Seconds must be a finite value of at least zero") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("Seconds must be a finite value of at least zero")
    return seconds


def _enrichment_concurrency(value: str) -> int:
    concurrency = _positive_integer(value)
    if concurrency > 10:
        raise argparse.ArgumentTypeError("Enrichment concurrency must be between 1 and 10")
    return concurrency


def _settings_overrides(arguments: argparse.Namespace) -> CollectionPolicyOverrides:
    overrides: CollectionPolicyOverrides = {}
    if (value := getattr(arguments, "window_delay_seconds", None)) is not None:
        overrides["collection_window_delay_seconds"] = value
    if (value := getattr(arguments, "request_interval_seconds", None)) is not None:
        overrides["http_request_interval_seconds"] = value
    if (value := getattr(arguments, "enrichment_concurrency", None)) is not None:
        overrides["enrichment_concurrency"] = value
    if (value := getattr(arguments, "max_consecutive_failures", None)) is not None:
        overrides["collection_max_consecutive_failures"] = value
    return overrides


def _parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Dates must use a valid YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("Dates must use YYYY-MM-DD")
    return parsed


async def _collection_command(arguments: argparse.Namespace) -> int:
    overrides = _settings_overrides(arguments)
    settings = Settings(**overrides) if overrides else Settings()
    engine = build_async_engine(settings.database_url, echo=settings.database_echo)
    sessions = build_session_factory(engine)
    checkpoints = SqlAlchemyCollectionRepository(sessions)
    try:
        if arguments.command == "collection-status":
            result = await collection_progress(
                checkpoints=checkpoints,
                start_month=arguments.start_month,
                end_month=arguments.end_month,
                start_date=arguments.start_date,
                end_date=arguments.end_date,
                window_days=arguments.window_days,
                agencies=arguments.agencies,
            )
        else:
            async with build_http_client(settings) as client:
                result = await MonthlyCollectionService(
                    source=build_usaspending_source(client, settings),
                    repository=SqlAlchemySignalRepository(sessions),
                    checkpoints=checkpoints,
                    window_delay_seconds=settings.collection_window_delay_seconds,
                    max_consecutive_failures=settings.collection_max_consecutive_failures,
                ).collect(
                    start_month=arguments.start_month,
                    end_month=arguments.end_month,
                    start_date=arguments.start_date,
                    end_date=arguments.end_date,
                    window_days=arguments.window_days,
                    agencies=arguments.agencies,
                    refresh=arguments.refresh,
                )
        print(result.model_dump_json(indent=2))
        return 0 if result.status == "completed" else 2
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Orbital Signal local workspace")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="Run the local dashboard and API (also the default)")
    for name, help_text in (
        ("collect-monthly", "Collect or resume historical NASA and DoD award windows"),
        ("collection-status", "Read persisted monthly collection progress"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--start-month", type=parse_month, metavar="YYYY-MM")
        command.add_argument("--end-month", type=parse_month, metavar="YYYY-MM")
        command.add_argument("--start-date", type=_parse_date, metavar="YYYY-MM-DD")
        command.add_argument("--end-date", type=_parse_date, metavar="YYYY-MM-DD")
        command.add_argument(
            "--window-days",
            type=_positive_integer,
            default=3,
            help="Maximum days per initial search window (default: 3)",
        )
        command.add_argument(
            "--agency",
            choices=("nasa", "dod", "all"),
            default="all",
            help="Awarding agency to collect or inspect (default: all)",
        )
        if name == "collect-monthly":
            command.add_argument(
                "--window-delay-seconds",
                type=_nonnegative_seconds,
                help="Delay between collection windows; overrides environment configuration",
            )
            command.add_argument(
                "--request-interval-seconds",
                type=_nonnegative_seconds,
                help="Minimum spacing between upstream request starts; overrides configuration",
            )
            command.add_argument(
                "--enrichment-concurrency",
                type=_enrichment_concurrency,
                help="Concurrent transaction enrichment requests (1-10); overrides configuration",
            )
            command.add_argument(
                "--max-consecutive-failures",
                type=_positive_integer,
                help="Stop after this many consecutive failed windows; overrides configuration",
            )
            command.add_argument("--refresh", action="store_true", help="Revisit completed windows")
            command.add_argument(
                "--plan",
                action="store_true",
                help="Print the date plan without any source or database access",
            )
    arguments = parser.parse_args(argv)
    if arguments.command in {None, "serve"}:
        uvicorn.run("orbital_signal.api:app", host="127.0.0.1", port=8000, reload=True)
        return
    try:
        windows = selection_windows(
            start_month=arguments.start_month,
            end_month=arguments.end_month,
            start_date=arguments.start_date,
            end_date=arguments.end_date,
            window_days=arguments.window_days,
        )
        if arguments.end_month is not None and arguments.end_month >= date.today().replace(day=1):
            raise ValueError("Only completed calendar months can be collected")
        if arguments.end_date is not None and arguments.end_date >= date.today():
            raise ValueError("Only dates before today can be collected")
    except ValueError as exc:
        parser.error(str(exc))
    arguments.agencies = {
        "nasa": (DEFAULT_AGENCIES[0],),
        "dod": (DEFAULT_AGENCIES[1],),
        "all": DEFAULT_AGENCIES,
    }[arguments.agency]
    if arguments.command == "collect-monthly" and arguments.plan:
        print(
            json.dumps(
                {
                    "source": "usaspending",
                    "windows": [
                        {
                            "agency": agency,
                            "start_date": start.isoformat(),
                            "end_date": end.isoformat(),
                        }
                        for start, end in windows
                        for agency in arguments.agencies
                    ],
                    "window_days": arguments.window_days,
                    "agencies": arguments.agencies,
                    "refresh": arguments.refresh,
                    "policy_overrides": _settings_overrides(arguments),
                    "policy_note": (
                        "Only explicit CLI overrides are shown. Environment and .env policy "
                        "settings are resolved when collection runs."
                    ),
                    "coverage_note": COVERAGE_NOTE,
                },
                indent=2,
            )
        )
        return
    try:
        code = asyncio.run(_collection_command(arguments))
    except CollectionAlreadyRunningError:
        parser.exit(
            1,
            "Another collection is already running for this database. "
            "Wait for it to finish before retrying.\n",
        )
    except SQLAlchemyError:
        parser.exit(
            1,
            "Collection storage is unavailable; review database settings and migrations.\n",
        )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
