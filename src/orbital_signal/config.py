"""Application settings."""

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from ORBITAL_SIGNAL_* variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ORBITAL_SIGNAL_",
        extra="ignore",
    )

    usaspending_base_url: str = "https://api.usaspending.gov"
    http_timeout_seconds: float = Field(default=30.0, gt=0)
    http_retry_attempts: int = Field(default=4, ge=1, le=10)
    http_retry_base_delay_seconds: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    http_retry_max_delay_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    enrichment_concurrency: int = Field(default=2, ge=1, le=10)
    http_max_connections: int = Field(default=4, ge=1, le=100)
    http_keepalive_expiry_seconds: float = Field(default=5.0, ge=0, allow_inf_nan=False)
    http_request_interval_seconds: float = Field(default=0.5, ge=0, allow_inf_nan=False)
    collection_window_delay_seconds: float = Field(default=5.0, ge=0, allow_inf_nan=False)
    collection_max_consecutive_failures: int = Field(default=3, ge=1)
    enrichment_cache_directory: Path | None = Path(".orbital-signal-cache/enrichment")
    enrichment_cache_ttl_seconds: float = Field(default=86400.0, gt=0, allow_inf_nan=False)
    enrichment_cache_max_pages: int = Field(default=8, ge=1)
    database_url: str = (
        "postgresql+asyncpg://orbital_signal:orbital_signal@127.0.0.1:55432/orbital_signal"
    )
    database_echo: bool = False
    api_writes_enabled: bool = False

    @model_validator(mode="after")
    def validate_retry_delays(self) -> "Settings":
        if self.http_retry_max_delay_seconds < self.http_retry_base_delay_seconds:
            raise ValueError("http_retry_max_delay_seconds must be at least the base delay")
        return self
