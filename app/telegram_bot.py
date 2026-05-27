import base64
import io
import logging
import asyncio
import csv
import warnings
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.warnings import PTBUserWarning
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
from .evolution_power import EvolutionPowerManager
from .profiles import get_profile
from .scheduler import CampaignScheduler, campaign_controls, cancel_confirmation_controls


logger = logging.getLogger(__name__)

(
    WAITING_CONTACT_SOURCE,
    WAITING_CSV,
    WAITING_MEDIA,
    WAITING_CAPTION,
    WAITING_LIST_NAME,
    WAITING_LIST_CONTACTS,
    WAITING_REMOVE_CONTACTS,
    WAITING_RENAME_LIST,
) = range(8)
PERM_STATE_KEY = "perm_state"


def register_handlers(application):
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("conexao", conexao))
    application.add_handler(CommandHandler("desconectar", desconectar))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("disparar", disparar))

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="If 'per_message=False'.*", category=PTBUserWarning)
        application.add_handler(
            ConversationHandler(
                entry_points=[
                    CommandHandler(["nova", "nova_confianca", "nova_precaucao"], nova),
                    CommandHandler("listas", listas),
                ],
                states={
                    WAITING_CONTACT_SOURCE: [
                        CallbackQueryHandler(handle_contact_source_callback, pattern="^(src_|list_|menu_)"),
                    ],
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
                    WAITING_LIST_NAME: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_list_name),
                    ],
                    WAITING_LIST_CONTACTS: [
                        MessageHandler(filters.Document.ALL, receive_list_contacts_file),
                        MessageHandler(filters.CONTACT, receive_list_contact_card),
                        CommandHandler("pronto", list_contacts_done),
                    ],
                    WAITING_REMOVE_CONTACTS: [
                        MessageHandler(filters.Document.ALL, receive_remove_contacts_file),
                        MessageHandler(filters.CONTACT, receive_remove_contact_card),
                        CommandHandler("pronto", list_remove_done),
                    ],
                    WAITING_RENAME_LIST: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_rename_list),
                    ],
                },
                fallbacks=[CommandHandler("cancelar", cancelar)],
                allow_reentry=True,
            )
        )
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(CommandHandler("cancelar", cancelar))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, perm_receive_name))
    application.add_error_handler(error_handler)


def services(context: ContextTypes.DEFAULT_TYPE) -> tuple[Settings, Database, EvolutionClient, CampaignScheduler]:
    return (
        context.application.bot_data["settings"],
        context.application.bot_data["db"],
        context.application.bot_data["evolution"],
        context.application.bot_data["scheduler"],
    )


def power_service(context: ContextTypes.DEFAULT_TYPE) -> EvolutionPowerManager:
    return context.application.bot_data["power"]


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
            "/desconectar - fechar conexao sem apagar sessao quando possivel\n"
            "/nova - criar campanha com precaucao\n"
            "/nova_confianca - campanha para contatos de confianca\n"
            "/nova_precaucao - campanha mais cuidadosa\n"
            "/listas - gerenciar listas de contatos\n"
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
    power = power_service(context)
    user = update.effective_user
    instance_name = f"vendor_{user.id}"
    await db.ensure_vendor(user.id, user.username or f"user_{user.id}", instance_name)

    await update.message.reply_text("Preparando conexao do WhatsApp...")
    try:
        await power.ensure_running()
    except Exception as exc:
        logger.exception("Erro ao ligar Evolution")
        await update.message.reply_text(f"Erro ao ligar Evolution: {exc}")
        return

    state = await evolution.connection_state(instance_name)
    if state == "open":
        await update.message.reply_text("WhatsApp ja esta conectado para esta vendedora.")
        schedule_idle_stop(context)
        return

    try:
        qr_base64 = await evolution.ensure_fresh_qr(instance_name)
    except Exception as exc:
        logger.exception("Erro ao gerar QR")
        await update.message.reply_text(f"Erro ao gerar QR: {exc}")
        return

    image = qr_photo_bytes(qr_base64)
    if not image:
        state = await evolution.connection_state(instance_name)
        if state == "open":
            await update.message.reply_text("WhatsApp ja esta conectado para esta vendedora.")
        else:
            await update.message.reply_text(
                f"Evolution respondeu sem QR e a conexao esta em '{state}'. Tente /login novamente em alguns segundos."
            )
        schedule_idle_stop(context)
        return

    await update.message.reply_photo(
        photo=io.BytesIO(image),
        caption="Escaneie em WhatsApp > Aparelhos conectados > Conectar aparelho.",
    )
    schedule_idle_stop(context)


