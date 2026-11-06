"""Shared conservative HTTP settings for USAspending collection entry points."""

import httpx

from orbital_signal import __version__
from orbital_signal.config import Settings
from orbital_signal.sources.usaspending import USAspendingClient


def build_http_client(settings: Settings) -> httpx.AsyncClient:
    """Keep verified TLS and environment proxies while bounding connection reuse."""

    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.http_timeout_seconds, pool=min(settings.http_timeout_seconds, 5)
        ),
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_connections,
            keepalive_expiry=settings.http_keepalive_expiry_seconds,
        ),
        headers={"User-Agent": f"orbital-signal/{__version__}"},
        trust_env=True,
    )


def build_usaspending_source(client: httpx.AsyncClient, settings: Settings) -> USAspendingClient:
    """Use one request retry and enrichment policy in the API and CLI."""

    return USAspendingClient(
        client,
        base_url=settings.usaspending_base_url,
        enrichment_concurrency=settings.enrichment_concurrency,
        retry_attempts=settings.http_retry_attempts,
        retry_base_delay_seconds=settings.http_retry_base_delay_seconds,
        retry_max_delay_seconds=settings.http_retry_max_delay_seconds,
        request_interval_seconds=settings.http_request_interval_seconds,
        enrichment_cache_directory=settings.enrichment_cache_directory,
        enrichment_cache_ttl_seconds=settings.enrichment_cache_ttl_seconds,
        enrichment_cache_max_pages=settings.enrichment_cache_max_pages,
    )
