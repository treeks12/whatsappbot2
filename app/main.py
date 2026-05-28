import logging

from telegram import Update
from telegram.ext import Application

from .cleanup import cleanup_campaign_payload, cleanup_orphan_campaign_dirs, cleanup_tmp_import_dir
from .config import load_settings
from .db import Database
from .docker_control import DockerControl
from .evolution import EvolutionClient
from .evolution_power import EvolutionPowerManager
from .scheduler import CampaignScheduler, SuspicionTracker
from .telegram_bot import register_handlers
from .webhook import WebhookServer


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
    webhook_server = application.bot_data.get("webhook_server")
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
    if webhook_server is not None:
        try:
            await webhook_server.start()
        except Exception:
            logger.exception("Falha ao iniciar webhook server")
    if (
        settings.webhook_auto_configure
        and settings.webhook_public_url
        and settings.webhook_token
    ):
        await _reconfigure_vendor_instances(db, evolution, settings.webhook_public_url)
    await power.stop_if_idle(db)
    logger.info("Bot v2 inicializado")


async def _reconfigure_vendor_instances(db: Database, evolution: EvolutionClient, public_url: str):
    """Para cada vendor conhecido, tenta garantir que o webhook esta apontando aqui."""
    try:
        with db.connect() as conn:
            rows = conn.execute("SELECT instance_name FROM vendors").fetchall()
        instances = [row["instance_name"] for row in rows if row["instance_name"]]
    except Exception:
        logger.exception("Falha ao listar vendors para reconfigurar webhook")
        return

    for instance_name in instances:
        try:
            await evolution.set_instance_webhook(instance_name, public_url)
            logger.info("Webhook reconfigurado em %s -> %s", instance_name, public_url)
        except Exception:
            logger.warning("Falha ao reconfigurar webhook em %s", instance_name, exc_info=True)


async def post_shutdown(application: Application):
    evolution = application.bot_data.get("evolution")
    power = application.bot_data.get("power")
    webhook_server = application.bot_data.get("webhook_server")
    if webhook_server:
        await webhook_server.close()
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
    suspicion_tracker = SuspicionTracker()
    webhook_server = None
    if settings.webhook_token:
        webhook_server = WebhookServer(
            db,
            host=settings.webhook_listen_host,
            port=settings.webhook_listen_port,
            token=settings.webhook_token,
            suspicion_tracker=suspicion_tracker,
        )

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
        suspicion_tracker=suspicion_tracker,
    )

    application.bot_data["settings"] = settings
    application.bot_data["db"] = db
    application.bot_data["evolution"] = evolution
    application.bot_data["power"] = power
    application.bot_data["scheduler"] = scheduler
    application.bot_data["webhook_server"] = webhook_server

    register_handlers(application)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
