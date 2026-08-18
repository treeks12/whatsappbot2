import asyncio
import base64
import logging
import random
import re
import secrets
import struct
import uuid
import zlib
from datetime import datetime, time
from pathlib import Path
from typing import Dict, Optional, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from .cleanup import cleanup_campaign_payload
from .db import Database
from .evolution import EvolutionClient, EvolutionError
from .profiles import get_profile


logger = logging.getLogger(__name__)


SPINTAX_RE = re.compile(r"\{([^{}|]+(?:\|[^{}|]+)+)\}")
PRESENCE_MS_PER_CHAR = 40
PRESENCE_MIN_DELAY_MS = 1500
PRESENCE_MAX_DELAY_MS = 8000


# --- Shadowban / restricao auto-pause -----------------------------------------

# Quando o score de suspeita atinge esse valor, a campanha pausa sozinha.
SUSPICION_LIMIT = 5
# Bumps por categoria de evento.
SUSPICION_BUMP = {
    "shadowban": 3,
    "not_authorized": 3,
    "rate_limit": 2,
    "connection": 1,
    "generic": 1,
    "connection_close_event": 1,  # vindo do webhook
}
# Decay aplicado por envio bem-sucedido.
SUSPICION_DECAY_ON_OK = 1


class SuspicionTracker:
    """Score por instancia (vendor_<id>) usado pra detectar shadowban precoce.

    Compartilhado entre `CampaignScheduler` (que faz bump em erros e decay em
    sucessos) e `WebhookServer` (que faz bump quando recebe `connection.update`
    com state=close).
    """

    def __init__(self):
        self._scores: Dict[str, int] = {}

    def get(self, instance_name: str) -> int:
        return self._scores.get(instance_name, 0)

    def bump(self, instance_name: str, category: str) -> int:
        amount = SUSPICION_BUMP.get(category, 1)
        new = self._scores.get(instance_name, 0) + amount
        self._scores[instance_name] = new
        return new

    def decay(self, instance_name: str, amount: int = SUSPICION_DECAY_ON_OK) -> int:
        new = max(0, self._scores.get(instance_name, 0) - amount)
        if new == 0:
            self._scores.pop(instance_name, None)
        else:
            self._scores[instance_name] = new
        return new

    def reset(self, instance_name: str):
        self._scores.pop(instance_name, None)


class CampaignConnectionLost(RuntimeError):
    pass


