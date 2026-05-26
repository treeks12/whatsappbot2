import asyncio
import logging
from datetime import datetime, time
from pathlib import Path
from typing import Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from .cleanup import cleanup_campaign_payload
from .db import Database
from .evolution import EvolutionClient
from .profiles import get_profile


logger = logging.getLogger(__name__)


def available_memory_mb() -> float:
    try:
        info = Path("/proc/meminfo").read_text()
        for line in info.splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except Exception:
        pass
    return 9999


def campaign_controls(campaign_id: int, status: str) -> Optional[InlineKeyboardMarkup]:
    if status == "running":
        primary = InlineKeyboardButton("⏸ Pausar", callback_data=f"campaign_pause:{campaign_id}")
    elif status == "paused":
        primary = InlineKeyboardButton("▶️ Continuar", callback_data=f"campaign_resume:{campaign_id}")
    else:
        return None

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Status", callback_data=f"campaign_status:{campaign_id}"),
            primary,
        ],
        [
            InlineKeyboardButton("🛑 Cancelar", callback_data=f"campaign_cancel_confirm:{campaign_id}"),
        ],
    ])


class CampaignScheduler:
    def __init__(
        self,
        db: Database,
        evolution: EvolutionClient,
        telegram_app: Application,
        send_window: Optional[str],
        progress_update_interval_seconds: int = 5,
        campaigns_dir: Optional[Path] = None,
        cleanup_campaign_files_on_finish: bool = True,
        min_free_memory_mb: int = 256,
    ):
        self.db = db
        self.evolution = evolution
        self.telegram_app = telegram_app
        self.send_window = parse_window(send_window)
        self.progress_update_interval_seconds = max(2, progress_update_interval_seconds)
        self.campaigns_dir = campaigns_dir
        self.cleanup_campaign_files_on_finish = cleanup_campaign_files_on_finish
        self.min_free_memory_mb = min_free_memory_mb
        self.tasks: Dict[int, asyncio.Task] = {}
        self.instance_locks: Dict[str, asyncio.Lock] = {}

    async def start(self, campaign_id: int, progress_chat_id: Optional[int] = None, progress_message_id: Optional[int] = None) -> bool:
        if campaign_id in self.tasks and not self.tasks[campaign_id].done():
            return False

        campaign = await self.db.get_campaign_with_vendor(campaign_id)
        if not campaign:
            return False

        task = asyncio.create_task(self._run_campaign(campaign_id, progress_chat_id, progress_message_id))
        self.tasks[campaign_id] = task
        return True

    async def pause_vendor_campaign(self, vendor_id: int, campaign_id: Optional[int] = None) -> bool:
        campaign = await self._vendor_campaign(vendor_id, campaign_id)
        if not campaign or campaign["status"] != "running":
            return False

        await self.db.set_campaign_status(campaign["id"], "paused")
        return True

    async def resume_vendor_campaign(self, vendor_id: int, campaign_id: Optional[int] = None) -> bool:
        campaign = await self._vendor_campaign(vendor_id, campaign_id)
        if not campaign or campaign["status"] != "paused":
            return False

        await self.db.set_campaign_status(campaign["id"], "running")
        return True

    async def cancel_vendor_campaign(self, vendor_id: int, campaign_id: Optional[int] = None) -> bool:
        campaign = await self._vendor_campaign(vendor_id, campaign_id)
        if not campaign:
            return False

        task = self.tasks.get(campaign["id"])
        if task and not task.done():
            task.cancel()

        await self.db.finish_campaign(campaign["id"], "cancelled")
        await self._cleanup_campaign_payload(campaign["id"])
        return True

    async def _vendor_campaign(self, vendor_id: int, campaign_id: Optional[int] = None):
        campaign = await self.db.get_active_campaign_for_vendor(vendor_id)
        if not campaign:
            return None
        if campaign_id is not None and campaign["id"] != campaign_id:
            return None
        return campaign

    async def _run_campaign(self, campaign_id: int, progress_chat_id: Optional[int], progress_message_id: Optional[int]):
        campaign = await self.db.get_campaign_with_vendor(campaign_id)
        if not campaign:
            return

        instance_name = campaign["instance_name"]
        lock = self.instance_locks.setdefault(instance_name, asyncio.Lock())
        progress = ProgressMessage(self.telegram_app, progress_chat_id or campaign["vendor_id"], progress_message_id)

        async with lock:
            await self.db.start_campaign(campaign_id)
            profile = get_profile(campaign["profile_id"])

            await progress.update(
                f"Campanha #{campaign_id} iniciada.\n"
                f"Contatos: 0/{campaign['total_contacts']}\n"
                "Preparando primeiro envio...",
                campaign_controls(campaign_id, "running"),
            )

            try:
                while True:
                    await self._wait_for_window()
                    await self._wait_if_paused(campaign_id, progress)

                    contact = await self.db.next_pending_contact(campaign_id)
                    if not contact:
                        await self.db.finish_campaign(campaign_id, "completed")
                        await self._cleanup_campaign_payload(campaign_id)
                        counts = await self.db.campaign_progress(campaign_id)
                        await progress.update(
                            f"Campanha #{campaign_id} concluida.\n"
                            f"Enviados: {counts['sent']}/{counts['total']}\n"
                            f"Falhas: {counts['failed']}"
                        )
                        return

                    before = await self.db.campaign_progress(campaign_id)
                    total = before["total"] or campaign["total_contacts"]
                    current_number = before["processed"] + 1
                    contact_name = contact["name"] or "Cliente"
                    returning_client = await self.db.phone_has_previous_success(contact["phone"], campaign_id)
                    relationship = "cliente conhecido" if returning_client else "cliente novo"

                    await progress.update(
                        f"Campanha #{campaign_id} em andamento.\n"
                        f"Contato {current_number}/{total}: enviando para {contact_name}.\n"
                        f"Tipo: {relationship}.",
                        campaign_controls(campaign_id, "running"),
                    )

                    try:
                        await self._send_contact(campaign, contact, profile)
                        await self.db.mark_contact_sent(campaign_id, contact["id"], contact["phone"])
                        result_line = f"Contato {current_number}/{total} enviado."
                    except Exception as exc:
                        logger.exception("Erro ao enviar contato %s", contact["id"])
                        await self.db.mark_contact_failed(contact["id"], str(exc))
                        result_line = f"Contato {current_number}/{total} falhou. Pulando para o proximo."

                    counts = await self.db.campaign_progress(campaign_id)
                    processed_count = counts["processed"]
                    sent_count = counts["sent"]
                    total = counts["total"] or total

                    if processed_count and processed_count % profile.pause_every == 0:
                        delay = profile.pause()
                        await self._sleep_with_progress(
                            campaign_id,
                            delay,
                            progress,
                            f"Campanha #{campaign_id} em pausa tecnica.\n"
                            f"{result_line}\n"
                            f"Progresso: {processed_count}/{total}\n"
                            f"Enviados: {sent_count} | Falhas: {counts['failed']}",
                            "Retomando em",
                        )
                    else:
                        delay = profile.between_returning_clients() if returning_client else profile.between_clients()
                        next_number = processed_count + 1
                        if next_number <= total:
                            await self._sleep_with_progress(
                                campaign_id,
                                delay,
                                progress,
                                f"Campanha #{campaign_id} em andamento.\n"
                                f"{result_line}\n"
                                f"Progresso: {processed_count}/{total}\n"
                                f"Enviados: {sent_count} | Falhas: {counts['failed']}",
                                f"Contato {next_number}/{total} em",
                            )
            except asyncio.CancelledError:
                await self.db.finish_campaign(campaign_id, "cancelled")
                await self._cleanup_campaign_payload(campaign_id)
                await progress.update(f"Campanha #{campaign_id} cancelada.")
                raise
            except Exception:
                logger.exception("Erro fatal na campanha %s", campaign_id)
                await self.db.finish_campaign(campaign_id, "failed")
                await self._cleanup_campaign_payload(campaign_id)
                await progress.update(f"Campanha #{campaign_id} falhou.")

    async def _send_contact(self, campaign, contact, profile):
        await self._wait_for_memory()
        media_items = await self.db.get_media(campaign["id"])
        text = (campaign["caption"] or "").replace("{nome}", contact["name"] or "Cliente")

        for index, media in enumerate(media_items):
            is_last_media = index == len(media_items) - 1
            await self._wait_for_memory()
            await self.evolution.send_media(
                campaign["instance_name"],
                contact["phone"],
                media["path"],
                media["mime_type"],
                media["file_name"],
                text if is_last_media else "",
            )
            if index < len(media_items) - 1:
                await asyncio.sleep(profile.between_media())

        if text and not media_items:
            await asyncio.sleep(profile.before_text())
            await self.evolution.send_text(campaign["instance_name"], contact["phone"], text)

    async def _wait_for_window(self):
        if not self.send_window:
            return

        while not is_inside_window(self.send_window):
            await asyncio.sleep(60)

    async def _wait_if_paused(self, campaign_id: int, progress):
        while True:
            campaign = await self.db.get_campaign(campaign_id)
            if not campaign or campaign["status"] != "paused":
                return

            counts = await self.db.campaign_progress(campaign_id)
            await progress.update(
                f"Campanha #{campaign_id} pausada.\n"
                f"Progresso: {counts['processed']}/{counts['total']}\n"
                f"Enviados: {counts['sent']} | Falhas: {counts['failed']}\n\n"
                "WhatsApp liberado para uso.\n"
                "Use ▶️ Continuar para retomar.",
                campaign_controls(campaign_id, "paused"),
            )
            await asyncio.sleep(2)

    async def _wait_for_memory(self):
        threshold = self.min_free_memory_mb
        free = available_memory_mb()
        if free >= threshold:
            return
        logger.warning("RAM livre %.0fMB abaixo do limite %dMB, aguardando...", free, threshold)
        while available_memory_mb() < threshold:
            await asyncio.sleep(30)

    async def _cleanup_campaign_payload(self, campaign_id: int):
        if not self.campaigns_dir:
            return
        await cleanup_campaign_payload(
            self.db,
            self.campaigns_dir,
            campaign_id,
            self.cleanup_campaign_files_on_finish,
        )

    async def _sleep_with_progress(self, campaign_id: int, delay: float, progress, body: str, countdown_label: str):
        remaining = max(0, int(delay))
        while remaining > 0:
            await self._wait_if_paused(campaign_id, progress)
            await progress.update(
                f"{body}\n{countdown_label}: {remaining}s.",
                campaign_controls(campaign_id, "running"),
            )
            step = min(self.progress_update_interval_seconds, remaining)
            await asyncio.sleep(step)
            remaining -= step


def parse_window(raw: Optional[str]):
    if not raw:
        return None
    start_raw, end_raw = raw.split("-", 1)
    return parse_time(start_raw), parse_time(end_raw)


def parse_time(raw: str) -> time:
    hour, minute = raw.strip().split(":", 1)
    return time(int(hour), int(minute))


def is_inside_window(window) -> bool:
    start, end = window
    now = datetime.now().time()
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


class ProgressMessage:
    def __init__(self, telegram_app: Application, chat_id: int, message_id: Optional[int]):
        self.telegram_app = telegram_app
        self.chat_id = chat_id
        self.message_id = message_id
        self.last_text = ""
        self.last_markup = None

    async def update(self, text: str, reply_markup=None):
        if text == self.last_text and repr(reply_markup) == self.last_markup:
            return

        self.last_text = text
        self.last_markup = repr(reply_markup)
        try:
            if self.message_id:
                await self.telegram_app.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=text,
                    reply_markup=reply_markup,
                )
            else:
                message = await self.telegram_app.bot.send_message(
                    self.chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    disable_notification=True,
                )
                self.message_id = message.message_id
        except Exception:
            logger.debug("Nao foi possivel atualizar painel de progresso", exc_info=True)