async def conexao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return

    _, db, evolution, _ = services(context)
    power = power_service(context)
    user = update.effective_user
    instance_name = f"vendor_{user.id}"
    await db.ensure_vendor(user.id, user.username or f"user_{user.id}", instance_name)
    if power.enabled and await power.container_state() != "running":
        await update.message.reply_text("Evolution API: desligada. WhatsApp: sem socket ativo.")
        return
    try:
        await power.ensure_running()
    except Exception as exc:
        logger.exception("Erro ao verificar Evolution")
        await update.message.reply_text(f"Erro ao verificar Evolution: {exc}")
        return
    state = await evolution.connection_state(instance_name)
    await update.message.reply_text(f"Conexao WhatsApp: {state}.")
    schedule_idle_stop(context)


async def desconectar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return

    _, db, evolution, _ = services(context)
    power = power_service(context)
    user = update.effective_user
    instance_name = f"vendor_{user.id}"
    await db.ensure_vendor(user.id, user.username or f"user_{user.id}", instance_name)

    if await db.count_running_or_paused_campaigns() > 0:
        await update.message.reply_text("Existe campanha rodando/pausada. Cancele ou conclua antes de desconectar.")
        return

    if power.enabled and await power.container_state() != "running":
        await update.message.reply_text("Evolution API ja esta desligada. Sessao preservada.")
        return

    try:
        await power.ensure_running()
    except Exception as exc:
        logger.exception("Erro ao ligar Evolution")
        await update.message.reply_text(f"Erro ao ligar Evolution: {exc}")
        return
    if await evolution.disconnect_instance(instance_name):
        await update.message.reply_text("Disconnect enviado para esta sessao. Sessao preservada.")
        return

    if power.enabled:
        await power.stop_container()
        await update.message.reply_text(
            "A Evolution deste build nao aceitou disconnect por sessao. "
            "Desliguei o container como fallback, preservando a sessao."
        )
        return

    await update.message.reply_text(
        "A Evolution deste build nao aceitou disconnect por sessao e controle de container nao esta configurado."
    )


