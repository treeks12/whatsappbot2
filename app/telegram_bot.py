import base64
import io
import logging
import asyncio
import csv
import re
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
from .evolution import EvolutionClient, EvolutionError, normalize_phone
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
    WAITING_BLACKLIST_FILE,
    WAITING_PROFILE_CHOICE,
    WAITING_CONNECT_PHONE,
) = range(11)
PERM_STATE_KEY = "perm_state"
BLACKLIST_PAGE_SIZE = 15
FRIENDLY_ERROR = (
    "😞 Algo não funcionou agora. O suporte já foi avisado — "
    "tente de novo em alguns minutos."
)


def register_handlers(application):
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("atalhos", atalhos))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("conexao", conexao))
    application.add_handler(CommandHandler("desconectar", desconectar))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("disparar", disparar))
    application.add_handler(CommandHandler("blacklist", blacklist_add))
    application.add_handler(CommandHandler("blacklist_remover", blacklist_remove))
    application.add_handler(CommandHandler("blacklist_listar", blacklist_list))

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="If 'per_message=False'.*", category=PTBUserWarning)
        application.add_handler(
            ConversationHandler(
                entry_points=[
                    CommandHandler(["nova", "nova_confianca", "nova_precaucao"], nova),
                    CommandHandler("listas", listas),
                    CallbackQueryHandler(menu_new_entry, pattern="^menu_new$"),
                    CallbackQueryHandler(menu_lists_entry, pattern="^menu_lists$"),
                    CallbackQueryHandler(menu_connect_entry, pattern="^menu_connect$"),
                    CallbackQueryHandler(changephone_entry, pattern="^cconnect_changephone$"),
                    CallbackQueryHandler(campaign_profile_callback, pattern="^cprof_"),
                    CallbackQueryHandler(draft_continue_entry, pattern="^cdraft_continue_"),
                ],
                states={
                    WAITING_PROFILE_CHOICE: [
                        CallbackQueryHandler(campaign_profile_callback, pattern="^cprof_"),
                    ],
                    WAITING_CONNECT_PHONE: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_connect_phone),
                        CallbackQueryHandler(menu_connect_entry, pattern="^menu_connect$"),
                    ],
                    WAITING_CONTACT_SOURCE: [
                        CallbackQueryHandler(handle_contact_source_callback, pattern="^(src_|list_)"),
                    ],
                    WAITING_CSV: [
                        MessageHandler(filters.Document.ALL, receive_contacts_file),
                        MessageHandler(filters.CONTACT, receive_contact_card),
                        CommandHandler("pronto", contacts_done),
                        CallbackQueryHandler(contacts_done_button, pattern="^ccontacts_done$"),
                    ],
                    WAITING_MEDIA: [
                        MessageHandler(filters.PHOTO, receive_photo),
                        MessageHandler(filters.Document.IMAGE, receive_image_document),
                        CommandHandler("pronto", media_done),
                        CommandHandler("sem_midia", media_done),
                        CallbackQueryHandler(media_done_button, pattern="^cmedia_done$"),
                    ],
                    WAITING_CAPTION: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_caption),
                        CommandHandler("sem_texto", receive_no_caption),
                        CallbackQueryHandler(no_caption_button, pattern="^ccap_none$"),
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
        application.add_handler(
            ConversationHandler(
                entry_points=[CommandHandler("blacklist_arquivo", blacklist_file_start)],
                states={
                    WAITING_BLACKLIST_FILE: [
                        MessageHandler(filters.Document.ALL, blacklist_file_receive_document),
                        MessageHandler(filters.CONTACT, blacklist_file_receive_contact),
                        CommandHandler("pronto", blacklist_file_done),
                    ],
                },
                fallbacks=[CommandHandler("cancelar", blacklist_file_cancel)],
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


# ----------------------------------------------------------------------------
# Menu principal e navegacao por botoes
# ----------------------------------------------------------------------------

SHORTCUTS_TEXT = (
    "Atalhos de teclado para quem prefere digitar:\n\n"
    "/menu - menu principal\n"
    "/login - conectar WhatsApp\n"
    "/conexao - verificar conexao do WhatsApp\n"
    "/desconectar - fechar conexao sem apagar sessao\n"
    "/nova - criar campanha cautelosa\n"
    "/nova_confianca - campanha para contatos de confianca\n"
    "/nova_precaucao - campanha mais cuidadosa\n"
    "/listas - gerenciar listas de contatos\n"
    "/disparar - iniciar campanha pronta\n"
    "/status - ver ultimas campanhas\n"
    "/cancelar - cancelar campanha ativa\n"
    "/blacklist - bloquear telefone\n"
    "/blacklist_remover - remover telefone da blacklist\n"
    "/blacklist_listar - ver telefones bloqueados\n"
    "/blacklist_arquivo - importar varios telefones para a blacklist"
)

CONNECT_POLL_SECONDS = 5
CONNECT_POLL_TIMEOUT = 180
QR_REFRESH_ATTEMPTS = 3
QR_REFRESH_SECONDS = 50


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📣 Nova campanha", callback_data="menu_new")],
        [InlineKeyboardButton("👥 Minhas listas", callback_data="menu_lists")],
        [InlineKeyboardButton("📊 Status", callback_data="menu_status")],
        [InlineKeyboardButton("📱 Conectar WhatsApp", callback_data="menu_connect")],
        [InlineKeyboardButton("⛔ Bloquear número", callback_data="menu_blacklist")],
    ])


def back_to_menu_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton("◀️ Voltar ao menu", callback_data="menu_main")]


async def send_main_menu(message, hello: str = "Escolha o que fazer 👇"):
    await message.reply_text(hello, reply_markup=main_menu_keyboard())


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str):
    settings = context.application.bot_data["settings"]
    for admin_id in settings.telegram_admin_ids:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except Exception:
            logger.debug("Falha ao avisar admin %s", admin_id, exc_info=True)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return
    await send_main_menu(update.effective_message)


async def atalhos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return
    await update.effective_message.reply_text(SHORTCUTS_TEXT)


