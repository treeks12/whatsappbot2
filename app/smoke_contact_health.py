"""Smoke test do contact_health (Fase 2)."""

import asyncio
import sqlite3
import tempfile
from pathlib import Path

from app.db import Database


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "bot.sqlite3"
        db = Database(db_path)
        await db.setup()
        await db.ensure_vendor(1, "vendora", "vendor_1")

        # Record send attempt cria a linha + streak=1
        await db.record_send_attempt(1, "5511999998888")
        row = await db.get_contact_health(1, "5511999998888")
        assert row is not None
        assert row["consecutive_no_delivery"] == 1, dict(row)
        assert row["last_sent_at"] is not None
        print("send_creates_row.ok")

        # Outro send incrementa o streak
        await db.record_send_attempt(1, "5511999998888")
        row = await db.get_contact_health(1, "5511999998888")
        assert row["consecutive_no_delivery"] == 2, dict(row)
        print("send_increments_streak.ok")

        # delivered reseta streak
        await db.record_event(1, "5511999998888", "delivered")
        row = await db.get_contact_health(1, "5511999998888")
        assert row["consecutive_no_delivery"] == 0, dict(row)
        assert row["last_delivered_at"] is not None
        print("delivered_resets_streak.ok")

        # read marca leitura sem mexer no streak
        await db.record_event(1, "5511999998888", "read")
        row = await db.get_contact_health(1, "5511999998888")
        assert row["last_read_at"] is not None
        print("read_marks.ok")

        # replied registra texto e mantem streak zerado
        await db.record_event(1, "5511999998888", "replied", text="oi tudo bem?")
        row = await db.get_contact_health(1, "5511999998888")
        assert row["last_replied_at"] is not None
        assert row["last_reply_text"] == "oi tudo bem?", dict(row)
        print("replied_stores_text.ok")

        # link_chat_lid backfilla a coluna
        await db.link_chat_lid(1, "5511999998888", "999999999999@lid")
        row = await db.get_contact_health(1, "5511999998888")
        assert row["chat_lid"] == "999999999999@lid", dict(row)
        print("link_chat_lid.ok")

        # @lid-only event resolve via chat_lid existente
        await db.record_event(1, None, "delivered", chat_lid="999999999999@lid")
        row = await db.get_contact_health(1, "5511999998888")
        assert row["last_delivered_at"] is not None
        print("lid_only_event_resolves.ok")

        # @lid-only sem mapeamento e ignorado silenciosamente
        await db.record_event(1, None, "delivered", chat_lid="123456789@lid")
        row_none = await db.get_contact_health(1, "123456789")
        assert row_none is None
        print("lid_only_no_mapping_ignored.ok")

        # Classificacao
        await db.ensure_vendor(2, "outra", "vendor_2")

        # hot: respondeu agora
        await db.record_send_attempt(2, "5511700000001")
        await db.record_event(2, "5511700000001", "replied", text="oi")

        # warm: entregou mas nunca respondeu
        await db.record_send_attempt(2, "5511700000002")
        await db.record_event(2, "5511700000002", "delivered")

        # cold: streak alto sem nenhuma entrega
        await db.record_send_attempt(2, "5511700000003")
        await db.record_send_attempt(2, "5511700000003")
        await db.record_send_attempt(2, "5511700000003")  # streak=3, sem delivered

        # unknown: nem chamamos record_send_attempt
        unknown_phone = "5511700000999"

        result = await db.classify_phones(2, [
            "5511700000001",
            "5511700000002",
            "5511700000003",
            unknown_phone,
        ])
        assert result["5511700000001"] == "hot", result
        assert result["5511700000002"] == "warm", result
        assert result["5511700000003"] == "cold", result
        assert result[unknown_phone] == "unknown", result
        print(f"classify.ok hot={1} warm={1} cold={1} unknown={1}")

        # Cold por idade da entrega: forca last_delivered_at antigo e nenhuma resposta
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                UPDATE contact_health
                SET last_delivered_at = datetime('now', '-200 days'),
                    last_replied_at = NULL,
                    last_read_at = NULL,
                    consecutive_no_delivery = 0
                WHERE vendor_id = 2 AND phone = '5511700000002'
                """
            )
            conn.commit()
        finally:
            conn.close()
        result = await db.classify_phones(2, ["5511700000002"])
        assert result["5511700000002"] == "cold", result
        print("classify_old_delivery_is_cold.ok")

        print("smoke_contact_health.ok")


if __name__ == "__main__":
    asyncio.run(main())