async def nova(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return ConversationHandler.END

    settings, db, evolution, _ = services(context)
    power = power_service(context)
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

    try:
        await power.ensure_running()
    except Exception as exc:
        logger.exception("Erro ao ligar Evolution")
        await update.message.reply_text(f"Erro ao ligar Evolution: {exc}")
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
        image = qr_photo_bytes(qr_base64)
        if image:
            await update.message.reply_photo(
                photo=io.BytesIO(image),
                caption="Escaneie o QR e rode /nova novamente quando a conexao estiver aberta.",
            )
            schedule_idle_stop(context)
            return ConversationHandler.END
        state = await evolution.connection_state(instance_name)
        if state == "open":
            profile_id = profile_from_command(update, settings.default_profile)
            context.user_data["pending_campaign_profile_id"] = profile_id
            await show_campaign_contact_source(update.message, db, user.id, profile_id)
            await power.stop_if_idle(db)
            return WAITING_CONTACT_SOURCE
        else:
            await update.message.reply_text(f"Conexao ainda nao abriu. Estado atual: {state}. Rode /conexao para verificar.")
        schedule_idle_stop(context)
        return ConversationHandler.END

    profile_id = profile_from_command(update, settings.default_profile)
    context.user_data["pending_campaign_profile_id"] = profile_id
    await show_campaign_contact_source(update.message, db, user.id, profile_id)
    schedule_idle_stop(context)
    return WAITING_CONTACT_SOURCE


async def listas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return ConversationHandler.END

    context.user_data.pop("pending_campaign_profile_id", None)
    await show_lists_menu(update.message, context.application.bot_data["db"], update.effective_user.id)
    return WAITING_CONTACT_SOURCE


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


def contact_limit_label(limit: int) -> str:
    return "sem limite fixo" if limit <= 0 else f"{limit} contatos"


async def show_campaign_contact_source(message, db: Database, vendor_id: int, profile_id: str):
    profile = get_profile(profile_id)
    rows = await db.list_contact_lists(vendor_id)
    keyboard = []
    if rows:
        keyboard.append([InlineKeyboardButton("Usar lista salva", callback_data="src_saved")])
    keyboard.append([InlineKeyboardButton("Criar nova lista", callback_data="src_new_list")])
    keyboard.append([InlineKeyboardButton("Carregar so para esta campanha", callback_data="src_temp")])
    await message.reply_text(
        f"Campanha: {profile.label}.\nComo deseja escolher os contatos?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_lists_menu(message, db: Database, vendor_id: int, *, edit: bool = False):
    rows = await db.list_contact_lists(vendor_id)
    keyboard = []
    for row in rows[:8]:
        keyboard.append([
            InlineKeyboardButton(
                f"{row['name']} - {row['total_contacts']}",
                callback_data=f"list_open:{row['id']}",
            )
        ])
    keyboard.append([InlineKeyboardButton("Criar nova lista", callback_data="list_new")])
    keyboard.append([InlineKeyboardButton("Sair", callback_data="list_exit")])
    text = "Suas listas:" if rows else "Voce ainda nao tem listas salvas."
    if len(rows) > 8:
        text += f"\nMostrando 8 de {len(rows)} listas."
    markup = InlineKeyboardMarkup(keyboard)
    if edit:
        await message.edit_message_text(text, reply_markup=markup)
    else:
        await message.reply_text(text, reply_markup=markup)


async def show_saved_lists_for_campaign(query, db: Database, vendor_id: int):
    rows = await db.list_contact_lists(vendor_id)
    if not rows:
        await query.edit_message_text(
            "Nenhuma lista salva ainda.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Criar nova lista", callback_data="src_new_list")],
                [InlineKeyboardButton("Carregar so para esta campanha", callback_data="src_temp")],
            ]),
        )
        return

    keyboard = [
        [InlineKeyboardButton(f"{row['name']} - {row['total_contacts']}", callback_data=f"src_pick:{row['id']}")]
        for row in rows[:8]
    ]
    keyboard.append([InlineKeyboardButton("Voltar", callback_data="src_back")])
    await query.edit_message_text("Escolha uma lista:", reply_markup=InlineKeyboardMarkup(keyboard))


async def show_selected_list_for_campaign(query, db: Database, vendor_id: int, list_id: int):
    row = await db.get_contact_list(list_id, vendor_id)
    if not row:
        await query.edit_message_text("Lista nao encontrada.")
        return
    total = await db.contact_list_count(list_id)
    await query.edit_message_text(
        f"Lista: {row['name']}\nContatos: {total}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Usar esta lista", callback_data=f"src_use:{list_id}")],
            [InlineKeyboardButton("Adicionar contatos nela", callback_data=f"src_add:{list_id}")],
            [InlineKeyboardButton("Voltar", callback_data="src_saved")],
        ]),
    )


async def show_contact_list_detail(query, db: Database, vendor_id: int, list_id: int):
    row = await db.get_contact_list(list_id, vendor_id)
    if not row:
        await query.edit_message_text("Lista nao encontrada.")
        return
    total = await db.contact_list_count(list_id)
    await query.edit_message_text(
        f"{row['name']}\nContatos: {total}\nAtualizada: {row['updated_at']}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Usar em campanha", callback_data=f"list_campaign:{list_id}")],
            [InlineKeyboardButton("Adicionar contatos", callback_data=f"list_add:{list_id}")],
            [InlineKeyboardButton("Exportar CSV", callback_data=f"list_export:{list_id}")],
            [InlineKeyboardButton("Reduzir lista", callback_data=f"list_reduce:{list_id}")],
            [InlineKeyboardButton("Renomear", callback_data=f"list_rename:{list_id}")],
            [InlineKeyboardButton("Backups", callback_data=f"list_backups:{list_id}")],
            [InlineKeyboardButton("Apagar", callback_data=f"list_delete_ask:{list_id}")],
            [InlineKeyboardButton("Voltar", callback_data="list_menu")],
            [InlineKeyboardButton("Sair", callback_data="list_exit")],
        ]),
    )


