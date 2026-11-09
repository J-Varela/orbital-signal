import json
import stat
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from orbital_signal import source_cache
from orbital_signal.config import Settings
from orbital_signal.domain import AwardRecord
from orbital_signal.http_client import build_usaspending_source
from orbital_signal.source_cache import PendingEnrichmentCache
from orbital_signal.sources.usaspending import (
    DEFAULT_AGENCIES,
    SEARCH_PATH,
    TRANSACTIONS_PATH,
    SourceDataError,
    USAspendingClient,
)

NASA, DOD = DEFAULT_AGENCIES
START, END = date(2026, 1, 1), date(2026, 1, 3)
BASE_URL = "https://cache-user:cache-secret@api.usaspending.test"


def award_row(identifier: str) -> dict[str, Any]:
    return {
        "Award ID": identifier,
        "generated_internal_id": identifier,
        "Recipient Name": "Example Space LLC",
        "Award Amount": 100_000,
        "Awarding Agency": NASA,
        "Description": "Satellite development.",
    }


def make_source(
    client: httpx.AsyncClient, directory: Path | None, **overrides: Any
) -> USAspendingClient:
    settings: dict[str, Any] = {
        "base_url": BASE_URL,
        "retry_attempts": 1,
        "enrichment_concurrency": 1,
        "enrichment_cache_directory": directory,
    }
    return USAspendingClient(client, **(settings | overrides))


async def get_page(source: USAspendingClient, **overrides: Any):
    arguments: dict[str, Any] = {
        "start_date": START,
        "end_date": END,
        "agency": NASA,
    }
    return await source.search_awards_page(**(arguments | overrides))


@pytest.mark.parametrize("known_date", [None, "2026-01-02"])
async def test_pending_search_and_successful_lookups_survive_client_and_process_replacement(
    tmp_path: Path, known_date: str | None
) -> None:
    searches = 0
    lookups: list[str] = []
    fail = True

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal searches
        body = json.loads(request.content)
        if request.url.path == SEARCH_PATH:
            searches += 1
            return httpx.Response(
                200,
                json={
                    "results": [award_row("KNOWN"), award_row("RETRY")],
                    "page_metadata": {"hasNext": False},
                },
            )
        assert request.url.path == TRANSACTIONS_PATH
        identifier = body["award_id"]
        lookups.append(identifier)
        if identifier == "RETRY" and fail:
            raise httpx.RemoteProtocolError("disconnect", request=request)
        value = known_date if identifier == "KNOWN" else "2026-01-03"
        return httpx.Response(200, json={"results": [{"action_date": value}] if value else []})

    settings = Settings(
        _env_file=None,
        usaspending_base_url=BASE_URL,
        enrichment_cache_directory=tmp_path,
        http_request_interval_seconds=0,
        http_retry_attempts=1,
        enrichment_concurrency=1,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = build_usaspending_source(client, settings)
        with pytest.raises(httpx.RemoteProtocolError) as first:
            await get_page(source)

    assert getattr(first.value, "source_page_rows", None) == 2
    assert getattr(first.value, "source_page_cache_hit", None) is False
    [checkpoint] = list(tmp_path.glob("*.json"))
    assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600
    saved = checkpoint.read_text()
    assert "cache-user" not in saved and "cache-secret" not in saved
    assert BASE_URL not in saved
    assert len(checkpoint.stem) == 64
    assert json.loads(saved)["action_dates"] == {"KNOWN": known_date}

    # A fresh interpreter must be able to recover these observations from disk.
    script = """
import json, sys
from datetime import date
from pathlib import Path
from orbital_signal.source_cache import PendingEnrichmentCache
cache = PendingEnrichmentCache(directory=Path(sys.argv[1]), base_url=sys.argv[2])
entry = cache.get(
    agency=sys.argv[3], start_date=date(2026,1,1), end_date=date(2026,1,3),
    page=1, records_per_page=100,
)
assert entry is not None
print(json.dumps({'rows': len(entry.awards), 'dates': entry.action_dates}, default=str))
"""
    restored = json.loads(
        subprocess.check_output(
            [sys.executable, "-c", script, str(tmp_path), BASE_URL, NASA], text=True
        )
    )
    assert restored == {"rows": 2, "dates": {"KNOWN": known_date}}

    fail = False
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await get_page(build_usaspending_source(client, settings))

    assert result.cache_hit is True
    assert result.fetched_count == 2
    assert searches == 1
    assert lookups == ["KNOWN", "RETRY", "RETRY"]
    assert [award.action_date for award in result.awards] == [
        date.fromisoformat(known_date) if known_date else None,
        date(2026, 1, 3),
    ]
    assert list(tmp_path.glob("*.json")) == []

    # Completed pages are not reused by a later collection or refresh.
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        fresh = await get_page(build_usaspending_source(client, settings))
    assert fresh.cache_hit is False
    assert searches == 2
    assert lookups[-2:] == ["KNOWN", "RETRY"]


@pytest.mark.parametrize(
    "changed",
    [
        {"agency": DOD},
        {"start_date": date(2026, 1, 2)},
        {"end_date": date(2026, 1, 4)},
        {"page": 2},
        {"records_per_page": 50},
    ],
)
async def test_cache_identity_keeps_agencies_dates_pages_and_page_sizes_separate(
    tmp_path: Path, changed: dict[str, Any]
) -> None:
    searches = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal searches
        if request.url.path == SEARCH_PATH:
            searches += 1
            return httpx.Response(
                200, json={"results": [award_row("RETRY")], "page_metadata": {"hasNext": False}}
            )
        raise httpx.ReadError("temporary lookup failure", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.ReadError):
            await get_page(make_source(client, tmp_path))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.ReadError) as caught:
            await get_page(make_source(client, tmp_path), **changed)

    assert searches == 2
    assert getattr(caught.value, "source_page_cache_hit", None) is False
    assert len(list(tmp_path.glob("*.json"))) == 2