async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Acoes do menu principal que nao precisam de estado de conversa."""
    query = update.callback_query
    data = query.data or ""

    if data == "menu_main":
        await query.answer()
        await send_main_menu(query.message)
        return

    if data == "menu_status":
        await query.answer()
        _, db, _, _ = services(context)
        rows = await db.campaign_summary_for_vendor(query.from_user.id)
        if not rows:
            text = "Você ainda não tem campanhas. 🎉"
        else:
            lines = ["Suas últimas campanhas:"]
            for row in rows:
                processed = row["processed_count"] or 0
                failed = row["failed_count"] or 0
                lines.append(
                    f"#{row['id']} {row['status']} - enviados {row['sent_count']} | "
                    f"parados em {processed}/{row['total_contacts']} | falhas {failed}"
                )
            text = "\n".join(lines)
        await query.message.reply_text(text, reply_markup=main_menu_keyboard())
        return

    if data == "menu_blacklist":
        await query.answer()
        await query.message.reply_text(
            "Para um telefone nunca mais receber suas mensagens:\n\n"
            "/blacklist 5511999998888 motivo opcional\n\n"
            "Quer ver quem está bloqueado? Toque abaixo.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Ver bloqueados", callback_data="bl_menu_list")],
                back_to_menu_row(),
            ]),
        )
        return

    await query.answer()


# ----------------------------------------------------------------------------
# Conexao do WhatsApp: pairing code (codigo) ou QR com auto-refresh
# ----------------------------------------------------------------------------


def format_pairing_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}" if len(code) > 4 else code


def format_phone_br(phone_e164: str) -> str:
    """5511940069474 -> +55 11 94006-9474 (para a vendedora reconhecer o numero)."""
    digits = re.sub(r"\D", "", phone_e164 or "")
    if len(digits) >= 12:
        ddi, ddd, rest = digits[:2], digits[2:4], digits[4:]
        tail = f"{rest[-4:]}" if len(rest) > 4 else rest
        head = rest[:-4] if len(rest) > 4 else ""
        return f"+{ddi} {ddd} {head}-{tail}"
    return phone_e164


def connect_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📷 Conectar com QR Code (recomendado)", callback_data="cconnect_qr")],
        [InlineKeyboardButton("🔢 Conectar com código (tem que colar rápido)", callback_data="cconnect_pair")],
        [InlineKeyboardButton("✏️ Trocar número", callback_data="cconnect_changephone")],
        back_to_menu_row(),
    ])


async def menu_connect_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await require_user(update, context):
        return ConversationHandler.END

    settings, db, evolution, _ = services(context)
    power = power_service(context)
    user = query.from_user
    instance_name = f"vendor_{user.id}"
    await db.ensure_vendor(user.id, user.username or f"user_{user.id}", instance_name)

    try:
        await power.ensure_running()
    except Exception as exc:
        logger.exception("Erro ao ligar Evolution ao conectar")
        await notify_admins(
            context,
            f"⚠️ Falha ao ligar Evolution (conectar WhatsApp)\nUsuário: {user.full_name} ({user.id})\n{exc}",
        )
        await safe_edit_query_text(query, FRIENDLY_ERROR, reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    state = await evolution.connection_state(instance_name)
    if state == "open":
        await ensure_instance_webhook(settings, evolution, instance_name)
        await safe_edit_query_text(
            query,
            "✅ Seu WhatsApp já está conectado!\nSe precisar trocar, use /desconectar antes.",
            reply_markup=main_menu_keyboard(),
        )
        schedule_idle_stop(context)
        return ConversationHandler.END

    phone = await db.get_vendor_phone(user.id)
    if phone:
        await safe_edit_query_text(
            query,
            f"Vamos conectar o WhatsApp do número {phone}.\n\n"
            "🔢 Com código — você digita um número no WhatsApp. O mais fácil!\n"
            "📷 Com QR Code — aponta a câmera.",
            reply_markup=connect_choice_keyboard(),
        )
        schedule_idle_stop(context)
        return ConversationHandler.END

    await safe_edit_query_text(
        query,
        "Primeiro me diga: qual o número de WhatsApp que você usa para atender clientes?\n\n"
        "Envie só o número com DDD. Exemplo: 11999998888",
    )
    return WAITING_CONNECT_PHONE


async def changephone_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await require_user(update, context):
        return ConversationHandler.END
    await safe_edit_query_text(
        query,
        "Qual o número de WhatsApp que você usa para atender clientes?\n\n"
        "Envie só o número com DDD. Exemplo: 11999998888",
    )
    return WAITING_CONNECT_PHONE


async def receive_connect_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, db, _, _ = services(context)
    digits = re.sub(r"\D", "", update.message.text or "")
    phone = usable_phone(digits)
    if not phone:
        await update.message.reply_text(
            "Não entendi o número. 🙈 Envie só números com DDD, por exemplo: 11999998888"
        )
        return WAITING_CONNECT_PHONE

    user = update.effective_user
    await db.set_vendor_phone(user.id, phone)
    await update.message.reply_text(
        f"Número {phone} salvo! ✅\nComo prefere conectar?",
        reply_markup=connect_choice_keyboard(),
    )
    return ConversationHandler.END


async def _send_pairing_code_message(query, context: ContextTypes.DEFAULT_TYPE, code: str, phone: str = "") -> int | None:
    """Passos no painel (editado) + codigo em mensagem propria, copiavel com um toque.

    Retorna o id da mensagem do codigo para o watcher mante-la sempre atual
    (a Evolution renova o codigo a cada ~20s enquanto espera o pareamento).
    """
    where = f" do número {format_phone_br(phone)}" if phone else ""
    await safe_edit_query_text(
        query,
        f"No celular, abra o WhatsApp{where} e toque:\n"
        "1. Configurações (a engrenagem ⚙️)\n"
        "2. Aparelhos conectados\n"
        "3. Conectar um aparelho\n"
        "4. Conectar com número de telefone\n\n"
        "👉 Copie o código da próxima mensagem e cole LÁ RÁPIDO.\n"
        "⚠️ O código muda sozinho a cada ~20s — mas eu atualizo a mensagem com o "
        "código atual. Se der incorreto, copie o que está na tela de novo.",
    )
    message = await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="🔐 Código de conexão (toque e segure para copiar):\n\n"
        f"`{format_pairing_code(code)}`",
        parse_mode="Markdown",
    )
    return message.message_id


def _reset_offer_keyboard(mode: str) -> InlineKeyboardMarkup:
    label = "🔄 Resetar e gerar código" if mode == "pair" else "🔄 Resetar e gerar QR"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"cconnect_reset_{mode}")],
        back_to_menu_row(),
    ])


async def _maybe_offer_reset(query, context, exc: Exception, mode: str) -> bool:
    """Quando a sessao antiga morreu e travou a instancia na Evolution,
    oferece o reset (apagar sessao morta e comecar de novo)."""
    text = str(exc)
    if "Pairing code nao retornado" not in text and "nao retornou QR" not in text:
        return False
    await safe_edit_query_text(
        query,
        "A conexão antiga desse número ficou presa no sistema. 😕\n"
        "Um reset resolve: apago a sessão morta e começo de novo.\n"
        "Seus contatos e campanhas ficam salvos — é só conectar de novo.",
        reply_markup=_reset_offer_keyboard(mode),
    )
    return True


async def reset_and_connect(query, context: ContextTypes.DEFAULT_TYPE, mode: str):
    """Apaga a instancia com sessao morta e refaz o fluxo de conexao."""
    await query.answer("Resetando a conexão...")
    settings, db, evolution, _ = services(context)
    power = power_service(context)
    user = query.from_user
    instance_name = f"vendor_{user.id}"

    if await evolution.connection_state(instance_name) == "open":
        await safe_edit_query_text(query, "✅ Seu WhatsApp já está conectado!")
        return

    try:
        await power.ensure_running()
        try:
            await evolution.delete_instance(instance_name)
        except Exception:
            logger.warning("Falha ao apagar instancia %s no reset", instance_name, exc_info=True)
        # Limpeza interna da Evolution e assincrona; dar respiro evita colisao
        # entre o delete e o create que vem a seguir.
        await asyncio.sleep(3)
        if mode == "pair":
            phone = await db.get_vendor_phone(user.id)
            if not phone:
                await safe_edit_query_text(
                    query,
                    "Não encontrei seu número salvo. Use 📱 Conectar WhatsApp no menu.",
                    reply_markup=main_menu_keyboard(),
                )
                return
            code = await evolution.request_pairing_code(instance_name, phone)
            code_message_id = await _send_pairing_code_message(query, context, code, phone)
            schedule_idle_stop(context)
            _spawn_connect_watch(context, user.id, instance_name, query.message.chat_id, code_message_id)
            return
        else:
            qr_base64 = await evolution.ensure_fresh_qr(instance_name)
            image = qr_photo_bytes(qr_base64)
            if not image:
                raise EvolutionError("QR nao retornado apos reset")
            await query.message.reply_photo(
                photo=io.BytesIO(image),
                caption=(
                    "Escaneie no WhatsApp: Configurações > Aparelhos conectados > Conectar aparelho.\n\n"
                    "Não deu tempo? Eu mando um QR novo automaticamente. 😉"
                ),
            )
            schedule_idle_stop(context)
            _spawn_qr_watch(context, user.id, instance_name, query.message.chat_id)
            return
    except Exception as exc:
        logger.exception("Erro no reset de conexao")
        await notify_admins(
            context,
            f"⚠️ Falha no reset de conexao\nUsuário: {user.full_name} ({user.id})\n{exc}",
        )
        await safe_edit_query_text(query, FRIENDLY_ERROR, reply_markup=main_menu_keyboard())


async def start_pairing_connect(query, context: ContextTypes.DEFAULT_TYPE):
    await query.answer("Gerando seu código...")
    settings, db, evolution, _ = services(context)
    power = power_service(context)
    user = query.from_user
    instance_name = f"vendor_{user.id}"

    phone = await db.get_vendor_phone(user.id)
    if not phone:
        await safe_edit_query_text(
            query,
            "Não encontrei seu número salvo. Toque em 📱 Conectar WhatsApp no menu para começar de novo.",
            reply_markup=main_menu_keyboard(),
        )
        return

    try:
        await power.ensure_running()
        code = await evolution.request_pairing_code(instance_name, phone)
    except Exception as exc:
        logger.exception("Erro ao gerar pairing code")
        if await _maybe_offer_reset(query, context, exc, "pair"):
            return
        await notify_admins(
            context,
            f"⚠️ Falha ao gerar pairing code\nUsuário: {user.full_name} ({user.id})\n{exc}",
        )
        await safe_edit_query_text(query, FRIENDLY_ERROR, reply_markup=main_menu_keyboard())
        return

    if not code:
        await safe_edit_query_text(query, "✅ Seu WhatsApp já está conectado!")
        return

    code_message_id = await _send_pairing_code_message(query, context, code, phone)
    schedule_idle_stop(context)
    _spawn_connect_watch(context, user.id, instance_name, query.message.chat_id, code_message_id)


async def start_qr_connect(query, context: ContextTypes.DEFAULT_TYPE):
    await query.answer()
    settings, db, evolution, _ = services(context)
    power = power_service(context)
    user = query.from_user
    instance_name = f"vendor_{user.id}"

    try:
        await power.ensure_running()
        qr_base64 = await evolution.ensure_fresh_qr(instance_name)
    except Exception as exc:
        logger.exception("Erro ao gerar QR")
        if await _maybe_offer_reset(query, context, exc, "qr"):
            return
        await notify_admins(
            context,
            f"⚠️ Falha ao gerar QR\nUsuário: {user.full_name} ({user.id})\n{exc}",
        )
        await safe_edit_query_text(query, FRIENDLY_ERROR, reply_markup=main_menu_keyboard())
        return

    if not qr_base64:
        await safe_edit_query_text(query, "✅ Seu WhatsApp já está conectado!")
        return

    image = qr_photo_bytes(qr_base64)
    if not image:
        await safe_edit_query_text(query, FRIENDLY_ERROR, reply_markup=main_menu_keyboard())
        return

    await query.message.reply_photo(
        photo=io.BytesIO(image),
        caption=(
            "Escaneie no WhatsApp: Configurações > Aparelhos conectados > Conectar aparelho.\n\n"
            "Não deu tempo? Eu mando um QR novo automaticamente. 😉"
        ),
    )
    schedule_idle_stop(context)
    _spawn_qr_watch(context, user.id, instance_name, query.message.chat_id)


async def _try_appear_offline(evolution: EvolutionClient, instance_name: str):
    """Best-effort: faz o numero aparecer offline logo apos conectar.

    Sessao aberta != aparecer online; 'unavailable' esconde o online constante
    da maquina. Se o build da Evolution nao aceitar, segue silenciosamente.
    """
    try:
        await evolution.set_presence(instance_name, "unavailable")
    except Exception:
        logger.debug("Presenca global indisponivel para %s", instance_name, exc_info=True)


def _cancel_previous_task(context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    previous = context.application.bot_data.get(key)
    if previous and not previous.done():
        previous.cancel()


def _spawn_connect_watch(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    instance_name: str,
    chat_id: int,
    code_message_id: int | None = None,
):
    key = f"connect_watch_{user_id}"
    _cancel_previous_task(context, key)

    async def watch():
        settings, db, evolution, _ = services(context)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONNECT_POLL_TIMEOUT
        shown_code = ""
        while loop.time() < deadline:
            await asyncio.sleep(CONNECT_POLL_SECONDS)
            if await evolution.connection_state(instance_name) == "open":
                await ensure_instance_webhook(settings, evolution, instance_name)
                await _try_appear_offline(evolution, instance_name)
                await context.bot.send_message(
                    chat_id,
                    "✅ WhatsApp conectado com sucesso! Pode criar suas campanhas. 🎉",
                    reply_markup=main_menu_keyboard(),
                )
                return
            # A Evolution renova o pairingCode a cada ciclo de QR (~20s); o codigo
            # antigo morre. Mantemos a mensagem do Telegram sempre com o vigente.
            if code_message_id:
                phone = await db.get_vendor_phone(user_id)
                if not phone:
                    continue
                try:
                    snapshot = await evolution.connection_snapshot(instance_name, phone)
                except Exception:
                    continue
                code = str(snapshot.get("pairingCode") or "").strip()
                if code and code != shown_code:
                    shown_code = code
                    try:
                        await context.bot.edit_message_text(
                            "🔐 Código ATUAL (o anterior expirou — copie e cole rápido):\n\n"
                            f"`{format_pairing_code(code)}`",
                            chat_id=chat_id,
                            message_id=code_message_id,
                            parse_mode="Markdown",
                        )
                    except Exception:
                        logger.debug("Falha ao atualizar mensagem do codigo", exc_info=True)
        await context.bot.send_message(
            chat_id,
            "⏰ Não conectou a tempo. Toque abaixo para tentar de novo:",
            reply_markup=connect_choice_keyboard(),
        )

    task = context.application.create_task(watch())
    context.application.bot_data[key] = task


def _spawn_qr_watch(context: ContextTypes.DEFAULT_TYPE, user_id: int, instance_name: str, chat_id: int):
    key = f"connect_watch_{user_id}"
    _cancel_previous_task(context, key)

    async def watch():
        settings, db, evolution, _ = services(context)
        for attempt in range(QR_REFRESH_ATTEMPTS):
            await asyncio.sleep(QR_REFRESH_SECONDS)
            if await evolution.connection_state(instance_name) == "open":
                await ensure_instance_webhook(settings, evolution, instance_name)
                await _try_appear_offline(evolution, instance_name)
                await context.bot.send_message(
                    chat_id,
                    "✅ WhatsApp conectado com sucesso! Pode criar suas campanhas. 🎉",
                    reply_markup=main_menu_keyboard(),
                )
                return
            try:
                qr_base64 = await evolution.ensure_fresh_qr(instance_name)
            except Exception:
                logger.warning("Falha ao renovar QR (tentativa %d)", attempt + 1, exc_info=True)
                continue
            image = qr_photo_bytes(qr_base64)
            if image:
                await context.bot.send_photo(
                    chat_id,
                    photo=io.BytesIO(image),
                    caption=f"🔄 QR atualizado ({attempt + 2}/{QR_REFRESH_ATTEMPTS}). O anterior expirou.",
                )
        if await evolution.connection_state(instance_name) == "open":
            await context.bot.send_message(chat_id, "✅ WhatsApp conectado! 🎉")
            return
        await context.bot.send_message(
            chat_id,
            "⏰ Os QR Codes expiraram sem conexão. Toque abaixo para tentar de novo:",
            reply_markup=connect_choice_keyboard(),
        )

    task = context.application.create_task(watch())
    context.application.bot_data[key] = task


# ----------------------------------------------------------------------------
# Nova campanha por botoes (escolha de perfil + continuacao de rascunho)
# ----------------------------------------------------------------------------


async def menu_new_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await require_user(update, context):
        return ConversationHandler.END

    settings, db, _, _ = services(context)
    user = query.from_user
    await db.ensure_vendor(user.id, user.username or f"user_{user.id}", f"vendor_{user.id}")

    active = await db.get_active_campaign_for_vendor(user.id)
    if active:
        await show_active_campaign_card(query, context, active)
        return ConversationHandler.END

    await safe_edit_query_text(
        query,
        "Que tipo de campanha vamos criar?\n\n"
        "⚡ Clientes de confiança — mais rápida, para quem já conhece a loja.\n"
        "🐢 Cautelosa — mais devagar, ideal para contatos novos.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚡ Clientes de confiança", callback_data="cprof_confianca_100")],
            [InlineKeyboardButton("🐢 Cautelosa (contatos novos)", callback_data="cprof_precaucao_100")],
            back_to_menu_row(),
        ]),
    )
    return WAITING_PROFILE_CHOICE


async def campaign_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await require_user(update, context):
        return ConversationHandler.END

    settings, db, evolution, _ = services(context)
    power = power_service(context)
    user = query.from_user
    instance_name = f"vendor_{user.id}"
    await db.ensure_vendor(user.id, user.username or f"user_{user.id}", instance_name)

    profile_id = (query.data or "")[len("cprof_"):]
    context.user_data["pending_campaign_profile_id"] = profile_id

    active = await db.get_active_campaign_for_vendor(user.id)
    if active:
        await show_active_campaign_card(query, context, active)
        return ConversationHandler.END

    try:
        await power.ensure_running()
    except Exception as exc:
        logger.exception("Erro ao ligar Evolution ao criar campanha")
        await notify_admins(
            context,
            f"⚠️ Falha ao ligar Evolution (nova campanha)\nUsuário: {user.full_name} ({user.id})\n{exc}",
        )
        await safe_edit_query_text(query, FRIENDLY_ERROR, reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    state = await evolution.connection_state(instance_name)
    if state != "open":
        await safe_edit_query_text(
            query,
            "Seu WhatsApp ainda não está conectado. 😕\n"
            "Conecte primeiro (leva 1 minutinho) e depois volte aqui.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📱 Conectar WhatsApp agora", callback_data="menu_connect")],
                back_to_menu_row(),
            ]),
        )
        schedule_idle_stop(context)
        return ConversationHandler.END

    await ensure_instance_webhook(settings, evolution, instance_name)
    await show_campaign_contact_source(query.message, db, user.id, profile_id)
    schedule_idle_stop(context)
    return WAITING_CONTACT_SOURCE


async def menu_lists_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await require_user(update, context):
        return ConversationHandler.END

    context.user_data.pop("pending_campaign_profile_id", None)
    db = context.application.bot_data["db"]
    await show_lists_menu(query, db, query.from_user.id, edit=True)
    return WAITING_CONTACT_SOURCE


async def show_active_campaign_card(query, context: ContextTypes.DEFAULT_TYPE, campaign):
    """Cartao com o estado da campanha ativa e as acoes possiveis."""
    campaign_id = campaign["id"]
    total = campaign["total_contacts"] or 0
    status = campaign["status"]

    if status in ("running", "paused"):
        state_text = "em andamento ⏳" if status == "running" else "pausada ⏸"
        text = f"📣 Sua campanha #{campaign_id} está {state_text}.\nContatos: {total}."
        keyboard = [
            [InlineKeyboardButton("📊 Ver andamento", callback_data="menu_status")],
            back_to_menu_row(),
        ]
    elif status == "ready":
        text = f"📣 Campanha #{campaign_id} pronta com {total} contatos.\nFalta só revisar e disparar!"
        keyboard = [
            [InlineKeyboardButton("🚀 Revisar e disparar", callback_data=f"cdisp_go_{campaign_id}")],
            [InlineKeyboardButton("🗑 Descartar campanha", callback_data=f"cdraft_discard_{campaign_id}")],
            back_to_menu_row(),
        ]
    else:  # draft
        text = (
            f"📣 Você tem uma campanha incompleta (#{campaign_id}) com {total} contatos.\n"
            "Quer continuar de onde parou?"
        )
        keyboard = [
            [InlineKeyboardButton("▶️ Continuar de onde parei", callback_data=f"cdraft_continue_{campaign_id}")],
            [InlineKeyboardButton("🗑 Descartar e começar outra", callback_data=f"cdraft_discard_{campaign_id}")],
            back_to_menu_row(),
        ]

    await safe_edit_query_text(query, text, reply_markup=InlineKeyboardMarkup(keyboard))


async def draft_continue_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await require_user(update, context):
        return ConversationHandler.END

    settings, db, _, _ = services(context)
    try:
        campaign_id = int((query.data or "").split("_")[-1])
    except ValueError:
        await safe_edit_query_text(query, "Campanha não encontrada.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    campaign = await db.get_campaign(campaign_id)
    if (
        not campaign
        or campaign["vendor_id"] != query.from_user.id
        or campaign["status"] != "draft"
    ):
        await safe_edit_query_text(query, "Essa campanha não está mais disponível.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    context.user_data["campaign_id"] = campaign_id
    context.user_data["pending_campaign_profile_id"] = campaign["profile_id"]
    context.user_data.pop("media_status_msg", None)
    context.user_data.pop("import_status_msg", None)
    campaign_dir(settings, campaign_id).mkdir(parents=True, exist_ok=True)

    if (campaign["total_contacts"] or 0) <= 0:
        await safe_edit_query_text(
            query,
            f"Continuando a campanha #{campaign_id}. 👍\n\n"
            "Envie os contatos: arquivo CSV, VCF, ZIP ou contatos do Telegram.\n"
            "Quando terminar, toque no botão abaixo.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Terminei os contatos", callback_data="ccontacts_done")],
                [InlineKeyboardButton("🗑 Descartar", callback_data=f"cdraft_discard_{campaign_id}")],
            ]),
        )
        return WAITING_CSV

    await safe_edit_query_text(
        query,
        f"Continuando a campanha #{campaign_id}. 👍\n\n"
        f"Contatos já importados: {campaign['total_contacts']}.\n"
        "Agora envie as fotos, uma por vez. Sem fotos? Toque no botão abaixo.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Continuar sem fotos", callback_data="cmedia_done")],
            [InlineKeyboardButton("🗑 Descartar", callback_data=f"cdraft_discard_{campaign_id}")],
        ]),
    )
    return WAITING_MEDIA


async def ensure_instance_webhook(settings: Settings, evolution: EvolutionClient, instance_name: str):
    if not (settings.webhook_auto_configure and settings.webhook_public_url and settings.webhook_token):
        return
    try:
        await evolution.set_instance_webhook(instance_name, settings.webhook_public_url)
    except Exception:
        logger.warning("Falha ao configurar webhook em %s", instance_name, exc_info=True)


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
        await send_main_menu(update.effective_message, "👋 Oi! Escolha o que fazer:")
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

    await update.message.reply_text("Preparando a conexão do WhatsApp... ⏳")
    try:
        await power.ensure_running()
    except Exception as exc:
        logger.exception("Erro ao ligar Evolution")
        await notify_admins(
            context,
            f"⚠️ Falha ao ligar Evolution (/login)\nUsuário: {user.full_name} ({user.id})\n{exc}",
        )
        await update.message.reply_text(FRIENDLY_ERROR)
        return

    state = await evolution.connection_state(instance_name)
    if state == "open":
        await ensure_instance_webhook(settings, evolution, instance_name)
        await update.message.reply_text("✅ Seu WhatsApp já está conectado!")
        schedule_idle_stop(context)
        return

    phone = await db.get_vendor_phone(user.id)
    if phone:
        await update.message.reply_text(
            f"Vamos conectar o WhatsApp do número {phone}. 📱\n\n"
            "🔢 Com código — você digita um número no WhatsApp. O mais fácil!\n"
            "📷 Com QR Code — aponta a câmera.",
            reply_markup=connect_choice_keyboard(),
        )
    else:
        await update.message.reply_text(
            "Para conectar, preciso do seu número uma única vez. 😊",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📱 Conectar WhatsApp", callback_data="menu_connect")],
            ]),
        )
    schedule_idle_stop(context)


async def conexao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return

    settings, db, evolution, _ = services(context)
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
    if state == "open":
        await ensure_instance_webhook(settings, evolution, instance_name)
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
        await notify_admins(
            context,
            f"⚠️ Falha ao ligar Evolution (/nova)\nUsuário: {user.full_name} ({user.id})\n{exc}",
        )
        await update.message.reply_text(FRIENDLY_ERROR)
        return ConversationHandler.END

    state = await evolution.connection_state(instance_name)
    if state != "open":
        await update.message.reply_text(
            "Seu WhatsApp ainda não está conectado. 😕\n"
            "Conecte primeiro (leva 1 minutinho) e rode /nova de novo.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📱 Conectar WhatsApp agora", callback_data="menu_connect")],
            ]),
        )
        schedule_idle_stop(context)
        return ConversationHandler.END

    profile_id = profile_from_command(update, settings.default_profile)
    context.user_data["pending_campaign_profile_id"] = profile_id
    await ensure_instance_webhook(settings, evolution, instance_name)
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
        f"Campanha #{campaign_id} criada. 🎉\n"
        f"Ritmo: {profile.label}.\n"
        f"Envie CSV, VCF bruto, ou ZIP com CSV/VCF dentro.\n"
        f"Limite: {contact_limit_label(contact_limit)}.\n\n"
        "Se o Telegram transformar em cartões de contato, tudo bem também.\n"
        "Quando terminar, toque no botão abaixo.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Terminei os contatos", callback_data="ccontacts_done")],
        ]),
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
    result = await db.copy_contact_list_to_campaign(list_id, query.from_user.id, campaign_id, limit)
    context.user_data["campaign_id"] = campaign_id
    campaign_dir(settings, campaign_id).mkdir(parents=True, exist_ok=True)
    extra = ""
    if result["blacklisted"]:
        extra = f"\nIgnorados por estarem na blacklist: {result['blacklisted']}."
    await query.edit_message_text(
        f"Campanha #{campaign_id} criada com {result['total']} contatos.{extra}\n"
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
    lines = [
        "Lista atualizada.",
        f"Novos: {result['added']}",
        f"Duplicados ignorados: {result['duplicates']}",
        f"Nomes atualizados: {result['updated']}",
    ]
    if result.get("blacklisted"):
        lines.append(f"Ignorados por estarem na blacklist: {result['blacklisted']}")
    lines.append(f"Total da lista: {result['total']}")
    return "\n".join(lines)


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
    extra = ""
    if total["blacklisted"]:
        extra = f"\nIgnorados por estarem na blacklist: {total['blacklisted']}."
    await upsert_status_message(
        context,
        context.user_data,
        "import_status_msg",
        update.effective_chat.id,
        f"Contatos importados: {total['total']}.{extra}\n"
        "Envie mais arquivos se quiser.\n\n"
        "Quando terminar, toque no botão abaixo.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Terminei os contatos", callback_data="ccontacts_done")],
        ]),
    )
    return WAITING_CSV


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
    result = await db.add_contacts(campaign_id, selected)
    extra_parts = []
    if len(selected) != len(contacts):
        extra_parts.append(f"{len(contacts) - len(selected)} contato(s) excederam o limite e foram ignorados.")
    if result["blacklisted"]:
        extra_parts.append(f"Ignorados por estarem na blacklist: {result['blacklisted']}.")
    extra = ("\n" + "\n".join(extra_parts)) if extra_parts else ""
    await update.message.reply_text(f"Contato recebido. Total da campanha: {result['total']}.{extra}\nUse /pronto quando terminar.")
    return WAITING_CSV


async def contacts_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, db, _, _ = services(context)
    message = update.effective_message
    campaign_id = context.user_data.get("campaign_id")
    if not campaign_id:
        await message.reply_text("Campanha nao encontrada. Use /nova.")
        return ConversationHandler.END

    campaign = await db.get_campaign(campaign_id)
    if not campaign or campaign["total_contacts"] <= 0:
        await message.reply_text("Nenhum contato valido importado ainda.")
        return WAITING_CSV

    context.user_data.pop("import_status_msg", None)
    await message.reply_text(
        f"Contatos confirmados: {campaign['total_contacts']}. ✅\n"
        "Agora envie as fotos, uma por vez.\n\n"
        "Sem fotos? Toque no botão abaixo.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Continuar sem fotos", callback_data="cmedia_done")],
        ]),
    )
    return WAITING_MEDIA


async def contacts_done_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return await contacts_done(update, context)


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
    context.user_data.pop("import_status_msg", None)
    await upsert_status_message(
        context,
        context.user_data,
        "media_status_msg",
        update.effective_chat.id,
        f"Imagem {count + 1} recebida. ✅ Envie outra ou toque no botão para continuar.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Continuar", callback_data="cmedia_done")],
        ]),
    )
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
    context.user_data.pop("import_status_msg", None)
    await upsert_status_message(
        context,
        context.user_data,
        "media_status_msg",
        update.effective_chat.id,
        f"Imagem {count + 1} recebida. ✅ Envie outra ou toque no botão para continuar.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Continuar", callback_data="cmedia_done")],
        ]),
    )
    return WAITING_MEDIA


def media_size_allowed(file_size: int | None, max_mb: int) -> bool:
    if not file_size:
        return True
    return file_size <= max_mb * 1024 * 1024


async def media_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("media_status_msg", None)
    context.user_data.pop("import_status_msg", None)
    await update.effective_message.reply_text(
        "Agora escreva a mensagem que vai junto das fotos. 💬\n\n"
        "Só as fotos, sem texto? Toque no botão abaixo.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭ Enviar sem texto", callback_data="ccap_none")],
        ]),
    )
    return WAITING_CAPTION


async def media_done_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return await media_done(update, context)


async def no_caption_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return await receive_no_caption(update, context)


async def receive_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, db, _, _ = services(context)
    campaign_id = context.user_data.get("campaign_id")
    if not campaign_id:
        return ConversationHandler.END

    await db.set_caption(campaign_id, update.message.text)
    await reply_campaign_ready(update.effective_message, db, campaign_id)
    return ConversationHandler.END


async def receive_no_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, db, _, _ = services(context)
    campaign_id = context.user_data.get("campaign_id")
    if not campaign_id:
        return ConversationHandler.END

    media_count = await db.count_media(campaign_id)
    if media_count <= 0:
        await update.effective_message.reply_text(
            "Sem imagem e sem texto não há o que enviar. 🙈 Escreva uma mensagem:"
        )
        return WAITING_CAPTION

    await db.set_caption(campaign_id, "")
    await reply_campaign_ready(update.effective_message, db, campaign_id)
    return ConversationHandler.END


async def reply_campaign_ready(message, db: Database, campaign_id: int):
    campaign = await db.get_campaign(campaign_id)
    await message.reply_text(
        f"Campanha #{campaign_id} pronta! 🎉\n"
        f"Contatos: {campaign['total_contacts']}\n\n"
        "Falta só a revisão final antes do disparo.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Revisar e disparar", callback_data=f"cdisp_go_{campaign_id}")],
            [InlineKeyboardButton("🗑 Descartar campanha", callback_data=f"cdraft_discard_{campaign_id}")],
            back_to_menu_row(),
        ]),
    )


async def disparar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return

    _, db, _, _ = services(context)
    campaign = await db.get_active_campaign_for_vendor(update.effective_user.id)
    if not campaign:
        await update.message.reply_text("Nenhuma campanha ativa.")
        return

    if campaign["status"] != "ready":
        await update.message.reply_text(f"Campanha #{campaign['id']} esta em status {campaign['status']}.")
        return

    text, keyboard = await _build_preflight(db, campaign["id"], campaign["vendor_id"])
    await update.message.reply_text(text, reply_markup=keyboard)


async def _build_preflight(db: Database, campaign_id: int, vendor_id: int) -> tuple[str, InlineKeyboardMarkup]:
    phones = await db.pending_phones_for_campaign(campaign_id)
    classification = await db.classify_phones(vendor_id, phones)
    counts = {"hot": 0, "warm": 0, "cold": 0, "unknown": 0}
    for cls in classification.values():
        counts[cls] = counts.get(cls, 0) + 1

    lines = [
        f"Campanha #{campaign_id} — revisão rápida 🔍",
        f"Vou enviar para {len(phones)} pessoas:",
        "",
        f"  ✅ Responderam recentemente: {counts['hot']}",
        f"  🙂 Já receberam antes: {counts['warm']}",
        f"  🆕 Nunca receberam: {counts['unknown']}",
        f"  💤 Sumidos há muito tempo: {counts['cold']}",
    ]
    if not phones:
        lines.append("")
        lines.append("Nenhum contato pendente para disparar.")
    if counts["cold"]:
        lines.append("")
        lines.append(
            f"Antes de disparar, vou remover {counts['cold']} contatos sumidos há muito tempo "
            "(eles deixam seu número em risco de bloqueio)."
        )

    keyboard_rows = []
    if phones:
        keyboard_rows.append([InlineKeyboardButton("🚀 Preparar e disparar", callback_data=f"disparar_go:{campaign_id}")])
        keyboard_rows.append(
            [InlineKeyboardButton("🔍 Checar números no WhatsApp", callback_data=f"disparar_check_wa:{campaign_id}")]
        )
    keyboard_rows.append([InlineKeyboardButton("✖️ Cancelar disparo", callback_data=f"disparar_cancel:{campaign_id}")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)


def _whatsapp_result_exists(item: dict) -> bool:
    for key in ("exists", "isWhatsApp", "isWhatsapp"):
        if key in item:
            return bool(item.get(key))
    return bool(item.get("jid") or item.get("id"))


def _whatsapp_result_phone(item: dict) -> str:
    for key in ("number", "phone", "jid", "id"):
        value = str(item.get(key) or "").strip()
        if not value or "@lid" in value:
            continue
        digits = normalize_phone(value)
        if digits:
            return digits
    return ""


async def _start_dispatch(query, context: ContextTypes.DEFAULT_TYPE, campaign_id: int):
    settings, db, evolution, scheduler = services(context)
    power = power_service(context)

    campaign = await db.get_campaign_with_vendor(campaign_id)
    if not campaign:
        await safe_edit_query_text(query, "Campanha nao encontrada.")
        return

    phones = await db.pending_phones_for_campaign(campaign_id)
    if not phones:
        text, keyboard = await _build_preflight(db, campaign_id, campaign["vendor_id"])
        await safe_edit_query_text(query, text, reply_markup=keyboard)
        return

    # Contatos "sumidos ha muito tempo" saem automaticamente: sao o maior risco
    # de denuncia, e a vendedora nao precisa entender a classificacao.
    classification = await db.classify_phones(campaign["vendor_id"], phones)
    cold_phones = [p for p, cls in classification.items() if cls == "cold"]
    dropped_cold = 0
    if cold_phones:
        dropped_cold = await db.fail_pending_phones(campaign_id, cold_phones, "preflight: frio")

    try:
        await power.ensure_running()
    except Exception as exc:
        logger.exception("Erro ao ligar Evolution")
        await notify_admins(
            context,
            f"⚠️ Falha ao ligar Evolution (disparo)\nCampanha: #{campaign_id}\n{exc}",
        )
        await safe_edit_query_text(query, FRIENDLY_ERROR)
        return

    state = await evolution.connection_state(campaign["instance_name"])
    if state != "open":
        await safe_edit_query_text(query, "Aguardando conexao do WhatsApp abrir...")
        if not await evolution.wait_until_open(campaign["instance_name"]):
            await safe_edit_query_text(query, "WhatsApp nao esta conectado. Use /login antes de disparar.")
            await power.stop_if_idle(db)
            return

    await ensure_instance_webhook(settings, evolution, campaign["instance_name"])
    progress_message = await context.bot.send_message(query.message.chat_id, "Preparando campanha...")
    ok = await scheduler.start(campaign_id, query.message.chat_id, progress_message.message_id)
    if not ok:
        await progress_message.edit_text("Campanha ja esta em execucao.")
    elif dropped_cold:
        await progress_message.edit_text(
            f"Removidos {dropped_cold} contatos sumidos há muito tempo (proteção do seu número). "
            "Iniciando a campanha..."
        )


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


async def dispatch_review_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Botao 'Revisar e disparar' do cartao de campanha pronta."""
    query = update.callback_query
    settings, db, _, _ = services(context)
    try:
        campaign_id = int((query.data or "").split("_")[-1])
    except ValueError:
        await query.answer("Ação inválida.", show_alert=True)
        return

    campaign = await db.get_campaign(campaign_id)
    if not campaign:
        await query.answer("Campanha não encontrada.", show_alert=True)
        return
    if campaign["vendor_id"] != query.from_user.id and not is_authorized(settings, query.from_user.id):
        await query.answer("Sem permissão.", show_alert=True)
        return
    if campaign["status"] != "ready":
        await query.answer(f"Campanha está {campaign['status']}.", show_alert=True)
        return

    await query.answer()
    text, keyboard = await _build_preflight(db, campaign_id, campaign["vendor_id"])
    await query.message.reply_text(text, reply_markup=keyboard)


