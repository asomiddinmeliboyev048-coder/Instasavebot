from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(min_length=10)
    bot_username: str = ""
    max_file_size_mb: int = Field(default=49, ge=1, le=2000)
    download_dir: Path = Path("downloads")
    max_concurrent_downloads: int = Field(default=2, ge=1, le=8)
    download_timeout_seconds: int = Field(default=180, ge=30, le=900)
    ytdlp_cookies_file: str = ""

    supabase_url: str = ""
    supabase_service_role_key: str = ""
    zikra_storage_bucket: str = ""
    zikra_allowed_hosts: str = "zikra.uz,www.zikra.uz"
    zikra_video_table: str = ""
    zikra_video_id_column: str = "id"
    zikra_video_path_column: str = "storage_path"
    zikra_video_bucket_column: str = ""

    log_level: str = "INFO"

    @property
    def allowed_zikra_hosts(self) -> set[str]:
        return {host.strip().lower() for host in self.zikra_allowed_hosts.split(",") if host.strip()}

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
