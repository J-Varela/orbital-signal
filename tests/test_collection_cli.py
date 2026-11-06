import argparse
import json
from datetime import date
from types import SimpleNamespace
from typing import Literal, Self, Unpack
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from orbital_signal import cli
from orbital_signal.cli import CollectionPolicyOverrides
from orbital_signal.collection import CollectionSummary
from orbital_signal.collection_models import CollectionAlreadyRunningError
from orbital_signal.config import Settings
from orbital_signal.sources.usaspending import DEFAULT_AGENCIES


@pytest.fixture(autouse=True)
def fixed_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDate(date):
        @classmethod
        def today(cls) -> Self:
            return cls(2026, 11, 1)

    monkeypatch.setattr(cli, "date", FixedDate)


@pytest.fixture
def forbid_collection_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_io(*args: object, **kwargs: object) -> None:
        pytest.fail("This command must not initialize collection, settings, a database, or HTTP")

    monkeypatch.setattr(cli, "Settings", unexpected_io)
    monkeypatch.setattr(cli, "build_async_engine", unexpected_io)
    monkeypatch.setattr(cli, "build_http_client", unexpected_io)
    monkeypatch.setattr(cli, "build_usaspending_source", unexpected_io)
    monkeypatch.setattr(cli, "_collection_command", unexpected_io)


@pytest.mark.parametrize("arguments", [[], ["serve"]])
def test_default_and_explicit_serve_preserve_local_api_command(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
    forbid_collection_io: None,
) -> None:
    serve = Mock()
    monkeypatch.setattr(cli.uvicorn, "run", serve)

    cli.main(arguments)

    serve.assert_called_once_with(
        "orbital_signal.api:app", host="127.0.0.1", port=8000, reload=True
    )


@pytest.mark.parametrize("refresh", [False, True])
def test_backfill_plan_prints_calendar_windows_without_collection_io(
    refresh: bool,
    capsys: pytest.CaptureFixture[str],
    forbid_collection_io: None,
) -> None:
    arguments = [
        "collect-monthly",
        "--start-month",
        "2026-01",
        "--end-month",
        "2026-10",
        "--plan",
    ]
    if refresh:
        arguments.append("--refresh")

    cli.main(arguments)

    result = json.loads(capsys.readouterr().out)
    assert result["source"] == "usaspending"
    assert result["refresh"] is refresh
    assert "not guaranteed source completeness" in result["coverage_note"]
    assert result["window_days"] == 3
    assert result["agencies"] == list(DEFAULT_AGENCIES)
    assert result["policy_overrides"] == {}
    assert len(result["windows"]) == 212
    assert {window["agency"] for window in result["windows"]} == set(DEFAULT_AGENCIES)
    assert result["windows"][0]["start_date"] == "2026-01-01"
    assert result["windows"][0]["end_date"] == "2026-01-03"
    assert result["windows"][-1]["start_date"] == "2026-10-31"
    assert result["windows"][-1]["end_date"] == "2026-10-31"
    assert sum(window["end_date"] == "2026-02-28" for window in result["windows"]) == 2


@pytest.mark.parametrize(
    ("start_month", "end_month"),
    [
        ("2026-1", "2026-10"),
        ("2026-01-01", "2026-10"),
        ("2026-13", "2026-10"),
        ("2026-10", "2026-01"),
        ("2026-01", "2026-11"),
        ("2026-01", "2027-01"),
    ],
)
def test_invalid_or_unfinished_months_are_rejected_before_collection_io(
    start_month: str,
    end_month: str,
    forbid_collection_io: None,
) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(
            ["collect-monthly", "--start-month", start_month, "--end-month", end_month, "--plan"]
        )

    assert error.value.code == 2


@pytest.mark.parametrize("command", ["collect-monthly", "collection-status"])
def test_collection_requires_an_explicit_period(command: str, forbid_collection_io: None) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main([command])

    assert error.value.code == 2


