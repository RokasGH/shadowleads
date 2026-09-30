"""Runtime configuration. Everything comes from env vars / `.env`; nothing secret is hard-coded."""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Vilnius city municipality bounding box (WGS84). Places outside the municipality are
# dropped later using addressComponents, the box only bounds the Google sweep.
VILNIUS_BBOX = (54.567, 25.024, 54.832, 25.481)  # (south, west, north, east)


def _alias(*names: str) -> AliasChoices:
    return AliasChoices(*names)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    data_dir: Path = Field(Path("data"), validation_alias=_alias("SHADOWLEADS_DATA_DIR"))
    output_dir: Path = Field(Path("output"), validation_alias=_alias("SHADOWLEADS_OUTPUT_DIR"))
    user_agent: str = "shadowleads/0.1 (VMI lead-list research prototype)"

    google_maps_api_key: SecretStr | None = Field(None, validation_alias=_alias("GOOGLE_MAPS_API_KEY"))
    # Hard cap of billable Google calls per SKU per calendar month (free tier is 1,000).
    google_budget: int = Field(900, validation_alias=_alias("SHADOWLEADS_GOOGLE_BUDGET"))

    oxylabs_username: str | None = Field(None, validation_alias=_alias("OXYLABS_USERNAME"))
    oxylabs_password: SecretStr | None = Field(None, validation_alias=_alias("OXYLABS_PASSWORD"))
    oxylabs_api_url: str = Field(
        "https://realtime.oxylabs.io/v1/queries", validation_alias=_alias("OXYLABS_API_URL")
    )
    oxylabs_geo_location: str = Field(
        "Vilnius,Lithuania", validation_alias=_alias("OXYLABS_GEO_LOCATION")
    )
    oxylabs_budget: int = Field(300, validation_alias=_alias("SHADOWLEADS_OXYLABS_BUDGET"))

    pseudonym_key: SecretStr = Field(
        SecretStr("local-dev-only-change-me"), validation_alias=_alias("SHADOWLEADS_PSEUDONYM_KEY")
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "warehouse.duckdb"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def has_google(self) -> bool:
        return bool(self.google_maps_api_key and self.google_maps_api_key.get_secret_value())

    @property
    def has_oxylabs(self) -> bool:
        return bool(self.oxylabs_username and self.oxylabs_password)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def current_run_month() -> str:
    """Run months are identified as 'YYYY-MM'."""
    return date.today().strftime("%Y-%m")
