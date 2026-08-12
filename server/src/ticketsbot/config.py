from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TICKETSBOT_", extra="ignore")

    bot_token: str = ""
    database_url: str = Field(default="sqlite+aiosqlite:///./ticketsbot.db")
    host: str = "127.0.0.1"
    port: int = 8010
    sqlite_busy_timeout_ms: int = 10_000
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    notify_chat_id: str = ""
    notify_thread_id: str | None = None
    media_dir: Path = Path("./media")
    public_base_url: str = "http://127.0.0.1:8010"
    max_attachment_bytes: int = 20 * 1024 * 1024
    max_request_body_bytes: int = 30 * 1024 * 1024
    max_text_field_length: int = 10_000
    max_init_data_length: int = 16_384
    init_data_max_age_seconds: int = 24 * 60 * 60
    init_data_future_skew_seconds: int = 60
    media_url_ttl_seconds: int = 60
    workers_enabled: bool = False
    worker_poll_seconds: float = 1.0
    worker_batch_size: int = 20
    worker_max_attempts: int = 12
    worker_claim_timeout_seconds: float = 300.0
    sheet_bridge_url: str = ""
    sheet_bridge_secret: str = ""
    roles_pull_seconds: float = 300.0

    @property
    def allowed_origins(self) -> list[str]:
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]

    @property
    def sqlite_path(self) -> Path | None:
        prefix = "sqlite+aiosqlite:///"
        return Path(self.database_url[len(prefix):]) if self.database_url.startswith(prefix) else None

    def validate_public_base_url(self) -> None:
        parsed = urlparse(self.public_base_url)
        local_hosts = {"localhost", "127.0.0.1", "::1", "testserver"}
        is_test_host = bool(parsed.hostname and parsed.hostname.endswith(".test"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("public_base_url must be an absolute HTTP(S) URL")
        if parsed.scheme != "https" and parsed.hostname not in local_hosts and not is_test_host:
            raise ValueError("public_base_url must use HTTPS outside localhost/test")


@lru_cache
def get_settings() -> Settings:
    return Settings()
