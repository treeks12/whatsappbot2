import base64
import io
import logging
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .config import Settings
from .csv_utils import mime_from_name, parse_contacts_file, parse_contacts_vcf_text, usable_phone
from .db import Database
from .evolution import EvolutionClient
from .profiles import get_profile
from .scheduler import CampaignScheduler, campaign_controls, cancel_confirmation_controls


logger = logging.getLogger(__name__)

WAITING_CSV, WAITING_MEDIA, WAITING_CAPTION = range(3)
PERM_STATE_KEY = "perm_state"


def register_handlers(application):
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("conexao", conexao))
    application.add_handler(CommandHandler("desconectar", desconectar))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("disparar", disparar))
    application.add_handler(CallbackQueryHandler(callback_handler))

    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler(["nova", "nova_confianca", "nova_precaucao"], nova)],
            states={
                WAITING_CSV: [
                    MessageHandler(filters.Document.ALL, receive_contacts_file),
                    MessageHandler(filters.CONTACT, receive_contact_card),
                    CommandHandler("pronto", contacts_done),
                ],
                WAITING_MEDIA: [
                    MessageHandler(filters.PHOTO, receive_photo),
                    MessageHandler(filters.Document.IMAGE, receive_image_document),
                    CommandHandler("pronto", media_done),
                    CommandHandler("sem_midia", media_done),
                ],
                WAITING_CAPTION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_caption),
                    CommandHandler("sem_texto", receive_no_caption),
                ],
            },
            fallbacks=[CommandHandler("cancelar", cancelar)],
            allow_reentry=True,
        )
    )
    application.add_handler(CommandHandler("cancelar", cancelar))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, perm_receive_name))


def services(context: ContextTypes.DEFAULT_TYPE) -> tuple[Settings, Database, EvolutionClient, CampaignScheduler]:
    return (
        context.application.bot_data["settings"],
        context.application.bot_data["db"],
        context.application.bot_data["evolution"],
        context.application.bot_data["scheduler"],
    )


def is_authorized(settings: Settings, user_id: int) -> bool:
    return not settings.telegram_admin_ids or user_id in settings.telegram_admin_ids


async def require_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings, db, _, _ = services(context)
    user_id = update.effective_user.id
    if is_authorized(settings, user_id):
        return True
    if await db.is_user_approved(user_id):
        return True
    await update.effective_message.reply_text("Acesso negado. Use /start para pedir permissão.")
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, _, _ = services(context)
    user_id = update.effective_user.id

    if is_authorized(settings, user_id) or await db.is_user_approved(user_id):
        await update.message.reply_text(
            "Bot v2 ativo.\n\n"
            "/login - conectar WhatsApp\n"
            "/conexao - verificar conexao do WhatsApp\n"
            "/desconectar - informa limite seguro da Evolution\n"
            "/nova - criar campanha com precaucao\n"
            "/nova_confianca - campanha para contatos de confianca\n"
            "/nova_precaucao - campanha mais cuidadosa\n"
            "/disparar - iniciar campanha pronta\n"
            "/status - ver ultimas campanhas\n"
            "/cancelar - cancelar campanha ativa"
        )
        return

    pending = await db.get_pending_request(user_id)
    if pending:
        await update.message.reply_text("Permissão já requisitada. Aguarde a aprovação.")
        return

    await update.message.reply_text(
        "Usuário desconhecido.\n\nDeseja pedir permissão?",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Sim", callback_data="perm_yes"),
                InlineKeyboardButton("Não", callback_data="perm_no"),
            ]
        ]),
    )


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return

    settings, db, evolution, _ = services(context)
    user = update.effective_user
    instance_name = f"vendor_{user.id}"
    await db.ensure_vendor(user.id, user.username or f"user_{user.id}", instance_name)

    await update.message.reply_text("Preparando conexao do WhatsApp...")
    state = await evolution.connection_state(instance_name)
    if state == "open":
        await update.message.reply_text("WhatsApp ja esta conectado para esta vendedora.")
        return

    try:
        qr_base64 = await evolution.ensure_fresh_qr(instance_name)
    except Exception as exc:
        logger.exception("Erro ao gerar QR")
        await update.message.reply_text(f"Erro ao gerar QR: {exc}")
        return

    image = base64.b64decode(qr_base64)
    await update.message.reply_photo(
        photo=io.BytesIO(image),
        caption="Escaneie em WhatsApp > Aparelhos conectados > Conectar aparelho.",
    )