async def test_cache_identity_includes_source_origin(tmp_path: Path) -> None:
    searches = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal searches
        if request.url.path == SEARCH_PATH:
            searches += 1
            return httpx.Response(
                200, json={"results": [award_row("RETRY")], "page_metadata": {"hasNext": False}}
            )
        raise httpx.ReadError("lookup unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.ReadError):
            await get_page(make_source(client, tmp_path))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.ReadError):
            await get_page(make_source(client, tmp_path, base_url="https://other.usaspending.test"))

    assert searches == 2
    assert len(list(tmp_path.glob("*.json"))) == 2


@pytest.mark.parametrize("damage", ["json", "utf8", "version", "identity", "expired"])
async def test_invalid_or_expired_cache_is_discarded_and_search_is_repeated(
    tmp_path: Path, monkeypatch, damage: str
) -> None:
    now = [1000.0]
    monkeypatch.setattr(source_cache, "time", lambda: now[0])
    searches = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal searches
        if request.url.path == SEARCH_PATH:
            searches += 1
            return httpx.Response(
                200, json={"results": [award_row("RETRY")], "page_metadata": {"hasNext": False}}
            )
        raise httpx.RemoteProtocolError("lookup unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.RemoteProtocolError):
            await get_page(make_source(client, tmp_path, enrichment_cache_ttl_seconds=10))
    [path] = list(tmp_path.glob("*.json"))
    if damage == "json":
        path.write_text("{broken", encoding="utf-8")
    elif damage == "utf8":
        path.write_bytes(b"\xff\xfe")
    elif damage == "expired":
        now[0] += 10
    else:
        content = json.loads(path.read_text())
        content["version" if damage == "version" else "key_digest"] = (
            99 if damage == "version" else "invalid"
        )
        path.write_text(json.dumps(content), encoding="utf-8")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.RemoteProtocolError) as caught:
            await get_page(make_source(client, tmp_path, enrichment_cache_ttl_seconds=10))

    assert searches == 2
    assert getattr(caught.value, "source_page_cache_hit", None) is False
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_cache_evicts_least_recently_used_pages_and_does_not_extend_maximum_age(
    tmp_path: Path, monkeypatch, satellite_award: AwardRecord
) -> None:
    now = [1000.0]
    monkeypatch.setattr(source_cache, "time", lambda: now[0])
    cache = PendingEnrichmentCache(
        directory=tmp_path, base_url=BASE_URL, max_pages=2, ttl_seconds=10
    )

    def entry(day: int):
        return cache.new_page(
            agency=NASA,
            start_date=date(2026, 1, day),
            end_date=date(2026, 1, day),
            page=1,
            records_per_page=100,
            awards=[satellite_award],
            has_more=False,
        )

    first, second = entry(1), entry(2)
    cache.save(first)
    now[0] += 1
    cache.save(second)
    now[0] += 1
    assert cache.get(agency=NASA, start_date=START, end_date=START, page=1, records_per_page=100)
    now[0] += 1
    third = entry(3)
    cache.save(third)
    assert len(list(tmp_path.glob("*.json"))) == 2
    assert not (tmp_path / f"{second.key_digest}.json").exists()
    assert (tmp_path / f"{first.key_digest}.json").exists()
    now[0] = 1009
    assert cache.get(agency=NASA, start_date=START, end_date=START, page=1, records_per_page=100)
    now[0] = 1010
    assert (
        cache.get(agency=NASA, start_date=START, end_date=START, page=1, records_per_page=100)
        is None
    )