def test_help_lists_commands_without_starting_anything(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    forbid_collection_io: None,
) -> None:
    serve = Mock()
    monkeypatch.setattr(cli.uvicorn, "run", serve)

    with pytest.raises(SystemExit) as error:
        cli.main(["--help"])

    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "serve" in help_text
    assert "collect-monthly" in help_text
    assert "collection-status" in help_text
    serve.assert_not_called()


@pytest.mark.parametrize("refresh", [False, True])
def test_collect_dispatches_selected_months_and_refresh_flag(
    refresh: bool,
    monkeypatch: pytest.MonkeyPatch,
    forbid_collection_io: None,
) -> None:
    received: list[argparse.Namespace] = []

    async def collect(arguments: argparse.Namespace) -> int:
        received.append(arguments)
        return 0

    monkeypatch.setattr(cli, "_collection_command", collect)
    arguments = ["collect-monthly", "--start-month", "2026-01", "--end-month", "2026-10"]
    if refresh:
        arguments.append("--refresh")

    with pytest.raises(SystemExit) as error:
        cli.main(arguments)

    assert error.value.code == 0
    [request] = received
    assert request.command == "collect-monthly"
    assert request.start_month == date(2026, 1, 1)
    assert request.end_month == date(2026, 10, 1)
    assert request.refresh is refresh
    assert request.plan is False
    assert request.start_date is None
    assert request.end_date is None
    assert request.window_days == 3
    assert request.agencies == DEFAULT_AGENCIES
    assert request.window_delay_seconds is None
    assert request.request_interval_seconds is None
    assert request.enrichment_concurrency is None
    assert request.max_consecutive_failures is None


@pytest.mark.parametrize(("status", "expected_exit"), [("completed", 0), ("incomplete", 2)])
def test_status_reads_progress_and_reports_completion_exit_code(
    status: Literal["completed", "incomplete"],
    expected_exit: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    summary = CollectionSummary(
        start_month=date(2026, 1, 1),
        end_month=date(2026, 10, 1),
        status=status,
        windows=[],
    )
    engine = SimpleNamespace(dispose=AsyncMock())
    settings = SimpleNamespace(database_url="sqlite+aiosqlite:///:memory:", database_echo=False)
    sessions = object()
    checkpoints = object()
    progress = AsyncMock(return_value=summary)
    http_client = Mock(side_effect=AssertionError("Status queries must not open an HTTP client"))
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "build_async_engine", Mock(return_value=engine))
    monkeypatch.setattr(cli, "build_session_factory", Mock(return_value=sessions))
    monkeypatch.setattr(cli, "SqlAlchemyCollectionRepository", Mock(return_value=checkpoints))
    monkeypatch.setattr(cli, "collection_progress", progress)
    monkeypatch.setattr(cli, "build_http_client", http_client)

    with pytest.raises(SystemExit) as error:
        cli.main(["collection-status", "--start-month", "2026-01", "--end-month", "2026-10"])

    assert error.value.code == expected_exit
    assert json.loads(capsys.readouterr().out)["status"] == status
    progress.assert_awaited_once_with(
        checkpoints=checkpoints,
        start_month=date(2026, 1, 1),
        end_month=date(2026, 10, 1),
        start_date=None,
        end_date=None,
        window_days=3,
        agencies=DEFAULT_AGENCIES,
    )
    engine.dispose.assert_awaited_once()
    http_client.assert_not_called()


def test_database_failure_has_nonzero_exit_without_internal_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    forbid_collection_io: None,
) -> None:
    async def fail(arguments: argparse.Namespace) -> int:
        raise SQLAlchemyError("private database diagnostic")

    monkeypatch.setattr(cli, "_collection_command", fail)

    with pytest.raises(SystemExit) as error:
        cli.main(["collection-status", "--start-month", "2026-01", "--end-month", "2026-10"])

    assert error.value.code == 1
    message = capsys.readouterr().err
    assert "Collection storage is unavailable" in message
    assert "private database diagnostic" not in message


