"""Smoke test do pre-flight de disparo."""

import asyncio
import tempfile
from pathlib import Path

from app.db import Database
from app.telegram_bot import _build_preflight, _whatsapp_result_exists, _whatsapp_result_phone


def _callback_data(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "bot.sqlite3")
        await db.setup()
        await db.ensure_vendor(1, "vendora", "vendor_1")

        campaign_id = await db.create_campaign(1, "confianca")
        await db.add_contacts(
            campaign_id,
            [
                {"row_index": 0, "name": "Quente", "phone": "5511000000001"},
                {"row_index": 1, "name": "Frio", "phone": "5511000000002"},
            ],
        )
        await db.set_caption(campaign_id, "Teste")

        await db.record_send_attempt(1, "5511000000001")
        await db.record_event(1, "5511000000001", "read")
        await db.record_send_attempt(1, "5511000000002")
        await db.record_send_attempt(1, "5511000000002")

        text, markup = await _build_preflight(db, campaign_id, 1)
        callbacks = _callback_data(markup)
        assert "Bloqueado: exclua os frios antes de disparar." in text, text
        assert f"disparar_go:{campaign_id}" not in callbacks, callbacks
        assert f"disparar_drop_cold:{campaign_id}" in callbacks, callbacks
        print("preflight_blocks_cold.ok")

    assert _whatsapp_result_exists({"exists": True}) is True
    assert _whatsapp_result_exists({"isWhatsApp": True}) is True
    assert _whatsapp_result_exists({"jid": "5511999998888@s.whatsapp.net"}) is True
    assert _whatsapp_result_phone({"number": "+55 11 99999-8888"}) == "5511999998888"
    assert _whatsapp_result_phone({"jid": "5511999998888@s.whatsapp.net"}) == "5511999998888"
    print("whatsapp_result_parse.ok")
    print("smoke_preflight.ok")


if __name__ == "__main__":
    asyncio.run(main())
