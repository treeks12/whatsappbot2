"""Smoke test da blacklist global.

Cobre:
- add idempotente, remove, is_phone_blacklisted
- filter_blacklisted (batch)
- filtro durante import_contacts_to_list (lista)
- filtro durante add_contacts (campanha por importacao)
- filtro durante copy_contact_list_to_campaign (lista -> campanha)
- filtro durante next_pending_contact (rede final)
- purge automatico da fila quando se adiciona durante campanha em andamento
- last_processed_contact_for_campaign (suporta o botao "Adicionar ultimo a blacklist")
"""

import asyncio
import tempfile
from pathlib import Path

from app.db import Database


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "bot.sqlite3")
        await db.setup()
        await db.ensure_vendor(1, "vendora", "vendor_1")

        # add idempotente
        first = await db.add_to_blacklist(
            "5511988887777",
            reason_code="manual_request",
            source="manual",
            added_by_user_id=1,
            added_by_vendor_id=1,
        )
        assert first["added"] is True, first
        assert first["removed_pending"] == 0, first

        repeat = await db.add_to_blacklist(
            "5511988887777",
            reason_code="manual_other",
            source="manual",
            added_by_user_id=1,
        )
        assert repeat["added"] is False, repeat
        print("add_idempotent.ok")

        assert await db.is_phone_blacklisted("5511988887777") is True
        assert await db.is_phone_blacklisted("5511966666666") is False

        # filter_blacklisted batch
        hits = await db.filter_blacklisted(["5511988887777", "5511966666666", "5511955554444"])
        assert hits == {"5511988887777"}, hits
        print("filter_batch.ok")

        # filtro em lista (import_contacts_to_list)
        list_id = await db.create_contact_list(1, "Clientes")
        result = await db.import_contacts_to_list(
            list_id,
            1,
            [
                {"row_index": 0, "name": "Bloqueado", "phone": "5511988887777"},
                {"row_index": 1, "name": "Cliente A", "phone": "5511955554444"},
                {"row_index": 2, "name": "Cliente B", "phone": "5521977776666"},
            ],
        )
        assert result["added"] == 2, result
        assert result["blacklisted"] == 1, result
        assert result["total"] == 2, result
        print(f"list_import_filters_blacklist.ok added={result['added']} blocked={result['blacklisted']}")

        # filtro em add_contacts (importacao direta para campanha)
        campaign_id = await db.create_campaign(1, "precaucao_100")
        add_result = await db.add_contacts(
            campaign_id,
            [
                {"row_index": 0, "name": "Bloqueado", "phone": "5511988887777"},
                {"row_index": 1, "name": "Cliente C", "phone": "5531977778888"},
            ],
        )
        assert add_result["blacklisted"] == 1, add_result
        assert add_result["total"] == 1, add_result
        print(f"campaign_import_filters_blacklist.ok total={add_result['total']} blocked={add_result['blacklisted']}")

        # filtro em copy_contact_list_to_campaign
        camp2 = await db.create_campaign(1, "precaucao_100")
        copy_result = await db.copy_contact_list_to_campaign(list_id, 1, camp2, 0)
        assert copy_result["total"] == 2, copy_result
        # blacklisted=0 porque ja foi filtrado na importacao da lista
        assert copy_result["blacklisted"] == 0, copy_result
        print(f"copy_list_to_campaign.ok total={copy_result['total']}")

        # rede final no next_pending_contact
        # injetando direto (simulando situacao em que blacklist veio depois)
        # cria nova campanha, importa varios, depois adiciona um a blacklist e deveria nao retornar
        camp3 = await db.create_campaign(1, "precaucao_100")
        await db.add_contacts(
            camp3,
            [
                {"row_index": 0, "name": "Vai sair", "phone": "5511944443333"},
                {"row_index": 1, "name": "Permanece", "phone": "5511933332222"},
            ],
        )
        # antes do bloqueio, o primeiro pendente eh o "5511944443333"
        first_pending = await db.next_pending_contact(camp3)
        assert first_pending["phone"] == "5511944443333", first_pending["phone"]

        # Marca como running para testar purge
        await db.start_campaign(camp3)

        bl_now = await db.add_to_blacklist(
            "5511944443333",
            reason_code="wrong_person",
            source="manual",
            added_by_user_id=1,
            added_by_vendor_id=1,
        )
        assert bl_now["added"] is True
        assert bl_now["removed_pending"] >= 1, bl_now
        print(f"purge_running_queue.ok removed={bl_now['removed_pending']}")

        # apos purge, next_pending_contact deve retornar so o "Permanece"
        next_after = await db.next_pending_contact(camp3)
        assert next_after is not None
        assert next_after["phone"] == "5511933332222", next_after["phone"]
        print("next_pending_skips_blacklist.ok")

        # remove e checa idempotente do remove
        removed = await db.remove_from_blacklist("5511988887777")
        assert removed is True
        again = await db.remove_from_blacklist("5511988887777")
        assert again is False
        print("remove_idempotent.ok")

        # last_processed_contact_for_campaign — sem nenhum processado ainda, retorna None
        last = await db.last_processed_contact_for_campaign(camp3)
        assert last is None
        # marca o primeiro pendente como sent
        contact = await db.next_pending_contact(camp3)
        assert contact is not None
        await db.mark_contact_sent(camp3, contact["id"], contact["phone"])
        last = await db.last_processed_contact_for_campaign(camp3)
        assert last is not None and last["phone"] == contact["phone"], last
        print(f"last_processed.ok phone={last['phone']}")

        # list_blacklist
        page = await db.list_blacklist(offset=0, limit=20)
        assert page["total"] >= 1, page
        print(f"list.ok total={page['total']} rows={len(page['rows'])}")

        print("smoke_blacklist.ok")


if __name__ == "__main__":
    asyncio.run(main())