@pytest.mark.parametrize(
    ("agency", "expected_agencies"),
    [
        ("nasa", DEFAULT_AGENCIES[:1]),
        ("dod", DEFAULT_AGENCIES[1:]),
        ("all", DEFAULT_AGENCIES),
    ],
)
def test_plan_only_contains_selected_agencies_and_three_day_windows(
    agency: str,
    expected_agencies: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
    forbid_collection_io: None,
) -> None:
    cli.main(
        [
            "collect-monthly",
            "--start-month",
            "2026-01",
            "--end-month",
            "2026-01",
            "--agency",
            agency,
            "--plan",
        ]
    )

    result = json.loads(capsys.readouterr().out)

    assert result["agencies"] == list(expected_agencies)
    assert len(result["windows"]) == 11 * len(expected_agencies)
    assert {window["agency"] for window in result["windows"]} == set(expected_agencies)
    nasa_or_dod = [
        (window["start_date"], window["end_date"])
        for window in result["windows"]
        if window["agency"] == expected_agencies[0]
    ]
    assert nasa_or_dod[:2] == [("2026-01-01", "2026-01-03"), ("2026-01-04", "2026-01-06")]
    assert nasa_or_dod[-1] == ("2026-01-31", "2026-01-31")


def test_explicit_date_plan_splits_at_month_boundaries(
    capsys: pytest.CaptureFixture[str],
    forbid_collection_io: None,
) -> None:
    cli.main(
        [
            "collect-monthly",
            "--start-date",
            "2026-01-30",
            "--end-date",
            "2026-02-05",
            "--window-days",
            "3",
            "--agency",
            "nasa",
            "--plan",
        ]
    )

    result = json.loads(capsys.readouterr().out)

    assert result["windows"] == [
        {"agency": DEFAULT_AGENCIES[0], "start_date": "2026-01-30", "end_date": "2026-01-31"},
        {"agency": DEFAULT_AGENCIES[0], "start_date": "2026-02-01", "end_date": "2026-02-03"},
        {"agency": DEFAULT_AGENCIES[0], "start_date": "2026-02-04", "end_date": "2026-02-05"},
    ]


def test_explicit_dates_allow_yesterday_in_the_current_month(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    forbid_collection_io: None,
) -> None:
    class NextDay(date):
        @classmethod
        def today(cls) -> Self:
            return cls(2026, 11, 2)

    monkeypatch.setattr(cli, "date", NextDay)

    cli.main(
        [
            "collect-monthly",
            "--start-date",
            "2026-11-01",
            "--end-date",
            "2026-11-01",
            "--agency",
            "nasa",
            "--plan",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert result["windows"] == [
        {"agency": DEFAULT_AGENCIES[0], "start_date": "2026-11-01", "end_date": "2026-11-01"}
    ]


def test_larger_window_size_keeps_month_boundaries(
    capsys: pytest.CaptureFixture[str],
    forbid_collection_io: None,
) -> None:
    cli.main(
        [
            "collect-monthly",
            "--start-month",
            "2026-01",
            "--end-month",
            "2026-10",
            "--window-days",
            "31",
            "--plan",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert result["window_days"] == 31
    assert len(result["windows"]) == 20
    assert result["windows"][0]["end_date"] == "2026-01-31"
    assert result["windows"][-1]["start_date"] == "2026-10-01"


@pytest.mark.parametrize(
    "selection",
    [
        ["--start-month", "2026-01"],
        ["--end-month", "2026-01"],
        ["--start-date", "2026-01-01"],
        ["--end-date", "2026-01-02"],
        ["--start-month", "2026-01", "--end-date", "2026-01-02"],
        [
            "--start-month",
            "2026-01",
            "--end-month",
            "2026-01",
            "--start-date",
            "2026-01-01",
            "--end-date",
            "2026-01-02",
        ],
        ["--start-date", "2026-02-30", "--end-date", "2026-03-01"],
        ["--start-date", "20260101", "--end-date", "2026-01-02"],
        ["--start-date", "2026-W01-1", "--end-date", "2026-01-02"],
        ["--start-date", "2026-01-02", "--end-date", "2026-01-01"],
        ["--start-date", "2026-10-31", "--end-date", "2026-11-01"],
        ["--start-date", "2026-11-02", "--end-date", "2026-11-02"],
    ],
)
def test_incomplete_mixed_invalid_or_future_date_selection_is_rejected_without_io(
    selection: list[str],
    forbid_collection_io: None,
) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["collect-monthly", *selection, "--plan"])

    assert error.value.code == 2


@pytest.mark.parametrize("window_days", ["0", "-1", "1.5", "many"])
def test_window_size_must_be_a_positive_integer(
    window_days: str,
    forbid_collection_io: None,
) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "collect-monthly",
                "--start-month",
                "2026-01",
                "--end-month",
                "2026-01",
                "--window-days",
                window_days,
                "--plan",
            ]
        )

    assert error.value.code == 2


def test_unknown_agency_is_rejected_without_io(forbid_collection_io: None) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "collect-monthly",
                "--start-month",
                "2026-01",
                "--end-month",
                "2026-01",
                "--agency",
                "doe",
                "--plan",
            ]
        )

    assert error.value.code == 2