async def conexao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return

    _, db, evolution, _ = services(context)
    user = update.effective_user
    instance_name = f"vendor_{user.id}"
    await db.ensure_vendor(user.id, user.username or f"user_{user.id}", instance_name)
    state = await evolution.connection_state(instance_name)
    await update.message.reply_text(f"Conexao WhatsApp: {state}.")


async def desconectar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return

    await update.message.reply_text(
        "A Evolution 2.4 nesta instalacao nao expoe um endpoint seguro para apenas fechar o socket mantendo a sessao.\n"
        "O endpoint disponivel e logout, que pode exigir QR novamente. Por isso o bot nao desconecta automaticamente."
    )


async def nova(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return ConversationHandler.END

    settings, db, evolution, _ = services(context)
    user = update.effective_user
    instance_name = f"vendor_{user.id}"
    await db.ensure_vendor(user.id, user.username or f"user_{user.id}", instance_name)

    active = await db.get_active_campaign_for_vendor(user.id)
    if active:
        await update.message.reply_text(
            f"Voce ja tem uma campanha ativa #{active['id']} ({active['status']}). "
            "Use /cancelar para descartar ou /disparar se ela estiver pronta."
        )
        return ConversationHandler.END

    state = await evolution.connection_state(instance_name)
    if state != "open":
        await update.message.reply_text("WhatsApp ainda nao esta conectado. Preparando QR...")
        try:
            qr_base64 = await evolution.ensure_fresh_qr(instance_name)
        except Exception as exc:
            logger.exception("Erro ao gerar QR")
            await update.message.reply_text(f"Erro ao gerar QR: {exc}")
            return ConversationHandler.END
        if qr_base64:
            image = base64.b64decode(qr_base64)
            await update.message.reply_photo(
                photo=io.BytesIO(image),
                caption="Escaneie o QR e rode /nova novamente quando a conexao estiver aberta.",
            )
        else:
            await update.message.reply_text("Conexao ainda nao abriu. Rode /conexao para verificar.")
        return ConversationHandler.END

    profile_id = profile_from_command(update, settings.default_profile)
    profile = get_profile(profile_id)
    contact_limit = contact_limit_for_profile(settings, profile.id)
    campaign_id = await db.create_campaign(user.id, profile.id)
    context.user_data["campaign_id"] = campaign_id
    campaign_dir(settings, campaign_id).mkdir(parents=True, exist_ok=True)

    await update.message.reply_text(
        f"Campanha #{campaign_id} criada.\n"
        f"Perfil: {profile.label}.\n"
        f"Envie um CSV, VCF bruto, ou ZIP com CSV/VCF dentro.\n"
        f"Limite: {contact_limit} contatos.\n"
        "Se o Telegram transformar em cartoes de contato, use /pronto quando terminar."
    )
    return WAITING_CSV


def profile_from_command(update: Update, default_profile: str) -> str:
    text = update.message.text or ""
    command = text.split()[0].lstrip("/").split("@", 1)[0]
    if command == "nova_confianca":
        return "confianca_100"
    if command in ("nova", "nova_precaucao"):
        return "precaucao_100"
    return default_profile


def contact_limit_for_profile(settings: Settings, profile_id: str) -> int:
    if profile_id == "confianca_100":
        return settings.max_trusted_clients_per_campaign
    if profile_id == "precaucao_100":
        return settings.max_precaution_clients_per_campaign
    return settings.max_clients_per_campaign


async def contact_limit_for_campaign(settings: Settings, db: Database, campaign_id: int) -> int:
    campaign = await db.get_campaign(campaign_id)
    if not campaign:
        return settings.max_clients_per_campaign
    return contact_limit_for_profile(settings, campaign["profile_id"])


async def receive_contacts_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, _, _ = services(context)
    campaign_id = context.user_data.get("campaign_id")
    if not campaign_id:
        await update.message.reply_text("Campanha nao encontrada. Use /nova.")
        return ConversationHandler.END

    document = update.message.document
    file_name = document.file_name or ""
    suffix = Path(file_name).suffix.lower()
    if not document or suffix not in (".csv", ".vcf", ".zip"):
        await update.message.reply_text("Envie um arquivo .csv, .vcf ou .zip.")
        return WAITING_CSV

    path = campaign_dir(settings, campaign_id) / f"contatos{suffix}"
    file = await context.bot.get_file(document.file_id)
    await file.download_to_drive(path)

    try:
        contact_limit = await contact_limit_for_campaign(settings, db, campaign_id)
        contacts = parse_contacts_file(path, contact_limit)
    except Exception as exc:
        await update.message.reply_text(str(exc))
        return WAITING_CSV

    total = await db.add_contacts(campaign_id, contacts)
    await update.message.reply_text(
        f"Contatos importados: {total}.\n"
        "Agora envie imagens, uma por vez, ou use /sem_midia. Quando terminar, use /pronto."
    )
    return WAITING_MEDIA


async def receive_contact_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, _, _ = services(context)
    campaign_id = context.user_data.get("campaign_id")
    if not campaign_id:
        await update.message.reply_text("Campanha nao encontrada. Use /nova.")
        return ConversationHandler.END

    campaign = await db.get_campaign(campaign_id)
    contact_limit = await contact_limit_for_campaign(settings, db, campaign_id)
    if campaign and campaign["total_contacts"] >= contact_limit:
        await update.message.reply_text(f"Limite de {contact_limit} contatos atingido. Use /pronto.")
        return WAITING_CSV

    contact = update.message.contact
    full_name = " ".join(item for item in [contact.first_name, contact.last_name] if item).strip() or "Cliente"
    contacts = []

    if contact.vcard:
        contacts = parse_contacts_vcf_text(contact.vcard)

    if not contacts:
        phone = usable_phone(contact.phone_number)
        if phone:
            contacts = [{"row_index": campaign["total_contacts"] if campaign else 0, "name": full_name, "phone": phone}]

    if not contacts:
        await update.message.reply_text(
            f"Contato '{full_name}' veio sem DDD ou sem numero completo. "
            "Envie o .vcf como arquivo/documento, ou um CSV com telefone completo."
        )
        return WAITING_CSV

    remaining = contact_limit - (campaign["total_contacts"] if campaign else 0)
    total = await db.add_contacts(campaign_id, contacts[:remaining])
    await update.message.reply_text(f"Contato recebido. Total da campanha: {total}. Use /pronto quando terminar.")
    return WAITING_CSV


async def contacts_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, db, _, _ = services(context)
    campaign_id = context.user_data.get("campaign_id")
    if not campaign_id:
        await update.message.reply_text("Campanha nao encontrada. Use /nova.")
        return ConversationHandler.END

    campaign = await db.get_campaign(campaign_id)
    if not campaign or campaign["total_contacts"] <= 0:
        await update.message.reply_text("Nenhum contato valido importado ainda.")
        return WAITING_CSV

    await update.message.reply_text(
        f"Contatos confirmados: {campaign['total_contacts']}.\n"
        "Agora envie imagens, uma por vez, ou use /sem_midia. Quando terminar, use /pronto."
    )
    return WAITING_MEDIA


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, _, _ = services(context)
    campaign_id = context.user_data.get("campaign_id")
    if not campaign_id:
        return ConversationHandler.END

    count = await db.count_media(campaign_id)
    if count >= settings.max_media_per_campaign:
        await update.message.reply_text(f"Limite de {settings.max_media_per_campaign} imagens atingido. Use /pronto.")
        return WAITING_MEDIA

    photo = update.message.photo[-1]
    if not media_size_allowed(photo.file_size, settings.max_media_file_mb):
        await update.message.reply_text(f"Imagem grande demais. Limite atual: {settings.max_media_file_mb} MB.")
        return WAITING_MEDIA

    file = await context.bot.get_file(photo.file_id)
    file_name = f"image_{count + 1}.jpg"
    path = campaign_dir(settings, campaign_id) / file_name
    await file.download_to_drive(path)
    await db.add_media(campaign_id, str(path), "image/jpeg", file_name)
    await update.message.reply_text(f"Imagem {count + 1} recebida. Envie outra ou use /pronto.")
    return WAITING_MEDIA


async def receive_image_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, _, _ = services(context)
    campaign_id = context.user_data.get("campaign_id")
    if not campaign_id:
        return ConversationHandler.END

    count = await db.count_media(campaign_id)
    if count >= settings.max_media_per_campaign:
        await update.message.reply_text(f"Limite de {settings.max_media_per_campaign} imagens atingido. Use /pronto.")
        return WAITING_MEDIA

    document = update.message.document
    if not media_size_allowed(document.file_size, settings.max_media_file_mb):
        await update.message.reply_text(f"Imagem grande demais. Limite atual: {settings.max_media_file_mb} MB.")
        return WAITING_MEDIA

    file_name = document.file_name or f"image_{count + 1}.jpg"
    path = campaign_dir(settings, campaign_id) / file_name
    file = await context.bot.get_file(document.file_id)
    await file.download_to_drive(path)
    await db.add_media(campaign_id, str(path), mime_from_name(file_name), file_name)
    await update.message.reply_text(f"Imagem {count + 1} recebida. Envie outra ou use /pronto.")
    return WAITING_MEDIA


def media_size_allowed(file_size: int | None, max_mb: int) -> bool:
    if not file_size:
        return True
    return file_size <= max_mb * 1024 * 1024


async def media_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Envie agora a legenda, ou use /sem_texto para enviar somente a imagem.")
    return WAITING_CAPTION


async def receive_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, db, _, _ = services(context)
    campaign_id = context.user_data.get("campaign_id")
    if not campaign_id:
        return ConversationHandler.END

    await db.set_caption(campaign_id, update.message.text)
    await reply_campaign_ready(update, db, campaign_id)
    return ConversationHandler.END


async def receive_no_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, db, _, _ = services(context)
    campaign_id = context.user_data.get("campaign_id")
    if not campaign_id:
        return ConversationHandler.END

    media_count = await db.count_media(campaign_id)
    if media_count <= 0:
        await update.message.reply_text("Sem imagem e sem texto nao ha o que enviar. Envie uma legenda.")
        return WAITING_CAPTION

    await db.set_caption(campaign_id, "")
    await reply_campaign_ready(update, db, campaign_id)
    return ConversationHandler.END


async def reply_campaign_ready(update: Update, db: Database, campaign_id: int):
    campaign = await db.get_campaign(campaign_id)
    await update.message.reply_text(
        f"Campanha #{campaign_id} pronta.\n"
        f"Contatos: {campaign['total_contacts']}\n"
        "Use /disparar para iniciar."
    )


async def disparar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return

    _, db, _, scheduler = services(context)
    campaign = await db.get_active_campaign_for_vendor(update.effective_user.id)
    if not campaign:
        await update.message.reply_text("Nenhuma campanha ativa.")
        return

    if campaign["status"] != "ready":
        await update.message.reply_text(f"Campanha #{campaign['id']} esta em status {campaign['status']}.")
        return

    progress_message = await update.message.reply_text("Preparando campanha...")
    ok = await scheduler.start(campaign["id"], update.effective_chat.id, progress_message.message_id)
    if not ok:
        await progress_message.edit_text("Campanha ja esta em execucao.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return

    _, db, _, _ = services(context)
    rows = await db.campaign_summary_for_vendor(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Nenhuma campanha encontrada.")
        return

    lines = ["Ultimas campanhas:"]
    for row in rows:
        processed = row["processed_count"] or 0
        failed = row["failed_count"] or 0
        lines.append(
            f"#{row['id']} {row['status']} - "
            f"processados {processed}/{row['total_contacts']} | enviados {row['sent_count']} | falhas {failed}"
        )
    await update.message.reply_text("\n".join(lines))


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return ConversationHandler.END

    _, db, _, scheduler = services(context)
    ok = await scheduler.cancel_vendor_campaign(update.effective_user.id)
    context.user_data.pop("campaign_id", None)
    await update.effective_message.reply_text("Campanha cancelada." if ok else "Nenhuma campanha ativa para cancelar.")
    return ConversationHandler.END


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("campaign_"):
        await handle_campaign_callback(update, context)
        return

    if data == "perm_yes":
        await query.answer()
        context.user_data[PERM_STATE_KEY] = "waiting_name"
        await query.edit_message_text("Escreva o seu nome:")
        return

    if data == "perm_no":
        await query.answer()
        await query.edit_message_text("Operação cancelada.")
        context.user_data.pop(PERM_STATE_KEY, None)
        return

    if data.startswith("approve_"):
        await query.answer()
        target_id = int(data.split("_", 1)[1])
        settings, db, _, _ = services(context)
        request = await db.get_pending_request(target_id)
        if not request:
            await query.edit_message_text("Solicitação não encontrada ou já processada.")
            return
        await db.approve_user(target_id, query.from_user.id)
        await query.edit_message_text(f"{request['name']} (ID {target_id}) aprovado.")
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="Sua permissão foi aprovada! Use /start para começar.",
            )
        except Exception:
            pass
        return

    if data.startswith("reject_"):
        await query.answer()
        target_id = int(data.split("_", 1)[1])
        settings, db, _, _ = services(context)
        request = await db.get_pending_request(target_id)
        if not request:
            await query.edit_message_text("Solicitação não encontrada ou já processada.")
            return
        await db.reject_user(target_id)
        await query.edit_message_text(f"ID {target_id} recusado.")
        return

    await query.answer()


async def handle_campaign_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    settings, db, _, scheduler = services(context)

    try:
        action, raw_campaign_id = data.split(":", 1)
        campaign_id = int(raw_campaign_id)
    except ValueError:
        await query.answer("Acao invalida.", show_alert=True)
        return

    campaign = await db.get_campaign(campaign_id)
    if not campaign:
        await query.answer("Campanha nao encontrada.", show_alert=True)
        return

    is_owner = campaign["vendor_id"] == query.from_user.id
    if not is_owner and not is_authorized(settings, query.from_user.id):
        await query.answer("Sem permissao para controlar esta campanha.", show_alert=True)
        return

    if action == "campaign_pause":
        ok = await scheduler.pause_campaign(campaign_id)
        await query.answer("Campanha pausada." if ok else "Nao foi possivel pausar.")
        if ok:
            await query.edit_message_text(
                f"Campanha #{campaign_id} pausada.\nUse Retomar para continuar.",
                reply_markup=campaign_controls(campaign_id, paused=True),
            )
        return

    if action == "campaign_resume":
        ok = await scheduler.resume_campaign(campaign_id)
        await query.answer("Campanha retomada." if ok else "Nao foi possivel retomar.")
        if ok:
            await query.edit_message_text(
                f"Campanha #{campaign_id} retomada.\nAguardando proximo envio...",
                reply_markup=campaign_controls(campaign_id),
            )
        return

    if action == "campaign_cancel_ask":
        paused = campaign["status"] == "paused"
        await query.answer()
        await query.edit_message_text(
            f"Cancelar campanha #{campaign_id}?\nEssa acao nao envia mais contatos.",
            reply_markup=cancel_confirmation_controls(campaign_id, paused=paused),
        )
        return

    if action == "campaign_cancel_no":
        await query.answer("Cancelamento descartado.")
        await query.edit_message_text(
            f"Campanha #{campaign_id} continua em andamento.",
            reply_markup=campaign_controls(campaign_id),
        )
        return

    if action == "campaign_cancel_no_paused":
        await query.answer("Cancelamento descartado.")
        await query.edit_message_text(
            f"Campanha #{campaign_id} continua pausada.",
            reply_markup=campaign_controls(campaign_id, paused=True),
        )
        return

    if action == "campaign_cancel_yes":
        ok = await scheduler.cancel_campaign(campaign_id)
        await query.answer("Campanha cancelada." if ok else "Nao foi possivel cancelar.")
        await query.edit_message_text(
            f"Campanha #{campaign_id} cancelada." if ok else f"Campanha #{campaign_id} nao estava ativa."
        )
        return

    await query.answer("Acao desconhecida.", show_alert=True)


async def perm_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get(PERM_STATE_KEY) != "waiting_name":
        return

    name = update.message.text.strip()[:100]
    if not name:
        await update.message.reply_text("Nome inválido. Tente novamente:")
        return

    user = update.effective_user
    settings, db, _, _ = services(context)

    context.user_data.pop(PERM_STATE_KEY, None)
    await db.request_access(user.id, user.username or "", name)

    for admin_id in settings.telegram_admin_ids:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"ID {user.id} - {name} pediu permissão, aceitar?",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("Aceitar", callback_data=f"approve_{user.id}"),
                        InlineKeyboardButton("Recusar", callback_data=f"reject_{user.id}"),
                    ]
                ]),
            )
        except Exception:
            pass

    await update.message.reply_text("Permissão requisitada. Aguarde a aprovação.")


def campaign_dir(settings: Settings, campaign_id: int) -> Path:
    return settings.campaigns_dir / str(campaign_id)