async def test_explicit_refresh_invalidates_only_overlapping_matching_agency_pages(
    tmp_path: Path,
) -> None:
    searches: list[tuple[str, str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == SEARCH_PATH:
            period = body["filters"]["time_period"][0]
            searches.append(
                (body["filters"]["agencies"][0]["name"], period["start_date"], period["end_date"])
            )
            return httpx.Response(
                200, json={"results": [award_row("RETRY")], "page_metadata": {"hasNext": False}}
            )
        raise httpx.ReadError("lookup unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = make_source(client, tmp_path)
        for options in (
            {},
            {"agency": DOD},
            {"start_date": date(2026, 1, 4), "end_date": date(2026, 1, 6)},
        ):
            with pytest.raises(httpx.ReadError):
                await get_page(source, **options)
        source.clear_cached_pages(
            start_date=date(2026, 1, 2), end_date=date(2026, 1, 2), agencies=(NASA,)
        )
        assert len(list(tmp_path.glob("*.json"))) == 2
        with pytest.raises(httpx.ReadError) as reused:
            await get_page(source, agency=DOD)
        assert getattr(reused.value, "source_page_cache_hit", None) is True
        with pytest.raises(httpx.ReadError):
            await get_page(source)

    assert len(searches) == 4
    assert searches[0] == searches[-1]


async def test_cache_directory_read_error_fails_before_upstream_access(tmp_path: Path) -> None:
    directory = tmp_path / "cache"
    directory.write_text("not a directory")

    def unexpected(_: httpx.Request) -> httpx.Response:
        raise AssertionError("an unreadable pending cache must not be silently bypassed")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(SourceDataError, match="cache directory permissions") as caught:
            await get_page(make_source(client, directory))

    assert getattr(caught.value, "source_operation", None) == "pending_enrichment_cache"
    assert getattr(caught.value, "source_page_received", False) is False


async def test_atomic_cache_write_error_is_visible_and_never_returns_a_completed_page(
    tmp_path: Path, monkeypatch
) -> None:
    lookups = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal lookups
        if request.url.path == SEARCH_PATH:
            return httpx.Response(
                200, json={"results": [award_row("KNOWN")], "page_metadata": {"hasNext": False}}
            )
        lookups += 1
        return httpx.Response(200, json={"results": []})

    def fail_replace(*args, **kwargs) -> None:
        raise PermissionError("fixture cache write denied")

    monkeypatch.setattr(source_cache.os, "replace", fail_replace)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(SourceDataError, match="cache directory permissions") as caught:
            await get_page(make_source(client, tmp_path))

    assert lookups == 0
    assert getattr(caught.value, "source_page_received", None) is True
    assert getattr(caught.value, "source_page_rows", None) == 1
    assert getattr(caught.value, "source_operation", None) == "pending_enrichment_cache"
    assert list(tmp_path.iterdir()) == []


async def test_direct_source_without_cache_keeps_no_local_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == SEARCH_PATH:
            return httpx.Response(
                200, json={"results": [award_row("RETRY")], "page_metadata": {"hasNext": False}}
            )
        raise httpx.ReadError("lookup unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.ReadError):
            await get_page(make_source(client, None))

    assert list(tmp_path.iterdir()) == []