def test_explicit_dates_agency_and_window_size_are_forwarded(
    monkeypatch: pytest.MonkeyPatch,
    forbid_collection_io: None,
) -> None:
    received: list[argparse.Namespace] = []

    async def collect(arguments: argparse.Namespace) -> int:
        received.append(arguments)
        return 0

    monkeypatch.setattr(cli, "_collection_command", collect)

    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "collect-monthly",
                "--start-date",
                "2026-01-30",
                "--end-date",
                "2026-02-02",
                "--window-days",
                "1",
                "--agency",
                "dod",
                "--refresh",
            ]
        )

    assert error.value.code == 0
    [request] = received
    assert request.start_month is request.end_month is None
    assert request.start_date == date(2026, 1, 30)
    assert request.end_date == date(2026, 2, 2)
    assert request.agencies == DEFAULT_AGENCIES[1:]
    assert request.window_days == 1
    assert request.refresh is True


def test_active_collector_reports_a_nonzero_exit_with_retry_guidance(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    forbid_collection_io: None,
) -> None:
    async def already_running(arguments: argparse.Namespace) -> int:
        raise CollectionAlreadyRunningError("internal lock identity")

    monkeypatch.setattr(cli, "_collection_command", already_running)

    with pytest.raises(SystemExit) as error:
        cli.main(["collect-monthly", "--start-month", "2026-01", "--end-month", "2026-01"])

    assert error.value.code == 1
    message = capsys.readouterr().err
    assert "already running" in message
    assert "Wait for it to finish" in message
    assert "internal lock identity" not in message


