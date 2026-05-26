import logging

from telegram import Update
from telegram.ext import Application

from .cleanup import cleanup_campaign_payload, cleanup_orphan_campaign_dirs, cleanup_tmp_import_dir
from .config import load_settings
from .db import Database
from .docker_control import DockerControl
from .evolution import EvolutionClient
from .evolution_power import EvolutionPowerManager
from .scheduler import CampaignScheduler
from .telegram_bot import register_handlers


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def post_init(application: Application):
    settings = application.bot_data["settings"]
    db = application.bot_data["db"]
    evolution = application.bot_data["evolution"]
    power = application.bot_data["power"]
    settings.campaigns_dir.mkdir(parents=True, exist_ok=True)
    try:
        await db.setup()
        await db.prune_contact_list_snapshots(
            settings.contact_list_snapshot_keep,
            settings.contact_list_snapshot_days,
        )
    except Exception as exc:
        logger.exception("DB setup failed")
        raise
    interrupted_ids = await db.recover_interrupted_campaigns() or []
    for campaign_id in interrupted_ids:
        await cleanup_campaign_payload(
            db,
            settings.campaigns_dir,
            campaign_id,
            settings.cleanup_campaign_files_on_finish,
        )
    for campaign_id in await db.terminal_campaign_ids():
        await cleanup_campaign_payload(
            db,
            settings.campaigns_dir,
            campaign_id,
            settings.cleanup_campaign_files_on_finish,
        )
    await cleanup_orphan_campaign_dirs(
        db,
        settings.campaigns_dir,
        settings.cleanup_campaign_files_on_finish,
    )
    cleanup_tmp_import_dir(settings.campaigns_dir)
    await evolution.start()
    await power.start()
    await power.stop_if_idle(db)
    logger.info("Bot v2 inicializado")


async def post_shutdown(application: Application):
    evolution = application.bot_data.get("evolution")
    power = application.bot_data.get("power")
    if power:
        await power.close()
    if evolution:
        await evolution.close()


def main():
    settings = load_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN nao configurado")
    if not settings.evolution_api_key:
        raise RuntimeError("EVOLUTION_API_KEY nao configurado")

    db = Database(settings.database_path)
    evolution = EvolutionClient(
        settings.evolution_api_url,
        settings.evolution_api_key,
        settings.max_parallel_media_uploads,
    )
    docker_control = None
    if settings.evolution_docker_control:
        docker_control = DockerControl(settings.docker_socket_path, settings.evolution_docker_container)
    power = EvolutionPowerManager(evolution, docker_control)

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    scheduler = CampaignScheduler(
        db,
        evolution,
        application,
        settings.send_window,
        settings.progress_update_interval_seconds,
        settings.campaigns_dir,
        settings.cleanup_campaign_files_on_finish,
        settings.min_free_memory_mb,
        power,
    )

    application.bot_data["settings"] = settings
    application.bot_data["db"] = db
    application.bot_data["evolution"] = evolution
    application.bot_data["power"] = power
    application.bot_data["scheduler"] = scheduler

    register_handlers(application)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
