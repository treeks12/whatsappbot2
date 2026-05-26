import logging
import shutil
from pathlib import Path

from .db import Database


logger = logging.getLogger(__name__)


def remove_campaign_dir(campaigns_dir: Path, campaign_id: int):
    base = campaigns_dir.resolve()
    target = (base / str(campaign_id)).resolve()

    if not target.exists():
        return

    if base == target or base not in target.parents:
        raise ValueError(f"Diretorio de campanha invalido para limpeza: {target}")

    shutil.rmtree(target)
    logger.info("Arquivos da campanha %s removidos de %s", campaign_id, target)


async def cleanup_campaign_payload(db: Database, campaigns_dir: Path, campaign_id: int, enabled: bool):
    if not enabled:
        return

    remove_campaign_dir(campaigns_dir, campaign_id)
    await db.delete_media_for_campaign(campaign_id)


async def cleanup_orphan_campaign_dirs(db: Database, campaigns_dir: Path, enabled: bool):
    if not enabled or not campaigns_dir.exists():
        return

    existing_ids = await db.existing_campaign_ids()
    for child in campaigns_dir.iterdir():
        if not child.is_dir() or not child.name.isdigit():
            continue

        campaign_id = int(child.name)
        if campaign_id not in existing_ids:
            remove_campaign_dir(campaigns_dir, campaign_id)


def cleanup_tmp_import_dir(campaigns_dir: Path):
    tmp_dir = (campaigns_dir / "_tmp").resolve()
    base = campaigns_dir.resolve()
    if not tmp_dir.exists():
        return
    if base == tmp_dir or base not in tmp_dir.parents:
        raise ValueError(f"Diretorio temporario invalido para limpeza: {tmp_dir}")
    shutil.rmtree(tmp_dir)