def test_collection_uses_shared_http_policy_and_forwards_date_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = CollectionSummary(
        start_month=date(2026, 1, 1),
        end_month=date(2026, 2, 1),
        status="completed",
        windows=[],
    )
    settings = SimpleNamespace(
        database_url="sqlite+aiosqlite:///:memory:",
        database_echo=False,
        collection_window_delay_seconds=5.0,
        collection_max_consecutive_failures=3,
    )
    engine = SimpleNamespace(dispose=AsyncMock())
    sessions, checkpoints, repository, source, client = (object() for _ in range(5))
    context = AsyncMock()
    context.__aenter__.return_value = client
    http_policy = Mock(return_value=context)
    source_policy = Mock(return_value=source)
    collect = AsyncMock(return_value=summary)
    service = Mock(return_value=SimpleNamespace(collect=collect))
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "build_async_engine", Mock(return_value=engine))
    monkeypatch.setattr(cli, "build_session_factory", Mock(return_value=sessions))
    monkeypatch.setattr(cli, "SqlAlchemyCollectionRepository", Mock(return_value=checkpoints))
    monkeypatch.setattr(cli, "SqlAlchemySignalRepository", Mock(return_value=repository))
    monkeypatch.setattr(cli, "build_http_client", http_policy)
    monkeypatch.setattr(cli, "build_usaspending_source", source_policy)
    monkeypatch.setattr(cli, "MonthlyCollectionService", service)

    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "collect-monthly",
                "--start-date",
                "2026-01-30",
                "--end-date",
                "2026-02-02",
                "--window-days",
                "1",
                "--agency",
                "dod",
                "--refresh",
            ]
        )

    assert error.value.code == 0
    http_policy.assert_called_once_with(settings)
    source_policy.assert_called_once_with(client, settings)
    service.assert_called_once_with(
        source=source,
        repository=repository,
        checkpoints=checkpoints,
        window_delay_seconds=settings.collection_window_delay_seconds,
        max_consecutive_failures=settings.collection_max_consecutive_failures,
    )
    collect.assert_awaited_once_with(
        start_month=None,
        end_month=None,
        start_date=date(2026, 1, 30),
        end_date=date(2026, 2, 2),
        window_days=1,
        agencies=DEFAULT_AGENCIES[1:],
        refresh=True,
    )
    context.__aexit__.assert_awaited_once()
    engine.dispose.assert_awaited_once()


@pytest.mark.parametrize(
    ("arguments", "expected_overrides"),
    [
        (
            [
                "--window-delay-seconds",
                "0",
                "--request-interval-seconds",
                "0.125",
                "--enrichment-concurrency",
                "1",
                "--max-consecutive-failures",
                "2",
            ],
            {
                "collection_window_delay_seconds": 0.0,
                "http_request_interval_seconds": 0.125,
                "enrichment_concurrency": 1,
                "collection_max_consecutive_failures": 2,
            },
        ),
        (
            [
                "--window-delay-seconds",
                "7.5",
                "--request-interval-seconds",
                "0",
                "--enrichment-concurrency",
                "10",
                "--max-consecutive-failures",
                "1",
            ],
            {
                "collection_window_delay_seconds": 7.5,
                "http_request_interval_seconds": 0.0,
                "enrichment_concurrency": 10,
                "collection_max_consecutive_failures": 1,
            },
        ),
        (["--window-delay-seconds", "0"], {"collection_window_delay_seconds": 0.0}),
    ],
)
def test_plan_reports_explicit_policy_overrides_without_loading_settings(
    arguments: list[str],
    expected_overrides: dict[str, int | float],
    capsys: pytest.CaptureFixture[str],
    forbid_collection_io: None,
) -> None:
    cli.main(
        [
            "collect-monthly",
            "--start-month",
            "2026-01",
            "--end-month",
            "2026-01",
            "--plan",
            *arguments,
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert result["policy_overrides"] == expected_overrides
    assert "Environment" in result["policy_note"]
    assert "when collection runs" in result["policy_note"]
    assert len(result["windows"]) == 22


@pytest.mark.parametrize(
    ("option", "value"),
    [
        (option, value)
        for option in ("--window-delay-seconds", "--request-interval-seconds")
        for value in ("-1", "nan", "inf", "-inf", "1e999", "not-a-number")
    ]
    + [("--enrichment-concurrency", value) for value in ("0", "-1", "11", "1.5", "many")]
    + [("--max-consecutive-failures", value) for value in ("0", "-1", "1.5", "many")],
)
def test_invalid_policy_overrides_are_rejected_before_any_io(
    option: str, value: str, forbid_collection_io: None
) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "collect-monthly",
                "--start-month",
                "2026-01",
                "--end-month",
                "2026-01",
                "--plan",
                f"{option}={value}",
            ]
        )

    assert error.value.code == 2


