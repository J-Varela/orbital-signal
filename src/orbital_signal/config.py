"""Application settings."""

from pydantic import Field
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
    database_url: str = (
        "postgresql+asyncpg://orbital_signal:orbital_signal@127.0.0.1:55432/orbital_signal"
    )
    database_echo: bool = False
