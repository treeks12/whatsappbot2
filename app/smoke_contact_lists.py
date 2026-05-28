import asyncio
import tempfile
from pathlib import Path

from .csv_utils import parse_contacts_csv_text
from .db import Database


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "bot.sqlite3")
        await db.setup()
        await db.ensure_vendor(1, "smoke", "vendor_1")

        contacts = parse_contacts_csv_text(
            "nome,telefone\n"
            "Ana,11999999999\n"
            "Ana Duplicada,+55 11 99999-9999\n"
            "Bia,21988887777\n"
        )
        list_id = await db.create_contact_list(1, "Clientes")
        imported = await db.import_contacts_to_list(list_id, 1, contacts)
        assert imported["added"] == 2, imported
        assert imported["duplicates"] == 0, imported
        duplicate = await db.import_contacts_to_list(
            list_id,
            1,
            [{"row_index": 0, "name": "Ana Duplicada", "phone": "5511999999999"}],
        )
        assert duplicate["added"] == 0, duplicate
        assert duplicate["duplicates"] == 1, duplicate
        print(f"import.ok added={imported['added']} incremental_duplicates={duplicate['duplicates']}")

        campaign_id = await db.create_campaign(1, "confianca_100")
        result = await db.copy_contact_list_to_campaign(list_id, 1, campaign_id, 0)
        assert result["total"] == 2, result
        assert result["blacklisted"] == 0, result
        print(f"campaign_copy.ok total={result['total']}")

        snapshot_id = await db.create_contact_list_snapshot(list_id, 1, "antes de reduzir")
        removed = await db.remove_contacts_from_list(list_id, 1, ["5511999999999"])
        assert removed == 1, removed
        assert await db.contact_list_count(list_id) == 1
        print(f"reduce.ok removed={removed}")

        restored = await db.restore_contact_list_snapshot(snapshot_id, 1)
        assert restored == 2, restored
        assert await db.contact_list_count(list_id) == 2
        print(f"restore.ok total={restored}")

        await db.prune_contact_list_snapshots(1, 14)
        snapshots = await db.list_contact_list_snapshots(list_id, 1)
        assert len(snapshots) == 1, len(snapshots)
        print(f"prune.ok snapshots={len(snapshots)}")


if __name__ == "__main__":
    asyncio.run(main())
