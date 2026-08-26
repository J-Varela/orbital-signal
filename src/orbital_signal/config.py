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
