"""Servidor HTTP que recebe webhooks da Evolution API.

Roda em paralelo com o poller do Telegram, dentro do mesmo loop asyncio.
A Evolution e configurada por instancia (em main.py / em /login) para chamar:

  POST <WEBHOOK_PUBLIC_URL>/messages-upsert
  POST <WEBHOOK_PUBLIC_URL>/messages-update
  POST <WEBHOOK_PUBLIC_URL>/connection-update
  POST <WEBHOOK_PUBLIC_URL>/send-message

A URL inclui um token na path; a unica forma de chegar no servidor e estar na
rede `bot-net` do Docker, e ainda assim o token precisa bater. A porta interna
(WEBHOOK_LISTEN_PORT, default 8090) nao e exposta para fora da VPS.

Eventos relevantes para o anti-ban:
- MESSAGES_UPSERT  -> resposta entrante (cliente respondeu)
- MESSAGES_UPDATE  -> mudanca de status (delivered/read)
- CONNECTION_UPDATE -> sintoma de shadowban / drop
- SEND_MESSAGE     -> confirmacao do nosso envio (info, nao acionavel hoje)

Eventos sao gravados em contact_health do Database. Vendor e identificado pelo
campo `instance` no payload (formato `vendor_<telegram_id>`).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from aiohttp import web

from .db import Database


logger = logging.getLogger(__name__)


# Status numericos do Baileys vs nomes que algumas versoes da Evolution mandam.
STATUS_DELIVERED = {3, "DELIVERY_ACK", "DELIVERED", "SERVER_ACK"}
STATUS_READ = {4, 5, "READ", "PLAYED"}


def _normalize_status(status):
    if isinstance(status, str):
        clean = status.strip()
        if clean.isdigit():
            return int(clean)
        return clean.upper()
    return status


def _vendor_id_from_instance(instance: Optional[str]) -> Optional[int]:
    if not instance:
        return None
    match = re.match(r"^vendor_(\d+)$", instance)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _parse_jid(jid: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Extrai (phone, chat_lid) a partir de um remoteJid.

    - "5511999998888@s.whatsapp.net" -> ("5511999998888", None)
    - "5511999998888@c.us"           -> ("5511999998888", None)
    - "12345678901@lid"              -> (None, "12345678901@lid")
    - None / formato desconhecido    -> (None, None)
    """
    if not jid:
        return None, None
    raw = str(jid).strip()
    if not raw:
        return None, None
    if raw.endswith("@lid") or "@lid" in raw:
        # Mantem o '@lid' inteiro como identificador.
        return None, raw
    if "@s.whatsapp.net" in raw or "@c.us" in raw:
        digits = re.sub(r"\D", "", raw.split("@", 1)[0])
        return (digits or None), None
    # JID de grupo, broadcast ou outro formato — ignora.
    return None, None


def _extract_text(message_obj) -> str:
    if not isinstance(message_obj, dict):
        return ""
    for key in ("conversation", "extendedTextMessage", "ephemeralMessage", "buttonsResponseMessage", "listResponseMessage"):
        value = message_obj.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            text = value.get("text") or value.get("selectedDisplayText") or value.get("title")
            if isinstance(text, str):
                return text
            if "message" in value:
                inner = _extract_text(value["message"])
                if inner:
                    return inner
    return ""