async def handle_contact_source_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await require_user(update, context):
        return ConversationHandler.END

    settings, db, _, _ = services(context)
    vendor_id = query.from_user.id
    data = query.data or ""

    if data == "list_exit":
        context.user_data.pop("active_list_id", None)
        context.user_data.pop("list_flow", None)
        context.user_data.pop("remove_phones", None)
        await query.edit_message_text("Menu fechado.")
        return ConversationHandler.END

    if data == "src_back":
        profile_id = context.user_data.get("pending_campaign_profile_id", settings.default_profile)
        rows = await db.list_contact_lists(vendor_id)
        keyboard = []
        if rows:
            keyboard.append([InlineKeyboardButton("Usar lista salva", callback_data="src_saved")])
        keyboard.append([InlineKeyboardButton("Criar nova lista", callback_data="src_new_list")])
        keyboard.append([InlineKeyboardButton("Carregar so para esta campanha", callback_data="src_temp")])
        await query.edit_message_text(
            f"Campanha: {get_profile(profile_id).label}.\nComo deseja escolher os contatos?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return WAITING_CONTACT_SOURCE

    if data == "src_saved":
        await show_saved_lists_for_campaign(query, db, vendor_id)
        return WAITING_CONTACT_SOURCE

    if data.startswith("src_pick:"):
        await show_selected_list_for_campaign(query, db, vendor_id, int(data.split(":", 1)[1]))
        return WAITING_CONTACT_SOURCE

    if data.startswith("src_use:"):
        await create_campaign_from_list(query, context, int(data.split(":", 1)[1]))
        return WAITING_MEDIA

    if data.startswith("src_add:"):
        list_id = int(data.split(":", 1)[1])
        context.user_data["active_list_id"] = list_id
        context.user_data["list_flow"] = "campaign_add"
        await query.edit_message_text(
            "Envie CSV, VCF bruto, ZIP ou contatos pelo clipe. Use /pronto quando terminar."
        )
        return WAITING_LIST_CONTACTS

    if data == "src_new_list" or data == "list_new":
        context.user_data["list_flow"] = "campaign_new" if context.user_data.get("pending_campaign_profile_id") else "menu_new"
        await query.edit_message_text("Nome da nova lista:")
        return WAITING_LIST_NAME

    if data == "src_temp":
        await create_temp_campaign(query, context)
        return WAITING_CSV

    if data == "list_menu":
        await show_lists_menu(query, db, vendor_id, edit=True)
        return WAITING_CONTACT_SOURCE

    if data.startswith("list_open:"):
        await show_contact_list_detail(query, db, vendor_id, int(data.split(":", 1)[1]))
        return WAITING_CONTACT_SOURCE

    if data.startswith("list_campaign:"):
        context.user_data["pending_campaign_profile_id"] = settings.default_profile
        await create_campaign_from_list(query, context, int(data.split(":", 1)[1]))
        return WAITING_MEDIA

    if data.startswith("list_add:"):
        context.user_data["active_list_id"] = int(data.split(":", 1)[1])
        context.user_data["list_flow"] = "menu_add"
        await query.edit_message_text(
            "Envie CSV, VCF bruto, ZIP ou contatos pelo clipe. Use /pronto quando terminar."
        )
        return WAITING_LIST_CONTACTS

    if data.startswith("list_export:"):
        await export_contact_list(query, context, int(data.split(":", 1)[1]))
        return WAITING_CONTACT_SOURCE

    if data.startswith("list_reduce:"):
        context.user_data["active_list_id"] = int(data.split(":", 1)[1])
        context.user_data["remove_phones"] = []
        await query.edit_message_text(
            "Envie CSV, VCF bruto, ZIP ou contatos a remover. Um backup sera criado antes da remocao. Use /pronto quando terminar."
        )
        return WAITING_REMOVE_CONTACTS

    if data.startswith("list_rename:"):
        context.user_data["active_list_id"] = int(data.split(":", 1)[1])
        await query.edit_message_text("Novo nome da lista:")
        return WAITING_RENAME_LIST

    if data.startswith("list_backups:"):
        list_id = int(data.split(":", 1)[1])
        context.user_data["active_list_id"] = list_id
        await show_list_backups(query, db, vendor_id, list_id)
        return WAITING_CONTACT_SOURCE

    if data.startswith("list_restore:"):
        snapshot_id = int(data.split(":", 1)[1])
        try:
            await db.restore_contact_list_snapshot(snapshot_id, vendor_id)
        except Exception as exc:
            await query.edit_message_text(f"Erro ao restaurar backup: {exc}")
            return ConversationHandler.END
        await query.edit_message_text("Backup restaurado.")
        return ConversationHandler.END

    if data.startswith("list_delete_ask:"):
        list_id = int(data.split(":", 1)[1])
        row = await db.get_contact_list(list_id, vendor_id)
        if not row:
            await query.edit_message_text("Lista nao encontrada.")
            return ConversationHandler.END
        await query.edit_message_text(
            f"Apagar lista '{row['name']}'? Essa acao e permanente.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Confirmar apagar", callback_data=f"list_delete_yes:{list_id}")],
                [InlineKeyboardButton("Voltar", callback_data=f"list_open:{list_id}")],
                [InlineKeyboardButton("Sair", callback_data="list_exit")],
            ]),
        )
        return WAITING_CONTACT_SOURCE

    if data.startswith("list_delete_yes:"):
        ok = await db.delete_contact_list(int(data.split(":", 1)[1]), vendor_id)
        await query.edit_message_text("Lista apagada." if ok else "Lista nao encontrada.")
        return ConversationHandler.END

    return WAITING_CONTACT_SOURCE


