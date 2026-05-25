import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set

from dotenv import load_dotenv


load_dotenv()


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    return int(value)


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "sim", "on")


def _admin_ids() -> Set[int]:
    raw = os.getenv("TELEGRAM_ADMIN_IDS", "")
    return {int(item.strip()) for item in raw.split(",") if item.strip()}


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_admin_ids: Set[int]
    evolution_api_url: str
    evolution_api_key: str
    database_path: Path
    campaigns_dir: Path
    max_clients_per_campaign: int
    max_trusted_clients_per_campaign: int
    max_precaution_clients_per_campaign: int
    max_media_per_campaign: int
    max_media_file_mb: int
    max_parallel_media_uploads: int
    progress_update_interval_seconds: int
    cleanup_campaign_files_on_finish: bool
    default_profile: str
    send_window: Optional[str]
    min_free_memory_mb: int


def load_settings() -> Settings:
    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_admin_ids=_admin_ids(),
        evolution_api_url=os.getenv("EVOLUTION_API_URL", "http://evolution-api:8080").rstrip("/"),
        evolution_api_key=os.getenv("EVOLUTION_API_KEY", ""),
        database_path=Path(os.getenv("DATABASE_PATH", "/app/data/bot.sqlite3")),
        campaigns_dir=Path(os.getenv("CAMPAIGNS_DIR", "/app/campaigns")),
        max_clients_per_campaign=_int_env("MAX_CLIENTS_PER_CAMPAIGN", 100),
        max_trusted_clients_per_campaign=_int_env("MAX_TRUSTED_CLIENTS_PER_CAMPAIGN", 300),
        max_precaution_clients_per_campaign=_int_env("MAX_PRECAUTION_CLIENTS_PER_CAMPAIGN", 100),
        max_media_per_campaign=_int_env("MAX_MEDIA_PER_CAMPAIGN", 5),
        max_media_file_mb=_int_env("MAX_MEDIA_FILE_MB", 3),
        max_parallel_media_uploads=_int_env("MAX_PARALLEL_MEDIA_UPLOADS", 2),
        progress_update_interval_seconds=_int_env("PROGRESS_UPDATE_INTERVAL_SECONDS", 5),
        cleanup_campaign_files_on_finish=_bool_env("CLEANUP_CAMPAIGN_FILES_ON_FINISH", True),
        default_profile=os.getenv("DEFAULT_PROFILE", "humano_100"),
        send_window=os.getenv("SEND_WINDOW") or None,
        min_free_memory_mb=_int_env("MIN_FREE_MEMORY_MB", 256),
    )
