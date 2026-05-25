import logging

from telegram import Update
from telegram.ext import Application

from .config import load_settings
from .db import Database
from .evolution import EvolutionClient
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
    await db.setup()
    await evolution.start()
    settings.campaigns_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Bot v2 inicializado")


async def post_shutdown(application: Application):
    evolution = application.bot_data.get("evolution")
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
    )

    application.bot_data["settings"] = settings
    application.bot_data["db"] = db
    application.bot_data["evolution"] = evolution
    application.bot_data["scheduler"] = scheduler

    register_handlers(application)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