async def create_temp_campaign(query, context: ContextTypes.DEFAULT_TYPE):
    settings, db, _, _ = services(context)
    profile_id = context.user_data.get("pending_campaign_profile_id", settings.default_profile)
    profile = get_profile(profile_id)
    contact_limit = contact_limit_for_profile(settings, profile.id)
    campaign_id = await db.create_campaign(query.from_user.id, profile.id)
    context.user_data["campaign_id"] = campaign_id
    campaign_dir(settings, campaign_id).mkdir(parents=True, exist_ok=True)
    await query.edit_message_text(
        f"Campanha #{campaign_id} criada.\n"
        f"Perfil: {profile.label}.\n"
        f"Envie CSV, VCF bruto, ou ZIP com CSV/VCF dentro.\n"
        f"Limite: {contact_limit_label(contact_limit)}.\n"
        "Se o Telegram transformar em cartoes de contato, use /pronto quando terminar."
    )


async def create_campaign_from_list(query, context: ContextTypes.DEFAULT_TYPE, list_id: int):
    settings, db, _, _ = services(context)
    profile_id = context.user_data.get("pending_campaign_profile_id", settings.default_profile)
    profile = get_profile(profile_id)
    limit = contact_limit_for_profile(settings, profile.id)
    if await db.contact_list_count(list_id) <= 0:
        await query.edit_message_text("A lista nao tem contatos validos.")
        return
    campaign_id = await db.create_campaign(query.from_user.id, profile.id)
    total = await db.copy_contact_list_to_campaign(list_id, query.from_user.id, campaign_id, limit)
    context.user_data["campaign_id"] = campaign_id
    campaign_dir(settings, campaign_id).mkdir(parents=True, exist_ok=True)
    await query.edit_message_text(
        f"Campanha #{campaign_id} criada com {total} contatos.\n"
        "Agora envie imagens, uma por vez, ou use /sem_midia. Quando terminar, use /pronto."
    )


async def receive_list_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, _, _ = services(context)
    user = update.effective_user
    await db.ensure_vendor(user.id, user.username or f"user_{user.id}", f"vendor_{user.id}")
    try:
        list_id = await db.create_contact_list(user.id, update.message.text)
    except Exception as exc:
        await update.message.reply_text(str(exc))
        return WAITING_LIST_NAME
    context.user_data["active_list_id"] = list_id
    await update.message.reply_text(
        "Lista criada. Envie CSV, VCF bruto, ZIP ou contatos pelo clipe. Use /pronto quando terminar."
    )
    return WAITING_LIST_CONTACTS


async def receive_list_contacts_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, _, _ = services(context)
    list_id = context.user_data.get("active_list_id")
    if not list_id:
        await update.message.reply_text("Lista nao encontrada. Use /listas.")
        return ConversationHandler.END

    try:
        contacts = await contacts_from_document(update, context, settings, 0)
        result = await db.import_contacts_to_list(list_id, update.effective_user.id, contacts)
    except Exception as exc:
        await update.message.reply_text(str(exc))
        return WAITING_LIST_CONTACTS

    await update.message.reply_text(import_summary(result) + "\nEnvie mais contatos ou use /pronto.")
    return WAITING_LIST_CONTACTS