def available_memory_mb() -> float:
    try:
        info = Path("/proc/meminfo").read_text()
        for line in info.splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except Exception:
        pass
    return 9999


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
        evolution_power: Optional[Any] = None,
        suspicion_tracker: Optional[SuspicionTracker] = None,
    ):
        self.db = db
        self.evolution = evolution
        self.telegram_app = telegram_app
        self.send_window = parse_window(send_window)
        self.progress_update_interval_seconds = max(2, progress_update_interval_seconds)
        self.campaigns_dir = campaigns_dir
        self.cleanup_campaign_files_on_finish = cleanup_campaign_files_on_finish
        self.min_free_memory_mb = min_free_memory_mb
        self.evolution_power = evolution_power
        self.suspicion = suspicion_tracker or SuspicionTracker()
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
        task.add_done_callback(lambda done_task, cid=campaign_id: self._clear_task(cid, done_task))
        return True

    def _clear_task(self, campaign_id: int, task: asyncio.Task):
        if self.tasks.get(campaign_id) is task:
            self.tasks.pop(campaign_id, None)

    async def cancel_vendor_campaign(self, vendor_id: int) -> bool:
        campaign = await self.db.get_active_campaign_for_vendor(vendor_id)
        if not campaign:
            return False

        return await self.cancel_campaign(campaign["id"])

    async def cancel_campaign(self, campaign_id: int) -> bool:
        campaign = await self.db.get_campaign(campaign_id)
        if not campaign or campaign["status"] not in ("draft", "ready", "running", "paused"):
            return False

        task = self.tasks.get(campaign["id"])
        if task and not task.done():
            task.cancel()

        await self.db.finish_campaign(campaign_id, "cancelled")
        await self._cleanup_campaign_payload(campaign_id)
        await self._stop_evolution_if_idle()
        return True

    async def pause_campaign(self, campaign_id: int) -> bool:
        campaign = await self.db.get_campaign(campaign_id)
        if not campaign or campaign["status"] != "running":
            return False
        await self.db.set_campaign_status(campaign_id, "paused")
        return True

    async def resume_campaign(self, campaign_id: int) -> bool:
        campaign = await self.db.get_campaign(campaign_id)
        if not campaign or campaign["status"] != "paused":
            return False
        await self.db.set_campaign_status(campaign_id, "running")
        # Da fresh start ao tracker. Se o problema voltar, o auto-pause volta.
        campaign_with_vendor = await self.db.get_campaign_with_vendor(campaign_id)
        if campaign_with_vendor:
            self.suspicion.reset(campaign_with_vendor["instance_name"])
        return True

    async def _run_campaign(self, campaign_id: int, progress_chat_id: Optional[int], progress_message_id: Optional[int]):
        campaign = await self.db.get_campaign_with_vendor(campaign_id)
        if not campaign:
            return

        instance_name = campaign["instance_name"]
        lock = self.instance_locks.setdefault(instance_name, asyncio.Lock())
        progress = ProgressMessage(self.telegram_app, progress_chat_id or campaign["vendor_id"], progress_message_id)

        async with lock:
            profile = get_profile(campaign["profile_id"])
            self.suspicion.reset(instance_name)

            await progress.update(
                f"Campanha #{campaign_id} iniciando.\n"
                f"Contatos: 0/{campaign['total_contacts']}\n"
                "Ligando conexao do WhatsApp...",
                campaign_controls(campaign_id),
            )

            try:
                await self._ensure_evolution_running()
                await self._ensure_connection_open(instance_name)
                await self.db.start_campaign(campaign_id)
                media_items = await self.db.get_media(campaign_id)
                media_cache: Dict[str, bytes] = {}
                await progress.update(
                    f"Campanha #{campaign_id} iniciada.\n"
                    f"Contatos: 0/{campaign['total_contacts']}\n"
                    "Preparando primeiro envio...",
                    campaign_controls(campaign_id),
                )

                while True:
                    await self._wait_for_window()
                    await self._wait_if_paused(campaign_id, progress)
                    await self._ensure_connection_open(instance_name)
                    contact = await self.db.next_pending_contact(campaign_id)
                    if not contact:
                        await self.db.finish_campaign(campaign_id, "completed")
                        await self._cleanup_campaign_payload(campaign_id)
                        counts = await self.db.campaign_progress(campaign_id)
                        await progress.update(
                            f"Campanha #{campaign_id} concluida.\n"
                            f"Enviados: {counts['sent']}/{counts['total']}\n"
                            f"Falhas: {counts['failed']}",
                            None,
                        )
                        return

                    # Re-check de blacklist: o numero pode ter sido bloqueado depois
                    # que a campanha foi montada (corrida com o purge das filas).
                    if await self.db.is_phone_blacklisted(contact["phone"]):
                        await self.db.mark_contact_failed(campaign_id, contact["id"], "blacklist")
                        logger.info(
                            "Contato %s pulado: telefone entrou na blacklist durante o disparo.",
                            contact["id"],
                        )
                        continue

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
                        campaign_controls(campaign_id),
                    )

                    try:
                        try:
                            await self.db.record_send_attempt(campaign["vendor_id"], contact["phone"])
                        except Exception:
                            logger.warning("Falha ao registrar tentativa para %s", contact["id"], exc_info=True)
                        await self._send_contact(campaign, contact, profile, media_items, media_cache)
                        await self.db.mark_contact_sent(campaign_id, contact["id"], contact["phone"])
                        self.suspicion.decay(instance_name)
                        result_line = f"Contato {current_number}/{total} enviado."
                    except Exception as exc:
                        if await self._connection_closed(instance_name):
                            raise CampaignConnectionLost("WhatsApp desconectou durante a campanha.") from exc
                        category = exc.category if isinstance(exc, EvolutionError) else "generic"
                        score = self.suspicion.bump(instance_name, category)
                        logger.warning(
                            "Erro ao enviar contato %s (cat=%s, suspicion=%d)",
                            contact["id"], category, score,
                        )
                        await self.db.mark_contact_failed(campaign_id, contact["id"], f"[{category}] {exc}")
                        result_line = f"Contato {current_number}/{total} falhou ({category}). Pulando para o proximo."

                    counts = await self.db.campaign_progress(campaign_id)
                    processed_count = counts["processed"]
                    sent_count = counts["sent"]
                    total = counts["total"] or total

                    # Auto-pause se a contagem de sinais ultrapassou o limite (incluindo
                    # bumps vindos do webhook de connection.update entre envios).
                    if self.suspicion.get(instance_name) >= SUSPICION_LIMIT and campaign["status"] != "paused":
                        await self._auto_pause_for_shadowban(campaign_id, instance_name, progress, counts)
                        # _wait_if_paused na proxima iteracao segura aqui.
                        continue

                    if processed_count and processed_count % profile.pause_every == 0:
                        delay = profile.pause()
                        await self._sleep_with_progress(
                            campaign_id,
                            delay,
                            progress,
                            f"Campanha #{campaign_id} em pausa.\n"
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
                await progress.update(f"Campanha #{campaign_id} cancelada.", None)
                raise
            except CampaignConnectionLost as exc:
                logger.warning("Campanha %s interrompida por desconexao: %s", campaign_id, exc)
                await self.db.finish_campaign(campaign_id, "failed")
                await self._cleanup_campaign_payload(campaign_id)
                await progress.update(
                    f"Campanha #{campaign_id} interrompida.\n"
                    "WhatsApp desconectou antes de continuar. Nenhum contato restante foi marcado como falha.",
                    None,
                )
            except Exception:
                logger.exception("Erro fatal na campanha %s", campaign_id)
                await self.db.finish_campaign(campaign_id, "failed")
                await self._cleanup_campaign_payload(campaign_id)
                await progress.update(f"Campanha #{campaign_id} falhou.", None)
            finally:
                await self._stop_evolution_if_idle()

    async def _send_contact(self, campaign, contact, profile, media_items, media_cache: Dict[str, bytes]):
        await self._wait_for_memory()
        text = render_caption(
            campaign["caption"] or "",
            contact["name"] or "Cliente",
        )

        for index, media in enumerate(media_items):
            is_last_media = index == len(media_items) - 1
            await self._wait_for_memory()
            raw_media = media_cache.get(media["path"])
            if raw_media is None:
                raw_media = await read_media_bytes(media["path"])
                media_cache[media["path"]] = raw_media
            outbound_media = mutate_media_bytes(raw_media, media["mime_type"])
            media_base64 = base64.b64encode(outbound_media).decode("ascii")
            file_name = random_media_file_name(media["file_name"], media["mime_type"])
            if text and is_last_media:
                await self._send_typing_presence(campaign["instance_name"], contact["phone"], text)
            await self.evolution.send_media(
                campaign["instance_name"],
                contact["phone"],
                media["path"],
                media["mime_type"],
                file_name,
                text if is_last_media else "",
                media_base64,
            )
            if index < len(media_items) - 1:
                await asyncio.sleep(profile.between_media())

        if text and not media_items:
            await asyncio.sleep(profile.before_text())
            await self._send_typing_presence(campaign["instance_name"], contact["phone"], text)
            await self.evolution.send_text(campaign["instance_name"], contact["phone"], text)

    async def _send_typing_presence(self, instance_name: str, phone: str, text: str):
        delay = typing_delay_ms(text)
        try:
            await self.evolution.send_presence(instance_name, phone, "composing", delay)
        except EvolutionError:
            logger.warning("Evolution nao aceitou sendPresence para %s", phone, exc_info=True)

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
            await progress.update(
                f"Campanha #{campaign_id} pausada.\nUse Retomar para continuar.",
                campaign_controls(campaign_id, paused=True),
            )
            await asyncio.sleep(self.progress_update_interval_seconds)

    async def _auto_pause_for_shadowban(self, campaign_id: int, instance_name: str, progress, counts: dict):
        """Pausa a campanha automaticamente ao detectar sinais de restricao.

        Mantem a sessao do WhatsApp viva. Envia mensagem instrutiva pra vendedora
        explicando o que NAO fazer (recriar instancia/desconectar) e o que fazer
        (esperar e usar o numero manualmente).
        """
        score = self.suspicion.get(instance_name)
        await self.db.set_campaign_status(campaign_id, "paused")
        logger.warning(
            "Auto-pause em campanha %s (instance=%s, suspicion=%d)",
            campaign_id, instance_name, score,
        )
        text = (
            f"Campanha #{campaign_id} pausada AUTOMATICAMENTE.\n\n"
            f"O numero apresentou sinais de restricao ou instabilidade (score {score}).\n\n"
            f"Progresso ate aqui: {counts['processed']}/{counts['total']} "
            f"(enviados {counts['sent']}, falhas {counts['failed']}).\n\n"
            "O QUE FAZER:\n"
            "- NAO use /desconectar nem refaca /login.\n"
            "- NAO recrie a instancia.\n"
            "- Abra o WhatsApp normalmente no celular e mande mensagens manuais "
            "para alguns contatos proximos por um tempo.\n"
            "- Aguarde algumas horas (geralmente 4 a 12h).\n"
            "- Quando voltar ao normal, clique em Retomar abaixo."
        )
        await progress.update(text, campaign_controls(campaign_id, paused=True))

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

    async def _ensure_evolution_running(self):
        if self.evolution_power:
            await self.evolution_power.ensure_running()

    async def _ensure_connection_open(self, instance_name: str):
        if await self._connection_closed(instance_name):
            raise CampaignConnectionLost("WhatsApp nao esta conectado.")

    async def _connection_closed(self, instance_name: str) -> bool:
        state = await self.evolution.connection_state(instance_name)
        return state != "open"

    async def _stop_evolution_if_idle(self):
        if self.evolution_power:
            await self.evolution_power.stop_if_idle(self.db)

    async def _sleep_with_progress(self, campaign_id: int, delay: float, progress, body: str, countdown_label: str):
        remaining = max(0, int(delay))
        while remaining > 0:
            await self._wait_if_paused(campaign_id, progress)
            await progress.update(
                f"{body}\n{countdown_label}: {remaining}s.",
                campaign_controls(campaign_id),
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


def render_caption(template: str, contact_name: str) -> str:
    def replace_spintax(match):
        options = [option.strip() for option in match.group(1).split("|")]
        options = [option for option in options if option]
        return random.choice(options) if options else match.group(0)

    spun = SPINTAX_RE.sub(replace_spintax, template)
    return spun.replace("{nome}", contact_name)


def typing_delay_ms(text: str) -> int:
    delay = len(text) * PRESENCE_MS_PER_CHAR
    return max(PRESENCE_MIN_DELAY_MS, min(PRESENCE_MAX_DELAY_MS, delay))


async def read_media_bytes(path: str) -> bytes:
    return await asyncio.to_thread(Path(path).read_bytes)


def mutate_media_bytes(raw: bytes, mime_type: str) -> bytes:
    mime = (mime_type or "").lower()
    if "jpeg" in mime or "jpg" in mime or raw.startswith(b"\xff\xd8"):
        return mutate_jpeg_comment(raw)
    if "png" in mime or raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return mutate_png_text_chunk(raw)
    return raw


def mutate_jpeg_comment(raw: bytes) -> bytes:
    if len(raw) < 4 or raw[-2:] != b"\xff\xd9":
        return raw
    comment = secrets.token_bytes(random.randint(8, 24))
    length = struct.pack(">H", len(comment) + 2)
    return raw[:-2] + b"\xff\xfe" + length + comment + b"\xff\xd9"


def mutate_png_text_chunk(raw: bytes) -> bytes:
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return raw
    iend_type_pos = raw.rfind(b"IEND")
    if iend_type_pos < 4:
        return raw
    iend_chunk_pos = iend_type_pos - 4
    chunk_type = b"tEXt"
    chunk_data = b"variant\x00" + secrets.token_hex(8).encode("ascii")
    length = struct.pack(">I", len(chunk_data))
    crc = struct.pack(">I", zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF)
    chunk = length + chunk_type + chunk_data + crc
    return raw[:iend_chunk_pos] + chunk + raw[iend_chunk_pos:]


def random_media_file_name(original_name: str, mime_type: str) -> str:
    suffix = Path(original_name or "").suffix.lower()
    if not suffix:
        mime = (mime_type or "").lower()
        if "png" in mime:
            suffix = ".png"
        elif "webp" in mime:
            suffix = ".webp"
        else:
            suffix = ".jpg"
    return f"{uuid.uuid4().hex[:12]}{suffix}"


class ProgressMessage:
    def __init__(self, telegram_app: Application, chat_id: int, message_id: Optional[int]):
        self.telegram_app = telegram_app
        self.chat_id = chat_id
        self.message_id = message_id
        self.last_text = ""
        self.last_markup = None

    async def update(self, text: str, reply_markup=None):
        markup_key = str(reply_markup.to_dict()) if reply_markup else ""
        if text == self.last_text and markup_key == self.last_markup:
            return

        self.last_text = text
        self.last_markup = markup_key
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
                    text,
                    disable_notification=True,
                    reply_markup=reply_markup,
                )
                self.message_id = message.message_id
        except Exception:
            logger.debug("Nao foi possivel atualizar painel de progresso", exc_info=True)


def campaign_controls(campaign_id: int, paused: bool = False) -> InlineKeyboardMarkup:
    if paused:
        first = InlineKeyboardButton("Retomar", callback_data=f"campaign_resume:{campaign_id}")
    else:
        first = InlineKeyboardButton("Pausar", callback_data=f"campaign_pause:{campaign_id}")
    return InlineKeyboardMarkup(
        [
            [first, InlineKeyboardButton("Cancelar", callback_data=f"campaign_cancel_ask:{campaign_id}")],
            [InlineKeyboardButton("Adicionar ultimo a blacklist", callback_data=f"bl_add_last:{campaign_id}")],
        ]
    )


def cancel_confirmation_controls(campaign_id: int, paused: bool = False) -> InlineKeyboardMarkup:
    keep_label = "Manter pausada" if paused else "Continuar"
    keep_action = "campaign_cancel_no_paused" if paused else "campaign_cancel_no"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirmar cancelamento", callback_data=f"campaign_cancel_yes:{campaign_id}"),
            ],
            [
                InlineKeyboardButton(keep_label, callback_data=f"{keep_action}:{campaign_id}"),
            ],
        ]
    )