async def draft_discard_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Botao 'Descartar' de campanha draft/pronta."""
    query = update.callback_query
    settings, db, _, scheduler = services(context)
    try:
        campaign_id = int((query.data or "").split("_")[-1])
    except ValueError:
        await query.answer("Ação inválida.", show_alert=True)
        return

    campaign = await db.get_campaign(campaign_id)
    if not campaign:
        await query.answer("Campanha não encontrada.", show_alert=True)
        return
    if campaign["vendor_id"] != query.from_user.id and not is_authorized(settings, query.from_user.id):
        await query.answer("Sem permissão.", show_alert=True)
        return

    await query.answer()
    ok = await scheduler.cancel_campaign(campaign_id)
    context.user_data.pop("campaign_id", None)
    await safe_edit_query_text(
        query,
        f"Campanha #{campaign_id} descartada. 🗑" if ok else f"Campanha #{campaign_id} não estava ativa.",
        reply_markup=main_menu_keyboard(),
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("menu_"):
        await handle_menu_callback(update, context)
        return

    if data.startswith("cdisp_go_"):
        await dispatch_review_action(update, context)
        return

    if data.startswith("cdraft_discard_"):
        await draft_discard_action(update, context)
        return

    if data == "cconnect_pair":
        await start_pairing_connect(query, context)
        return

    if data == "cconnect_qr":
        await start_qr_connect(query, context)
        return

    if data.startswith("cconnect_reset_"):
        await reset_and_connect(query, context, data[len("cconnect_reset_"):])
        return

    if data.startswith("campaign_"):
        await handle_campaign_callback(update, context)
        return

    if data.startswith("disparar_"):
        await handle_disparar_callback(update, context)
        return

    if data.startswith("bl_"):
        await handle_blacklist_callback(update, context)
        return

    if data.startswith(("src_", "list_")):
        await query.answer("Esse menu expirou. Use /menu para recomeçar.", show_alert=True)
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
        settings, db, _, _ = services(context)
        if not is_authorized(settings, query.from_user.id):
            await query.answer("Apenas administradores podem aprovar acesso.", show_alert=True)
            return
        await query.answer()
        target_id = int(data.split("_", 1)[1])
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
        settings, db, _, _ = services(context)
        if not is_authorized(settings, query.from_user.id):
            await query.answer("Apenas administradores podem recusar acesso.", show_alert=True)
            return
        await query.answer()
        target_id = int(data.split("_", 1)[1])
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


async def handle_disparar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    settings, db, evolution, _ = services(context)

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
    if campaign["vendor_id"] != query.from_user.id and not is_authorized(settings, query.from_user.id):
        await query.answer("Sem permissao.", show_alert=True)
        return
    if campaign["status"] != "ready":
        await query.answer(f"Campanha esta em status {campaign['status']}.", show_alert=True)
        return

    if action == "disparar_cancel":
        await query.answer("Disparo cancelado. Campanha continua pronta.")
        await safe_edit_query_text(
            query,
            f"Disparo da campanha #{campaign_id} cancelado.\nUse /disparar quando quiser revisitar.",
        )
        return

    if action == "disparar_drop_cold":
        phones = await db.pending_phones_for_campaign(campaign_id)
        classification = await db.classify_phones(campaign["vendor_id"], phones)
        cold_phones = [p for p, cls in classification.items() if cls == "cold"]
        removed = await db.fail_pending_phones(campaign_id, cold_phones, "preflight: frio")
        await query.answer(f"Removidos {removed} contatos frios.")
        text, keyboard = await _build_preflight(db, campaign_id, campaign["vendor_id"])
        await safe_edit_query_text(query, text, reply_markup=keyboard)
        return

    if action == "disparar_check_wa":
        await query.answer("Verificando numeros no WhatsApp...")
        campaign_with_vendor = await db.get_campaign_with_vendor(campaign_id)
        instance_name = campaign_with_vendor["instance_name"]
        # Garante que a Evolution esta no ar antes de chamar.
        try:
            await power_service(context).ensure_running()
        except Exception:
            logger.exception("Erro ao ligar Evolution para verificar numeros")
            await safe_edit_query_text(query, "Nao consegui ligar a Evolution para verificar numeros.")
            return

        phones = await db.pending_phones_for_campaign(campaign_id)
        invalid: set[str] = set()
        checked = 0
        errors: list[str] = []
        # Quebra em batches para nao mandar uma lista enorme num POST.
        for chunk_start in range(0, len(phones), 50):
            chunk = phones[chunk_start : chunk_start + 50]
            try:
                results = await evolution.whatsapp_numbers(instance_name, chunk)
            except Exception as exc:
                logger.exception("Erro em whatsapp_numbers (chunk %d)", chunk_start)
                errors.append(str(exc)[:160])
                continue
            checked += len(chunk)
            seen_existing = set()
            for index, item in enumerate(results):
                if not isinstance(item, dict):
                    continue
                number = _whatsapp_result_phone(item)
                exists = _whatsapp_result_exists(item)
                if exists and not number and index < len(chunk):
                    number = chunk[index]
                jid = item.get("jid") or ""
                if exists:
                    seen_existing.add(number)
                    if "@lid" in jid and number:
                        # Aproveita pra linkar phone <-> @lid em contact_health.
                        try:
                            await db.link_chat_lid(campaign["vendor_id"], number, jid)
                        except Exception:
                            logger.debug("Falha ao linkar @lid", exc_info=True)
            for phone in chunk:
                if phone not in seen_existing:
                    invalid.add(phone)

        if errors:
            text, keyboard = await _build_preflight(db, campaign_id, campaign["vendor_id"])
            await safe_edit_query_text(
                query,
                "Verificacao incompleta. Nada foi removido.\n"
                "Tente novamente quando a Evolution estiver estavel.\n\n"
                + text,
                reply_markup=keyboard,
            )
            return

        if checked != len(phones):
            text, keyboard = await _build_preflight(db, campaign_id, campaign["vendor_id"])
            await safe_edit_query_text(
                query,
                "Verificacao incompleta. Nada foi removido.\n"
                "Nem todos os contatos pendentes foram conferidos.\n\n"
                + text,
                reply_markup=keyboard,
            )
            return

        removed = await db.fail_pending_phones(campaign_id, invalid, "preflight: sem whatsapp")
        text, keyboard = await _build_preflight(db, campaign_id, campaign["vendor_id"])
        prefix = (
            f"Verificacao: {removed} numeros sem WhatsApp foram removidos.\n\n"
            if removed
            else f"Verificacao: {checked} pendentes conferidos, todos existem no WhatsApp.\n\n"
        )
        await safe_edit_query_text(query, prefix + text, reply_markup=keyboard)
        return

    if action == "disparar_go":
        await query.answer("Iniciando campanha...")
        await _start_dispatch(query, context, campaign_id)
        return

    await query.answer("Acao desconhecida.", show_alert=True)


async def handle_blacklist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    settings, db, _, _ = services(context)

    if data == "bl_menu_list":
        await query.answer()
        text, keyboard = await _render_blacklist_page(db, offset=0)
        await query.message.reply_text(text, reply_markup=keyboard)
        return

    if data.startswith("bl_page:"):
        try:
            offset = max(0, int(data.split(":", 1)[1]))
        except ValueError:
            await query.answer("Pagina invalida.", show_alert=True)
            return
        await query.answer()
        text, keyboard = await _render_blacklist_page(db, offset)
        await safe_edit_query_text(query, text, reply_markup=keyboard)
        return

    if data.startswith("bl_add_last:"):
        try:
            campaign_id = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Acao invalida.", show_alert=True)
            return

        campaign = await db.get_campaign(campaign_id)
        if not campaign:
            await query.answer("Campanha nao encontrada.", show_alert=True)
            return
        if campaign["vendor_id"] != query.from_user.id and not is_authorized(settings, query.from_user.id):
            await query.answer("Sem permissao.", show_alert=True)
            return

        last = await db.last_processed_contact_for_campaign(campaign_id)
        if not last:
            await query.answer("Ainda nao houve envio nesta campanha.", show_alert=True)
            return

        result = await db.add_to_blacklist(
            last["phone"],
            reason_code="wrong_person",
            reason_note=f"campanha #{campaign_id}",
            source="manual",
            added_by_user_id=query.from_user.id,
            added_by_vendor_id=campaign["vendor_id"],
        )
        if result["added"]:
            extra = f" e removido de {result['removed_pending']} envio(s) pendente(s)" if result["removed_pending"] else ""
            await query.answer(f"{last['phone']} adicionado a blacklist{extra}.")
        else:
            await query.answer(f"{last['phone']} ja estava na blacklist.")
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


# ----------------------------------------------------------------------------
# Blacklist
# ----------------------------------------------------------------------------


def _format_blacklist_entry(row) -> str:
    note = row["reason_note"] or ""
    parts = [f"{row['phone']} ({row['reason_code']}, {row['source']})"]
    if note:
        parts.append(f"  motivo: {note}")
    return "\n".join(parts)


async def blacklist_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return

    _, db, _, _ = services(context)
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Uso:\n"
            "/blacklist 5511999998888 motivo opcional\n"
            "Para importar varios, use /blacklist_arquivo."
        )
        return

    phone = usable_phone(args[0])
    if not phone:
        await update.message.reply_text(
            "Telefone invalido. Use o formato com DDD e (preferencialmente) DDI: 5511999998888."
        )
        return

    note = " ".join(args[1:]).strip() or None
    result = await db.add_to_blacklist(
        phone,
        reason_code="manual_request",
        reason_note=note,
        source="manual",
        added_by_user_id=update.effective_user.id,
        added_by_vendor_id=update.effective_user.id,
    )
    if result["added"]:
        msg = f"Telefone {phone} adicionado a blacklist."
        if result["removed_pending"]:
            msg += f"\nRemovido de {result['removed_pending']} envio(s) pendente(s) em campanhas em andamento."
        await update.message.reply_text(msg)
    else:
        existing = await db.get_blacklist_entry(phone)
        if existing:
            await update.message.reply_text(
                f"Telefone {phone} ja estava na blacklist.\n"
                f"  motivo: {existing['reason_note'] or '-'}\n"
                f"  desde: {existing['added_at']}"
            )
        else:
            await update.message.reply_text(f"Telefone {phone} ja estava na blacklist.")


async def blacklist_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return

    _, db, _, _ = services(context)
    args = context.args or []
    if not args:
        await update.message.reply_text("Uso: /blacklist_remover 5511999998888")
        return

    phone = usable_phone(args[0])
    if not phone:
        await update.message.reply_text(
            "Telefone invalido. Use o formato com DDD e (preferencialmente) DDI: 5511999998888."
        )
        return

    removed = await db.remove_from_blacklist(phone)
    await update.message.reply_text(
        f"Telefone {phone} removido da blacklist." if removed else f"Telefone {phone} nao estava na blacklist."
    )


def _blacklist_page_keyboard(offset: int, limit: int, total: int) -> InlineKeyboardMarkup | None:
    buttons = []
    if offset > 0:
        prev_offset = max(0, offset - limit)
        buttons.append(InlineKeyboardButton("Anterior", callback_data=f"bl_page:{prev_offset}"))
    if offset + limit < total:
        next_offset = offset + limit
        buttons.append(InlineKeyboardButton("Proxima", callback_data=f"bl_page:{next_offset}"))
    return InlineKeyboardMarkup([buttons]) if buttons else None


async def _render_blacklist_page(db: Database, offset: int) -> tuple[str, InlineKeyboardMarkup | None]:
    page = await db.list_blacklist(offset=offset, limit=BLACKLIST_PAGE_SIZE)
    if not page["rows"]:
        return ("Blacklist vazia." if page["total"] == 0 else "Pagina vazia.", None)
    lines = [
        f"Blacklist ({page['offset'] + 1}-{page['offset'] + len(page['rows'])} de {page['total']}):",
        "",
    ]
    lines.extend(_format_blacklist_entry(row) for row in page["rows"])
    keyboard = _blacklist_page_keyboard(page["offset"], page["limit"], page["total"])
    return ("\n".join(lines), keyboard)


async def blacklist_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return
    _, db, _, _ = services(context)
    text, keyboard = await _render_blacklist_page(db, offset=0)
    await update.message.reply_text(text, reply_markup=keyboard)


async def blacklist_file_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_user(update, context):
        return ConversationHandler.END
    context.user_data["blacklist_phones"] = []
    await update.message.reply_text(
        "Envie um arquivo .csv, .vcf ou .zip com os telefones a bloquear, ou encaminhe contatos pelo Telegram.\n"
        "Aceito ate 10000 telefones por importacao.\n"
        "Use /pronto quando terminar, ou /cancelar para abortar."
    )
    return WAITING_BLACKLIST_FILE


async def blacklist_file_receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings, _, _, _ = services(context)
    try:
        contacts = await contacts_from_document(update, context, settings, 10000)
    except Exception as exc:
        await update.message.reply_text(str(exc))
        return WAITING_BLACKLIST_FILE
    bucket = context.user_data.setdefault("blacklist_phones", [])
    bucket.extend(item["phone"] for item in contacts)
    unique_count = len({phone for phone in bucket if phone})
    await update.message.reply_text(
        f"Recebidos: {unique_count} telefones unicos ate agora.\nUse /pronto para confirmar ou envie mais."
    )
    return WAITING_BLACKLIST_FILE


async def blacklist_file_receive_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contacts = contacts_from_telegram_contact(update)
    if not contacts:
        await update.message.reply_text(
            "Contato sem numero completo. Envie um arquivo com os telefones se o Telegram cortar o DDD."
        )
        return WAITING_BLACKLIST_FILE
    bucket = context.user_data.setdefault("blacklist_phones", [])
    bucket.extend(item["phone"] for item in contacts)
    unique_count = len({phone for phone in bucket if phone})
    await update.message.reply_text(
        f"Recebidos: {unique_count} telefones unicos ate agora.\nUse /pronto para confirmar ou envie mais."
    )
    return WAITING_BLACKLIST_FILE


async def blacklist_file_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, db, _, _ = services(context)
    phones = list({phone for phone in context.user_data.get("blacklist_phones", []) if phone})
    context.user_data.pop("blacklist_phones", None)
    if not phones:
        await update.message.reply_text("Nenhum telefone valido recebido.")
        return ConversationHandler.END

    added = 0
    already = 0
    purged_total = 0
    for phone in phones:
        result = await db.add_to_blacklist(
            phone,
            reason_code="imported",
            reason_note=None,
            source="imported",
            added_by_user_id=update.effective_user.id,
            added_by_vendor_id=update.effective_user.id,
        )
        if result["added"]:
            added += 1
        else:
            already += 1
        purged_total += result["removed_pending"]

    parts = [
        "Importacao concluida.",
        f"Adicionados: {added}",
        f"Ja estavam na blacklist: {already}",
    ]
    if purged_total:
        parts.append(f"Removidos de filas pendentes: {purged_total}")
    await update.message.reply_text("\n".join(parts))
    return ConversationHandler.END


async def blacklist_file_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("blacklist_phones", None)
    await update.message.reply_text("Importacao para a blacklist cancelada.")
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    exc_info = None
    if context.error:
        exc_info = (type(context.error), context.error, context.error.__traceback__)
    logger.error("Erro inesperado no handler do Telegram", exc_info=exc_info)

    user = getattr(update, "effective_user", None)
    who = f"{user.full_name} ({user.id})" if user else "usuário desconhecido"
    error_text = f"{type(context.error).__name__}: {context.error}" if context.error else "sem detalhe"
    try:
        await notify_admins(context, f"⚠️ Erro no bot\nUsuário: {who}\n{error_text}")
    except Exception:
        pass

    message = getattr(update, "effective_message", None)
    if message:
        try:
            await message.reply_text(FRIENDLY_ERROR)
        except Exception:
            pass


async def safe_edit_query_text(query, text: str, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as exc:
        if "Message is not modified" in str(exc):
            return
        raise


async def upsert_status_message(
    context: ContextTypes.DEFAULT_TYPE,
    user_data: dict,
    key: str,
    chat_id: int,
    text: str,
    reply_markup=None,
):
    """Uma unica mensagem de status por etapa: edita a existente em vez de
    multiplicar mensagens novas no chat a cada foto/arquivo recebido."""
    message_id = user_data.get(key)
    if message_id:
        try:
            await context.bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup
            )
            return
        except BadRequest:
            pass
        except Exception:
            logger.debug("Falha ao editar status, enviando mensagem nova", exc_info=True)
    message = await context.bot.send_message(chat_id, text, reply_markup=reply_markup)
    user_data[key] = message.message_id


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