async def receive_list_contact_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    list_id = context.user_data.get("active_list_id")
    contacts = contacts_from_telegram_contact(update)
    if not list_id or not contacts:
        await update.message.reply_text("Contato sem numero completo. Envie arquivo bruto se o Telegram cortar o DDD.")
        return WAITING_LIST_CONTACTS
    result = await db.import_contacts_to_list(list_id, update.effective_user.id, contacts)
    await update.message.reply_text(import_summary(result) + "\nEnvie mais contatos ou use /pronto.")
    return WAITING_LIST_CONTACTS


async def list_contacts_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    list_id = context.user_data.get("active_list_id")
    flow = context.user_data.get("list_flow", "")
    if not list_id:
        return ConversationHandler.END

    if flow.startswith("campaign"):
        await update.message.reply_text(
            "Deseja usar essa lista na campanha?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Usar nesta campanha", callback_data=f"src_use:{list_id}")],
                [InlineKeyboardButton("Adicionar mais contatos", callback_data=f"src_add:{list_id}")],
            ]),
        )
        return WAITING_CONTACT_SOURCE

    row = await db.get_contact_list(list_id, update.effective_user.id)
    total = await db.contact_list_count(list_id)
    await update.message.reply_text(
        f"Lista '{row['name']}' atualizada.\nContatos: {total}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Abrir lista", callback_data=f"list_open:{list_id}")]]),
    )
    return WAITING_CONTACT_SOURCE


async def receive_remove_contacts_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, _, _, _ = services(context)
    try:
        contacts = await contacts_from_document(update, context, settings, 0)
    except Exception as exc:
        await update.message.reply_text(str(exc))
        return WAITING_REMOVE_CONTACTS
    phones = context.user_data.setdefault("remove_phones", [])
    phones.extend(item["phone"] for item in contacts)
    await update.message.reply_text(f"Marcados para remover: {len(set(phones))}. Envie mais ou use /pronto.")
    return WAITING_REMOVE_CONTACTS


async def receive_remove_contact_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contacts = contacts_from_telegram_contact(update)
    if not contacts:
        await update.message.reply_text("Contato sem numero completo. Envie arquivo bruto se o Telegram cortar o DDD.")
        return WAITING_REMOVE_CONTACTS
    phones = context.user_data.setdefault("remove_phones", [])
    phones.extend(item["phone"] for item in contacts)
    await update.message.reply_text(f"Marcados para remover: {len(set(phones))}. Envie mais ou use /pronto.")
    return WAITING_REMOVE_CONTACTS


async def list_remove_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    list_id = context.user_data.get("active_list_id")
    phones = set(context.user_data.get("remove_phones", []))
    if not list_id or not phones:
        await update.message.reply_text("Nenhum contato marcado para remover.")
        return WAITING_REMOVE_CONTACTS

    try:
        await db.create_contact_list_snapshot(list_id, update.effective_user.id, "antes de reduzir lista")
        removed = await db.remove_contacts_from_list(list_id, update.effective_user.id, phones)
    except Exception as exc:
        await update.message.reply_text(f"Erro ao reduzir lista: {exc}")
        return ConversationHandler.END

    context.user_data.pop("remove_phones", None)
    await update.message.reply_text(
        f"Removidos: {removed}. Backup criado.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Abrir lista", callback_data=f"list_open:{list_id}")]]),
    )
    return WAITING_CONTACT_SOURCE


async def receive_rename_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = context.application.bot_data["db"]
    list_id = context.user_data.get("active_list_id")
    if not list_id:
        return ConversationHandler.END
    try:
        ok = await db.rename_contact_list(list_id, update.effective_user.id, update.message.text)
    except Exception as exc:
        await update.message.reply_text(str(exc))
        return WAITING_RENAME_LIST
    await update.message.reply_text(
        "Lista renomeada." if ok else "Lista nao encontrada.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Abrir lista", callback_data=f"list_open:{list_id}")]]),
    )
    return WAITING_CONTACT_SOURCE