@pytest.mark.parametrize(
    ("arguments", "expected_overrides", "expected_policy"),
    [
        (
            ["--window-delay-seconds", "0"],
            {"collection_window_delay_seconds": 0.0},
            (0.0, 1.5, 4, 6),
        ),
        (
            [
                "--window-delay-seconds",
                "2",
                "--request-interval-seconds",
                "0.25",
                "--enrichment-concurrency",
                "1",
                "--max-consecutive-failures",
                "2",
            ],
            {
                "collection_window_delay_seconds": 2.0,
                "http_request_interval_seconds": 0.25,
                "enrichment_concurrency": 1,
                "collection_max_consecutive_failures": 2,
            },
            (2.0, 0.25, 1, 2),
        ),
    ],
)
def test_cli_policy_overrides_take_precedence_while_preserving_other_environment_settings(
    arguments: list[str],
    expected_overrides: dict[str, int | float],
    expected_policy: tuple[float, float, int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in (
        ("COLLECTION_WINDOW_DELAY_SECONDS", "8"),
        ("HTTP_REQUEST_INTERVAL_SECONDS", "1.5"),
        ("ENRICHMENT_CONCURRENCY", "4"),
        ("COLLECTION_MAX_CONSECUTIVE_FAILURES", "6"),
        ("DATABASE_URL", "sqlite+aiosqlite:///:memory:"),
    ):
        monkeypatch.setenv(f"ORBITAL_SIGNAL_{name}", value)
    resolved: list[Settings] = []
    received_overrides: list[CollectionPolicyOverrides] = []

    def configure(**overrides: Unpack[CollectionPolicyOverrides]) -> Settings:
        received_overrides.append(overrides)
        settings = Settings(_env_file=None, **overrides)
        resolved.append(settings)
        return settings

    summary = CollectionSummary(
        start_month=date(2026, 1, 1),
        end_month=date(2026, 1, 1),
        status="completed",
        windows=[],
    )
    engine = SimpleNamespace(dispose=AsyncMock())
    context = AsyncMock()
    source, client = object(), object()
    context.__aenter__.return_value = client
    http_policy = Mock(return_value=context)
    source_policy = Mock(return_value=source)
    service = Mock(return_value=SimpleNamespace(collect=AsyncMock(return_value=summary)))
    monkeypatch.setattr(cli, "Settings", configure)
    monkeypatch.setattr(cli, "build_async_engine", Mock(return_value=engine))
    monkeypatch.setattr(cli, "build_session_factory", Mock(return_value=object()))
    monkeypatch.setattr(cli, "SqlAlchemyCollectionRepository", Mock(return_value=object()))
    monkeypatch.setattr(cli, "SqlAlchemySignalRepository", Mock(return_value=object()))
    monkeypatch.setattr(cli, "build_http_client", http_policy)
    monkeypatch.setattr(cli, "build_usaspending_source", source_policy)
    monkeypatch.setattr(cli, "MonthlyCollectionService", service)

    with pytest.raises(SystemExit) as error:
        cli.main(
            ["collect-monthly", "--start-month", "2026-01", "--end-month", "2026-01", *arguments]
        )

    assert error.value.code == 0
    assert received_overrides == [expected_overrides]
    [settings] = resolved
    assert (
        settings.collection_window_delay_seconds,
        settings.http_request_interval_seconds,
        settings.enrichment_concurrency,
        settings.collection_max_consecutive_failures,
    ) == expected_policy
    http_policy.assert_called_once_with(settings)
    source_policy.assert_called_once_with(client, settings)
    assert service.call_args.kwargs["window_delay_seconds"] == expected_policy[0]
    assert service.call_args.kwargs["max_consecutive_failures"] == expected_policy[3]
    engine.dispose.assert_awaited_once()
