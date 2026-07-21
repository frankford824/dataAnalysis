from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COMMERCE_", env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = "sqlite:///./commerce.db"
    secret_key: str = "development-only-change-me"
    auto_create_schema: bool = True
    object_storage_backend: str = "local"
    object_storage_path: Path = Field(default=Path("./var/objects"))
    s3_endpoint_url: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_raw_bucket: str = "commerce-raw"
    s3_intermediate_bucket: str = "commerce-intermediate"
    s3_export_bucket: str = "commerce-exports"
    s3_region: str = "us-east-1"
    redis_url: str = "redis://localhost:6379/0"
    litellm_base_url: str | None = None
    sql_timeout_seconds: int = 10
    sql_max_rows: int = 1000

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith(("postgresql://", "postgresql+psycopg://"))


@lru_cache
def get_settings() -> Settings:
    return Settings()