async def export_contact_list(query, context: ContextTypes.DEFAULT_TYPE, list_id: int):
    db = context.application.bot_data["db"]
    row = await db.get_contact_list(list_id, query.from_user.id)
    if not row:
        await query.edit_message_text("Lista nao encontrada.")
        return
    contacts = await db.contact_list_contacts(list_id, query.from_user.id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["nome", "telefone"])
    for contact in contacts:
        writer.writerow([contact["name"] or "Cliente", contact["phone"]])
    data = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    data.name = f"lista_{list_id}.csv"
    await context.bot.send_document(
        chat_id=query.message.chat_id,
        document=data,
        filename=data.name,
        caption=f"{row['name']} - {len(contacts)} contatos",
    )
    await show_contact_list_detail(query, db, query.from_user.id, list_id)


async def show_list_backups(query, db: Database, vendor_id: int, list_id: int):
    context_list = await db.get_contact_list(list_id, vendor_id)
    if not context_list:
        await query.edit_message_text("Lista nao encontrada.")
        return
    rows = await db.list_contact_list_snapshots(list_id, vendor_id)
    if not rows:
        await query.edit_message_text(
            "Nenhum backup disponivel.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Voltar", callback_data=f"list_open:{list_id}")],
                [InlineKeyboardButton("Sair", callback_data="list_exit")],
            ]),
        )
        return
    keyboard = [
        [
            InlineKeyboardButton(
                f"{row['created_at']} - {row['total_contacts']}",
                callback_data=f"list_restore:{row['id']}",
            )
        ]
        for row in rows[:5]
    ]
    keyboard.append([InlineKeyboardButton("Voltar", callback_data=f"list_open:{list_id}")])
    keyboard.append([InlineKeyboardButton("Sair", callback_data="list_exit")])
    await query.edit_message_text("Backups disponiveis:", reply_markup=InlineKeyboardMarkup(keyboard))


async def contacts_from_document(update: Update, context: ContextTypes.DEFAULT_TYPE, settings: Settings, limit: int) -> list[dict]:
    document = update.message.document
    file_name = document.file_name or ""
    suffix = Path(file_name).suffix.lower()
    if not document or suffix not in (".csv", ".vcf", ".zip"):
        raise ValueError("Envie um arquivo .csv, .vcf ou .zip.")

    tmp_dir = settings.campaigns_dir / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / f"{update.effective_user.id}_{document.file_unique_id}{suffix}"
    file = await context.bot.get_file(document.file_id)
    await file.download_to_drive(path)
    try:
        return parse_contacts_file(path, limit)
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            logger.debug("Nao foi possivel apagar arquivo temporario %s", path, exc_info=True)


def contacts_from_telegram_contact(update: Update) -> list[dict]:
    contact = update.message.contact
    full_name = " ".join(item for item in [contact.first_name, contact.last_name] if item).strip() or "Cliente"
    contacts = parse_contacts_vcf_text(contact.vcard) if contact.vcard else []
    if contacts:
        return contacts
    phone = usable_phone(contact.phone_number)
    if not phone:
        return []
    return [{"row_index": 0, "name": full_name, "phone": phone}]


