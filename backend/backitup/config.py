from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BACKITUP_", env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = "postgresql+psycopg://backitup:backitup@localhost:54322/backitup"
    public_url: str = "http://localhost:8000"
    frontend_url: str = "http://127.0.0.1:5173"
    supabase_url: str = "http://localhost:54321"
    supabase_public_url: str = "http://localhost:54321"
    # Supabase's current low-privilege browser/server key. The legacy anon
    # key is intentionally no longer part of the public configuration.
    supabase_publishable_key: str = ""
    supabase_jwks_url: str = ""
    jwt_audience: str = "authenticated"
    credential_key_file: Path = Path("secrets/credentials.key")
    signing_key_file: Path = Path("secrets/manifest.pem")
    verification_keys_dir: Path = Path("secrets/verification")
    internal_token_file: Path = Path("secrets/internal.token")
    source_app_id: str = ""
    source_app_slug: str = ""
    source_client_id: str = ""
    source_client_secret: str = ""
    source_private_key_file: Path = Path("secrets/source-app.pem")
    source_webhook_secret: str = ""
    recovery_app_id: str = ""
    recovery_app_slug: str = ""
    recovery_client_id: str = ""
    recovery_client_secret: str = ""
    recovery_private_key_file: Path = Path("secrets/recovery-app.pem")
    github_api_version: str = "2026-03-10"
    managed_r2_account_id: str = ""
    managed_r2_bucket: str = ""
    managed_r2_access_key_id: str = ""
    managed_r2_secret_access_key: str = ""
    managed_workspace_bytes: int = 5_000_000_000
    managed_global_bytes: int = 100_000_000_000
    global_concurrency: int = 2
    workspace_concurrency: int = 1
    lease_seconds: int = 90
    max_attempts: int = 3
    job_timeout_seconds: int = 3600
    scratch_limit_bytes: int = 10_000_000_000
    max_archive_entries: int = 1_000_000
    manual_backups_per_hour: int = 10
    max_queued_per_workspace: int = 100
    scratch_dir: Path = Path(".state/work")
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True

    @field_validator("public_url", "frontend_url", "supabase_public_url")
    @classmethod
    def clean_url(cls, value: str) -> str:
        return value.rstrip("/")

    def validate_production(self) -> None:
        if self.environment != "production":
            return
        for url in (self.public_url, self.frontend_url, self.supabase_public_url):
            if not url.startswith("https://"):
                raise ValueError("Production public URLs must use HTTPS")
        for path in (self.credential_key_file, self.signing_key_file, self.internal_token_file):
            if not path.is_file():
                raise ValueError(f"Required server secret is missing: {path.name}")
        if not self.managed_storage_ready():
            raise ValueError("Managed R2 storage must be configured in production")

    def jwks_url(self) -> str:
        return self.supabase_jwks_url or (self.supabase_public_url + "/auth/v1/.well-known/jwks.json")

    def managed_storage_ready(self) -> bool:
        return all((self.managed_r2_account_id, self.managed_r2_bucket, self.managed_r2_access_key_id, self.managed_r2_secret_access_key))


@lru_cache
def get_settings() -> Settings:
    return Settings()