class WebhookServer:
    def __init__(self, db: Database, host: str, port: int, token: str, suspicion_tracker=None):
        self.db = db
        self.host = host
        self.port = port
        self.token = (token or "").strip()
        self.suspicion = suspicion_tracker
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

    async def start(self):
        if not self.token:
            logger.warning("WEBHOOK_TOKEN vazio: webhook server nao sera iniciado.")
            return
        app = web.Application()
        prefix = f"/webhook/{self.token}"
        app.router.add_post(f"{prefix}/messages-upsert", self._handle_messages_upsert)
        app.router.add_post(f"{prefix}/messages-update", self._handle_messages_update)
        app.router.add_post(f"{prefix}/connection-update", self._handle_connection_update)
        app.router.add_post(f"{prefix}/send-message", self._handle_send_message)
        # Catch-all com 404 para logar tentativas mal direcionadas (sem expor o token).
        app.router.add_route("*", "/{tail:.*}", self._handle_unknown)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        logger.info("Webhook server escutando em %s:%s", self.host, self.port)

    async def close(self):
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _read_payload(self, request: web.Request) -> dict:
        try:
            return await request.json()
        except json.JSONDecodeError:
            try:
                raw = await request.text()
            except Exception:
                raw = ""
            logger.debug("Webhook payload nao-JSON: %r", raw[:200])
            return {}
        except Exception:
            logger.debug("Webhook erro lendo payload", exc_info=True)
            return {}

    async def _handle_messages_upsert(self, request: web.Request) -> web.Response:
        payload = await self._read_payload(request)
        vendor_id = _vendor_id_from_instance(payload.get("instance"))
        if not vendor_id:
            return web.json_response({"ok": False, "reason": "instance"})

        data = payload.get("data") or payload.get("message") or {}
        # Algumas versoes encapsulam em lista.
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            return web.json_response({"ok": True})

        key = data.get("key") or {}
        if key.get("fromMe") is True:
            # Mensagem nossa, nao e resposta do cliente.
            return web.json_response({"ok": True, "skipped": "from_me"})

        phone, chat_lid = _parse_jid(key.get("remoteJid"))
        if phone is None and chat_lid is None:
            return web.json_response({"ok": True, "skipped": "jid"})

        text = _extract_text(data.get("message"))
        try:
            await self.db.record_event(vendor_id, phone, "replied", chat_lid=chat_lid, text=text)
        except Exception:
            logger.exception("Falha ao gravar messages-upsert")
        return web.json_response({"ok": True})

    async def _handle_messages_update(self, request: web.Request) -> web.Response:
        payload = await self._read_payload(request)
        vendor_id = _vendor_id_from_instance(payload.get("instance"))
        if not vendor_id:
            return web.json_response({"ok": False, "reason": "instance"})

        items = payload.get("data")
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            return web.json_response({"ok": True, "skipped": "no_items"})

        for item in items:
            if not isinstance(item, dict):
                continue
            key = item.get("key") or {}
            # Status pode vir em "status" (top-level) ou aninhado em "update.status".
            status = item.get("status")
            if status is None and isinstance(item.get("update"), dict):
                status = item["update"].get("status")
            status = _normalize_status(status)

            if not key.get("fromMe"):
                # Update so importa para mensagens nossas (delivered/read partem do destinatario).
                continue

            phone, chat_lid = _parse_jid(key.get("remoteJid"))
            if phone is None and chat_lid is None:
                continue

            event = None
            if status in STATUS_READ:
                event = "read"
            elif status in STATUS_DELIVERED:
                event = "delivered"

            if not event:
                continue
            try:
                await self.db.record_event(vendor_id, phone, event, chat_lid=chat_lid)
            except Exception:
                logger.exception("Falha ao gravar messages-update event=%s", event)

        return web.json_response({"ok": True})

    async def _handle_connection_update(self, request: web.Request) -> web.Response:
        payload = await self._read_payload(request)
        instance = payload.get("instance")
        data = payload.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        state = ""
        if isinstance(data, dict):
            state = data.get("state") or data.get("connectionStatus") or ""

        # Sinal de shadowban / drop: bump no tracker.
        if state == "close" and self.suspicion is not None and instance:
            score = self.suspicion.bump(str(instance), "connection_close_event")
            logger.warning(
                "Connection close em %s; suspicion=%d",
                instance, score,
            )
        else:
            logger.info(
                "Connection update instance=%s state=%s",
                instance, state,
            )
        return web.json_response({"ok": True})

    async def _handle_send_message(self, request: web.Request) -> web.Response:
        # Confirmacao da Evolution de que aceitou o envio. Sem acao no momento.
        return web.json_response({"ok": True})

    async def _handle_unknown(self, request: web.Request) -> web.Response:
        logger.debug("Webhook 404 path=%s method=%s", request.path, request.method)
        return web.Response(status=404, text="not found")