def import_summary(result: dict) -> str:
    return (
        "Lista atualizada.\n"
        f"Novos: {result['added']}\n"
        f"Duplicados ignorados: {result['duplicates']}\n"
        f"Nomes atualizados: {result['updated']}\n"
        f"Total da lista: {result['total']}"
    )


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
    if contact_limit > 0 and campaign and campaign["total_contacts"] >= contact_limit:
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
    selected = contacts if contact_limit <= 0 else contacts[:remaining]
    total = await db.add_contacts(campaign_id, selected)
    extra = "" if len(selected) == len(contacts) else f"\n{len(contacts) - len(selected)} contato(s) excederam o limite e foram ignorados."
    await update.message.reply_text(f"Contato recebido. Total da campanha: {total}.{extra}\nUse /pronto quando terminar.")
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

    _, db, evolution, scheduler = services(context)
    power = power_service(context)
    campaign = await db.get_active_campaign_for_vendor(update.effective_user.id)
    if not campaign:
        await update.message.reply_text("Nenhuma campanha ativa.")
        return

    if campaign["status"] != "ready":
        await update.message.reply_text(f"Campanha #{campaign['id']} esta em status {campaign['status']}.")
        return

    campaign_with_vendor = await db.get_campaign_with_vendor(campaign["id"])
    try:
        await power.ensure_running()
    except Exception as exc:
        logger.exception("Erro ao ligar Evolution")
        await update.message.reply_text(f"Erro ao ligar Evolution: {exc}")
        return
    state = await evolution.connection_state(campaign_with_vendor["instance_name"])
    if state != "open":
        status_message = await update.message.reply_text("Aguardando conexao do WhatsApp abrir...")
        if not await evolution.wait_until_open(campaign_with_vendor["instance_name"]):
            await status_message.edit_text("WhatsApp nao esta conectado. Use /login antes de disparar.")
            await power.stop_if_idle(db)
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

    if data.startswith(("src_", "list_", "menu_")):
        await query.answer("Esse menu expirou. Use /nova ou /listas novamente.", show_alert=True)
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
            await safe_edit_query_text(
                query,
                f"Campanha #{campaign_id} pausada.\nUse Retomar para continuar.",
                reply_markup=campaign_controls(campaign_id, paused=True),
            )
        return

    if action == "campaign_resume":
        ok = await scheduler.resume_campaign(campaign_id)
        await query.answer("Campanha retomada." if ok else "Nao foi possivel retomar.")
        if ok:
            await safe_edit_query_text(
                query,
                f"Campanha #{campaign_id} retomada.\nAguardando proximo envio...",
                reply_markup=campaign_controls(campaign_id),
            )
        return

    if action == "campaign_cancel_ask":
        paused = campaign["status"] == "paused"
        await query.answer()
        await safe_edit_query_text(
            query,
            f"Cancelar campanha #{campaign_id}?\nEssa acao nao envia mais contatos.",
            reply_markup=cancel_confirmation_controls(campaign_id, paused=paused),
        )
        return

    if action == "campaign_cancel_no":
        await query.answer("Cancelamento descartado.")
        await safe_edit_query_text(
            query,
            f"Campanha #{campaign_id} continua em andamento.",
            reply_markup=campaign_controls(campaign_id),
        )
        return

    if action == "campaign_cancel_no_paused":
        await query.answer("Cancelamento descartado.")
        await safe_edit_query_text(
            query,
            f"Campanha #{campaign_id} continua pausada.",
            reply_markup=campaign_controls(campaign_id, paused=True),
        )
        return

    if action == "campaign_cancel_yes":
        ok = await scheduler.cancel_campaign(campaign_id)
        await query.answer("Campanha cancelada." if ok else "Nao foi possivel cancelar.")
        await safe_edit_query_text(
            query,
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


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    exc_info = None
    if context.error:
        exc_info = (type(context.error), context.error, context.error.__traceback__)
    logger.error("Erro inesperado no handler do Telegram", exc_info=exc_info)
    message = getattr(update, "effective_message", None)
    if message:
        try:
            await message.reply_text("Erro interno no bot. Tente o comando novamente.")
        except Exception:
            pass


async def safe_edit_query_text(query, text: str, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as exc:
        if "Message is not modified" in str(exc):
            return
        raise


def campaign_dir(settings: Settings, campaign_id: int) -> Path:
    return settings.campaigns_dir / str(campaign_id)


def qr_photo_bytes(qr_base64: str) -> bytes | None:
    if not qr_base64:
        return None
    try:
        image = base64.b64decode(qr_base64, validate=True)
    except Exception:
        return None
    return image or None


def schedule_idle_stop(context: ContextTypes.DEFAULT_TYPE):
    settings, db, _, _ = services(context)
    power = power_service(context)
    if not power.enabled or settings.evolution_idle_stop_seconds <= 0:
        return

    previous = context.application.bot_data.get("idle_stop_task")
    if previous and not previous.done():
        previous.cancel()

    async def delayed_stop():
        try:
            await asyncio.sleep(settings.evolution_idle_stop_seconds)
            await power.stop_if_idle(db)
        except asyncio.CancelledError:
            raise

    task = context.application.create_task(delayed_stop())
    context.application.bot_data["idle_stop_task"] = task

    def clear(done_task):
        if context.application.bot_data.get("idle_stop_task") is done_task:
            context.application.bot_data.pop("idle_stop_task", None)

    task.add_done_callback(clear)
