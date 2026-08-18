import hashlib
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
    allow_open_access: bool
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
    send_window_tz: str
    min_free_memory_mb: int
    evolution_docker_control: bool
    evolution_docker_container: str
    docker_socket_path: str
    evolution_idle_stop_seconds: int
    contact_list_snapshot_keep: int
    contact_list_snapshot_days: int
    webhook_listen_host: str
    webhook_listen_port: int
    webhook_token: str
    webhook_public_url: str
    webhook_auto_configure: bool


def load_settings() -> Settings:
    evolution_api_key = os.getenv("EVOLUTION_API_KEY", "")
    webhook_token = (os.getenv("WEBHOOK_TOKEN", "") or "").strip()
    webhook_auto_configure = _bool_env("WEBHOOK_AUTO_CONFIGURE", True)
    webhook_listen_port = _int_env("WEBHOOK_LISTEN_PORT", 8090)
    if not webhook_token and webhook_auto_configure and evolution_api_key:
        digest = hashlib.sha256(evolution_api_key.encode("utf-8")).hexdigest()
        webhook_token = f"wh_{digest[:32]}"

    webhook_public_url = (os.getenv("WEBHOOK_PUBLIC_URL", "") or "").rstrip("/")
    if not webhook_public_url and webhook_token:
        webhook_public_url = f"http://whatsapp-bot-v2:{webhook_listen_port}/webhook/{webhook_token}"

    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_admin_ids=_admin_ids(),
        allow_open_access=_bool_env("ALLOW_OPEN_ACCESS", False),
        evolution_api_url=os.getenv("EVOLUTION_API_URL", "http://evolution-api:8080").rstrip("/"),
        evolution_api_key=evolution_api_key,
        database_path=Path(os.getenv("DATABASE_PATH", "/app/data/bot.sqlite3")),
        campaigns_dir=Path(os.getenv("CAMPAIGNS_DIR", "/app/campaigns")),
        max_clients_per_campaign=_int_env("MAX_CLIENTS_PER_CAMPAIGN", 0),
        max_trusted_clients_per_campaign=_int_env("MAX_TRUSTED_CLIENTS_PER_CAMPAIGN", 0),
        max_precaution_clients_per_campaign=_int_env("MAX_PRECAUTION_CLIENTS_PER_CAMPAIGN", 0),
        max_media_per_campaign=_int_env("MAX_MEDIA_PER_CAMPAIGN", 5),
        max_media_file_mb=_int_env("MAX_MEDIA_FILE_MB", 3),
        max_parallel_media_uploads=_int_env("MAX_PARALLEL_MEDIA_UPLOADS", 2),
        progress_update_interval_seconds=_int_env("PROGRESS_UPDATE_INTERVAL_SECONDS", 5),
        cleanup_campaign_files_on_finish=_bool_env("CLEANUP_CAMPAIGN_FILES_ON_FINISH", True),
        default_profile=os.getenv("DEFAULT_PROFILE", "humano_100"),
        send_window=os.getenv("SEND_WINDOW") or None,
        # Vazio = janela interpretada na hora local do container (UTC na imagem
        # oficial). Em producao (VPS no Brasil) use America/Sao_Paulo.
        send_window_tz=os.getenv("SEND_WINDOW_TZ", "").strip(),
        min_free_memory_mb=_int_env("MIN_FREE_MEMORY_MB", 256),
        evolution_docker_control=_bool_env("EVOLUTION_DOCKER_CONTROL", False),
        evolution_docker_container=os.getenv("EVOLUTION_DOCKER_CONTAINER", "evolution-api"),
        docker_socket_path=os.getenv("DOCKER_SOCKET_PATH", "/var/run/docker.sock"),
        evolution_idle_stop_seconds=_int_env("EVOLUTION_IDLE_STOP_SECONDS", 600),
        contact_list_snapshot_keep=_int_env("CONTACT_LIST_SNAPSHOT_KEEP", 3),
        contact_list_snapshot_days=_int_env("CONTACT_LIST_SNAPSHOT_DAYS", 14),
        webhook_listen_host=os.getenv("WEBHOOK_LISTEN_HOST", "0.0.0.0"),
        webhook_listen_port=webhook_listen_port,
        webhook_token=webhook_token,
        webhook_public_url=webhook_public_url,
        webhook_auto_configure=webhook_auto_configure,
    )
