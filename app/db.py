import asyncio
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS vendors (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    instance_name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    caption TEXT,
    profile_id TEXT NOT NULL,
    total_contacts INTEGER NOT NULL DEFAULT 0,
    sent_count INTEGER NOT NULL DEFAULT 0,
    current_index INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY(vendor_id) REFERENCES vendors(telegram_id)
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    row_index INTEGER NOT NULL,
    name TEXT,
    phone TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    UNIQUE(campaign_id, phone),
    FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
);

CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    file_name TEXT NOT NULL,
    FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
);

CREATE TABLE IF NOT EXISTS sent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    contact_id INTEGER NOT NULL,
    phone TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(campaign_id, phone),
    FOREIGN KEY(campaign_id) REFERENCES campaigns(id),
    FOREIGN KEY(contact_id) REFERENCES contacts(id)
    );

CREATE TABLE IF NOT EXISTS authorized_users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    name TEXT NOT NULL,
    approved INTEGER NOT NULL DEFAULT 0,
    approved_by INTEGER,
    requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS contact_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(vendor_id, name),
    FOREIGN KEY(vendor_id) REFERENCES vendors(telegram_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS contact_list_contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id INTEGER NOT NULL,
    name TEXT,
    phone TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(list_id, phone),
    FOREIGN KEY(list_id) REFERENCES contact_lists(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS contact_list_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id INTEGER NOT NULL,
    vendor_id INTEGER NOT NULL,
    list_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(list_id) REFERENCES contact_lists(id) ON DELETE CASCADE,
    FOREIGN KEY(vendor_id) REFERENCES vendors(telegram_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS contact_list_snapshot_contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    name TEXT,
    phone TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    FOREIGN KEY(snapshot_id) REFERENCES contact_list_snapshots(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_contact_lists_vendor ON contact_lists(vendor_id);
CREATE INDEX IF NOT EXISTS idx_contact_list_contacts_list ON contact_list_contacts(list_id);
CREATE INDEX IF NOT EXISTS idx_contact_list_snapshots_list ON contact_list_snapshots(list_id, created_at);

CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL UNIQUE,
    reason_code TEXT NOT NULL,
    reason_note TEXT,
    source TEXT NOT NULL,
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    added_by_user_id INTEGER,
    added_by_vendor_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_blacklist_phone ON blacklist(phone);
CREATE INDEX IF NOT EXISTS idx_blacklist_added_at ON blacklist(added_at);

CREATE TABLE IF NOT EXISTS contact_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_id INTEGER NOT NULL,
    phone TEXT NOT NULL,
    chat_lid TEXT,
    last_sent_at TEXT,
    last_delivered_at TEXT,
    last_read_at TEXT,
    last_replied_at TEXT,
    last_reply_text TEXT,
    consecutive_no_delivery INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(vendor_id, phone),
    FOREIGN KEY(vendor_id) REFERENCES vendors(telegram_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_contact_health_phone ON contact_health(phone);
CREATE INDEX IF NOT EXISTS idx_contact_health_chat_lid ON contact_health(chat_lid);
CREATE INDEX IF NOT EXISTS idx_contact_health_vendor ON contact_health(vendor_id);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    async def setup(self):
        async with self._lock:
            with self.connect() as conn:
                conn.executescript(SCHEMA)
                self._cleanup_orphan_records(conn)
                self._refresh_campaign_counters(conn)

    async def ensure_vendor(self, telegram_id: int, username: str, instance_name: str):
        async with self._lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO vendors (telegram_id, username, instance_name)
                    VALUES (?, ?, ?)
                    ON CONFLICT(telegram_id) DO UPDATE SET
                        username = excluded.username,
                        instance_name = excluded.instance_name
                    """,
                    (telegram_id, username, instance_name),
                )

    async def get_vendor(self, telegram_id: int) -> Optional[sqlite3.Row]:
        async with self._lock:
            with self.connect() as conn:
                return conn.execute("SELECT * FROM vendors WHERE telegram_id = ?", (telegram_id,)).fetchone()

    async def create_campaign(self, vendor_id: int, profile_id: str) -> int:
        async with self._lock:
            with self.connect() as conn:
                cur = conn.execute(
                    "INSERT INTO campaigns (vendor_id, status, profile_id) VALUES (?, 'draft', ?)",
                    (vendor_id, profile_id),
                )
                return int(cur.lastrowid)

    async def get_campaign(self, campaign_id: int) -> Optional[sqlite3.Row]:
        async with self._lock:
            with self.connect() as conn:
                return conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()

    async def get_campaign_with_vendor(self, campaign_id: int) -> Optional[sqlite3.Row]:
        async with self._lock:
            with self.connect() as conn:
                return conn.execute(
                    """
                    SELECT campaigns.*, vendors.instance_name, vendors.username
                    FROM campaigns
                    JOIN vendors ON vendors.telegram_id = campaigns.vendor_id
                    WHERE campaigns.id = ?
                    """,
                    (campaign_id,),
                ).fetchone()

    async def get_active_campaign_for_vendor(self, vendor_id: int) -> Optional[sqlite3.Row]:
        async with self._lock:
            with self.connect() as conn:
                return conn.execute(
                    """
                    SELECT * FROM campaigns
                    WHERE vendor_id = ? AND status IN ('draft', 'ready', 'running', 'paused')
                    ORDER BY id DESC LIMIT 1
                    """,
                    (vendor_id,),
                ).fetchone()

    async def set_campaign_status(self, campaign_id: int, status: str):
        async with self._lock:
            with self.connect() as conn:
                conn.execute("UPDATE campaigns SET status = ? WHERE id = ?", (status, campaign_id))

    async def start_campaign(self, campaign_id: int):
        async with self._lock:
            with self.connect() as conn:
                conn.execute(
                    "UPDATE campaigns SET status = 'running', started_at = COALESCE(started_at, CURRENT_TIMESTAMP) WHERE id = ?",
                    (campaign_id,),
                )

    async def finish_campaign(self, campaign_id: int, status: str = "completed"):
        async with self._lock:
            with self.connect() as conn:
                conn.execute(
                    "UPDATE campaigns SET status = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (status, campaign_id),
                )

    async def recover_interrupted_campaigns(self) -> list[int]:
        async with self._lock:
            with self.connect() as conn:
                rows = conn.execute(
                    "SELECT id FROM campaigns WHERE status IN ('running', 'paused')"
                ).fetchall()
                ids = [int(row["id"]) for row in rows]
                if ids:
                    conn.execute(
                        """
                        UPDATE campaigns
                        SET status = 'failed', finished_at = CURRENT_TIMESTAMP
                        WHERE status IN ('running', 'paused')
                        """
                    )
                return ids

    async def request_access(self, telegram_id: int, username: str, name: str):
        async with self._lock:
            with self.connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO authorized_users (telegram_id, username, name, approved) VALUES (?, ?, ?, 0)",
                    (telegram_id, username, name),
                )

    async def is_user_approved(self, telegram_id: int) -> bool:
        async with self._lock:
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT approved FROM authorized_users WHERE telegram_id = ?",
                    (telegram_id,),
                ).fetchone()
                return row is not None and row["approved"] == 1

    async def get_pending_request(self, telegram_id: int):
        async with self._lock:
            with self.connect() as conn:
                return conn.execute(
                    "SELECT * FROM authorized_users WHERE telegram_id = ? AND approved = 0",
                    (telegram_id,),
                ).fetchone()

    async def approve_user(self, telegram_id: int, admin_id: int):
        async with self._lock:
            with self.connect() as conn:
                conn.execute(
                    "UPDATE authorized_users SET approved = 1, approved_by = ?, approved_at = CURRENT_TIMESTAMP WHERE telegram_id = ?",
                    (admin_id, telegram_id),
                )

    async def reject_user(self, telegram_id: int):
        async with self._lock:
            with self.connect() as conn:
                conn.execute(
                    "DELETE FROM authorized_users WHERE telegram_id = ? AND approved = 0",
                    (telegram_id,),
                )

    async def set_caption(self, campaign_id: int, caption: str):
        async with self._lock:
            with self.connect() as conn:
                conn.execute("UPDATE campaigns SET caption = ?, status = 'ready' WHERE id = ?", (caption, campaign_id))

    async def add_contacts(self, campaign_id: int, contacts: Iterable[dict]) -> dict:
        """Insere contatos na campanha, ignorando os que estao em blacklist.

        Retorna dict com `total` (contatos finais na campanha) e `blacklisted`
        (quantos foram filtrados pela blacklist).
        """
        materialized = list(contacts)
        async with self._lock:
            with self.connect() as conn:
                phones = {item["phone"] for item in materialized if item.get("phone")}
                blacklisted = self._collect_blacklist_hits(conn, phones)
                accepted = [item for item in materialized if item.get("phone") not in blacklisted]
                rows = [
                    (campaign_id, item["row_index"], item.get("name"), item["phone"])
                    for item in accepted
                ]
                conn.executemany(
                    "INSERT OR IGNORE INTO contacts (campaign_id, row_index, name, phone) VALUES (?, ?, ?, ?)",
                    rows,
                )
                total = conn.execute(
                    "SELECT COUNT(*) FROM contacts WHERE campaign_id = ?",
                    (campaign_id,),
                ).fetchone()[0]
                conn.execute("UPDATE campaigns SET total_contacts = ? WHERE id = ?", (total, campaign_id))
                return {"total": int(total), "blacklisted": len(blacklisted)}

    async def add_media(self, campaign_id: int, path: str, mime_type: str, file_name: str):
        async with self._lock:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO media (campaign_id, path, mime_type, file_name) VALUES (?, ?, ?, ?)",
                    (campaign_id, path, mime_type, file_name),
                )

    async def delete_media_for_campaign(self, campaign_id: int):
        async with self._lock:
            with self.connect() as conn:
                conn.execute("DELETE FROM media WHERE campaign_id = ?", (campaign_id,))

    async def existing_campaign_ids(self) -> set[int]:
        async with self._lock:
            with self.connect() as conn:
                rows = conn.execute("SELECT id FROM campaigns").fetchall()
                return {int(row["id"]) for row in rows}

    async def terminal_campaign_ids(self) -> list[int]:
        async with self._lock:
            with self.connect() as conn:
                rows = conn.execute(
                    "SELECT id FROM campaigns WHERE status IN ('completed', 'cancelled', 'failed')"
                ).fetchall()
                return [int(row["id"]) for row in rows]

    async def count_media(self, campaign_id: int) -> int:
        async with self._lock:
            with self.connect() as conn:
                return int(conn.execute("SELECT COUNT(*) FROM media WHERE campaign_id = ?", (campaign_id,)).fetchone()[0])

    async def get_media(self, campaign_id: int):
        async with self._lock:
            with self.connect() as conn:
                return conn.execute("SELECT * FROM media WHERE campaign_id = ? ORDER BY id", (campaign_id,)).fetchall()

    async def next_pending_contact(self, campaign_id: int) -> Optional[sqlite3.Row]:
        async with self._lock:
            with self.connect() as conn:
                return conn.execute(
                    """
                    SELECT *
                    FROM contacts
                    WHERE campaign_id = ?
                      AND status = 'pending'
                      AND phone NOT IN (SELECT phone FROM blacklist)
                    ORDER BY row_index LIMIT 1
                    """,
                    (campaign_id,),
                ).fetchone()

    async def pending_phones_for_campaign(self, campaign_id: int) -> list[str]:
        async with self._lock:
            with self.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT phone FROM contacts
                    WHERE campaign_id = ? AND status = 'pending'
                    """,
                    (campaign_id,),
                ).fetchall()
                return [row["phone"] for row in rows if row["phone"]]

    async def fail_pending_phones(self, campaign_id: int, phones: Iterable[str], reason: str) -> int:
        unique = sorted({p for p in (phones or []) if p})
        if not unique:
            return 0
        async with self._lock:
            with self.connect() as conn:
                affected = 0
                for chunk_start in range(0, len(unique), 500):
                    chunk = unique[chunk_start : chunk_start + 500]
                    placeholders = ",".join("?" * len(chunk))
                    cur = conn.execute(
                        f"""
                        UPDATE contacts
                        SET status = 'failed', error = ?
                        WHERE campaign_id = ?
                          AND status = 'pending'
                          AND phone IN ({placeholders})
                        """,
                        (reason[:200], campaign_id, *chunk),
                    )
                    affected += cur.rowcount
                if affected:
                    self._refresh_campaign_counter(conn, campaign_id)
                return int(affected)

    async def mark_contact_sent(self, campaign_id: int, contact_id: int, phone: str):
        async with self._lock:
            with self.connect() as conn:
                conn.execute("UPDATE contacts SET status = 'sent', error = NULL WHERE id = ?", (contact_id,))
                conn.execute(
                    "INSERT OR IGNORE INTO sent_messages (campaign_id, contact_id, phone) VALUES (?, ?, ?)",
                    (campaign_id, contact_id, phone),
                )
                self._refresh_campaign_counter(conn, campaign_id)

    async def mark_contact_failed(self, campaign_id: int, contact_id: int, error: str):
        async with self._lock:
            with self.connect() as conn:
                conn.execute("UPDATE contacts SET status = 'failed', error = ? WHERE id = ?", (error[:500], contact_id))
                self._refresh_campaign_counter(conn, campaign_id)

    async def phone_has_previous_success(self, phone: str, current_campaign_id: int) -> bool:
        async with self._lock:
            with self.connect() as conn:
                row = conn.execute(
                    """
                    SELECT 1
                    FROM sent_messages
                    WHERE phone = ? AND campaign_id != ?
                    LIMIT 1
                    """,
                    (phone, current_campaign_id),
                ).fetchone()
                return row is not None

    async def campaign_progress(self, campaign_id: int) -> dict:
        async with self._lock:
            with self.connect() as conn:
                row = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent,
                        SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                        SUM(CASE WHEN status IN ('sent', 'failed') THEN 1 ELSE 0 END) AS processed
                    FROM contacts
                    WHERE campaign_id = ?
                    """,
                    (campaign_id,),
                ).fetchone()
                return {
                    "total": int(row["total"] or 0),
                    "sent": int(row["sent"] or 0),
                    "failed": int(row["failed"] or 0),
                    "processed": int(row["processed"] or 0),
                }

    async def active_running_campaigns(self):
        async with self._lock:
            with self.connect() as conn:
                return conn.execute("SELECT * FROM campaigns WHERE status = 'running'").fetchall()

    async def count_running_or_paused_campaigns(self) -> int:
        async with self._lock:
            with self.connect() as conn:
                return int(
                    conn.execute(
                        "SELECT COUNT(*) FROM campaigns WHERE status IN ('running', 'paused')"
                    ).fetchone()[0]
                )

    async def campaign_summary_for_vendor(self, vendor_id: int):
        async with self._lock:
            with self.connect() as conn:
                return conn.execute(
                    """
                    SELECT
                        campaigns.id,
                        campaigns.status,
                        campaigns.total_contacts,
                        campaigns.sent_count,
                        campaigns.created_at,
                        SUM(CASE WHEN contacts.status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                        SUM(CASE WHEN contacts.status IN ('sent', 'failed') THEN 1 ELSE 0 END) AS processed_count
                    FROM campaigns
                    LEFT JOIN contacts ON contacts.campaign_id = campaigns.id
                    WHERE vendor_id = ?
                    GROUP BY campaigns.id
                    ORDER BY campaigns.id DESC LIMIT 5
                    """,
                    (vendor_id,),
                ).fetchall()

    async def create_contact_list(self, vendor_id: int, name: str) -> int:
        clean_name = name.strip()[:80]
        if not clean_name:
            raise ValueError("Nome da lista vazio.")
        async with self._lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO contact_lists (vendor_id, name)
                    VALUES (?, ?)
                    ON CONFLICT(vendor_id, name) DO UPDATE SET updated_at = CURRENT_TIMESTAMP
                    """,
                    (vendor_id, clean_name),
                )
                row = conn.execute(
                    "SELECT id FROM contact_lists WHERE vendor_id = ? AND name = ?",
                    (vendor_id, clean_name),
                ).fetchone()
                return int(row["id"])

    async def list_contact_lists(self, vendor_id: int):
        async with self._lock:
            with self.connect() as conn:
                return conn.execute(
                    """
                    SELECT
                        contact_lists.*,
                        COUNT(contact_list_contacts.id) AS total_contacts
                    FROM contact_lists
                    LEFT JOIN contact_list_contacts ON contact_list_contacts.list_id = contact_lists.id
                    WHERE contact_lists.vendor_id = ?
                    GROUP BY contact_lists.id
                    ORDER BY contact_lists.updated_at DESC, contact_lists.id DESC
                    """,
                    (vendor_id,),
                ).fetchall()

    async def get_contact_list(self, list_id: int, vendor_id: Optional[int] = None):
        async with self._lock:
            with self.connect() as conn:
                if vendor_id is None:
                    return conn.execute("SELECT * FROM contact_lists WHERE id = ?", (list_id,)).fetchone()
                return conn.execute(
                    "SELECT * FROM contact_lists WHERE id = ? AND vendor_id = ?",
                    (list_id, vendor_id),
                ).fetchone()

    async def contact_list_count(self, list_id: int) -> int:
        async with self._lock:
            with self.connect() as conn:
                return int(
                    conn.execute(
                        "SELECT COUNT(*) FROM contact_list_contacts WHERE list_id = ?",
                        (list_id,),
                    ).fetchone()[0]
                )

    async def rename_contact_list(self, list_id: int, vendor_id: int, name: str) -> bool:
        clean_name = name.strip()[:80]
        if not clean_name:
            raise ValueError("Nome da lista vazio.")
        async with self._lock:
            with self.connect() as conn:
                cur = conn.execute(
                    """
                    UPDATE contact_lists
                    SET name = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND vendor_id = ?
                    """,
                    (clean_name, list_id, vendor_id),
                )
                return cur.rowcount > 0

    async def delete_contact_list(self, list_id: int, vendor_id: int) -> bool:
        async with self._lock:
            with self.connect() as conn:
                cur = conn.execute("DELETE FROM contact_lists WHERE id = ? AND vendor_id = ?", (list_id, vendor_id))
                return cur.rowcount > 0

    async def import_contacts_to_list(self, list_id: int, vendor_id: int, contacts: Iterable[dict]) -> dict:
        async with self._lock:
            with self.connect() as conn:
                owner = conn.execute(
                    "SELECT id FROM contact_lists WHERE id = ? AND vendor_id = ?",
                    (list_id, vendor_id),
                ).fetchone()
                if not owner:
                    raise ValueError("Lista nao encontrada.")

                materialized = list(contacts)
                phones_seen = {item["phone"] for item in materialized if item.get("phone")}
                blacklisted = self._collect_blacklist_hits(conn, phones_seen)

                added = 0
                duplicates = 0
                updated = 0
                blacklisted_skipped = 0
                seen = set()
                for item in materialized:
                    phone = item.get("phone")
                    if not phone or phone in seen:
                        if phone:
                            duplicates += 1
                        continue
                    seen.add(phone)
                    if phone in blacklisted:
                        blacklisted_skipped += 1
                        continue
                    name = (item.get("name") or "Cliente").strip()[:120]
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO contact_list_contacts (list_id, name, phone) VALUES (?, ?, ?)",
                        (list_id, name, phone),
                    )
                    if cur.rowcount:
                        added += 1
                        continue

                    row = conn.execute(
                        "SELECT id, name FROM contact_list_contacts WHERE list_id = ? AND phone = ?",
                        (list_id, phone),
                    ).fetchone()
                    duplicates += 1
                    if row and _generic_name(row["name"]) and not _generic_name(name):
                        conn.execute(
                            """
                            UPDATE contact_list_contacts
                            SET name = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                            """,
                            (name, row["id"]),
                        )
                        updated += 1

                conn.execute(
                    "UPDATE contact_lists SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (list_id,),
                )
                total = conn.execute(
                    "SELECT COUNT(*) FROM contact_list_contacts WHERE list_id = ?",
                    (list_id,),
                ).fetchone()[0]
                return {
                    "added": added,
                    "duplicates": duplicates,
                    "updated": updated,
                    "blacklisted": blacklisted_skipped,
                    "total": int(total),
                }

    async def contact_list_contacts(self, list_id: int, vendor_id: int, limit: int = 0):
        async with self._lock:
            with self.connect() as conn:
                owner = conn.execute(
                    "SELECT id FROM contact_lists WHERE id = ? AND vendor_id = ?",
                    (list_id, vendor_id),
                ).fetchone()
                if not owner:
                    return []
                sql = """
                    SELECT name, phone
                    FROM contact_list_contacts
                    WHERE list_id = ?
                    ORDER BY id
                """
                params: tuple = (list_id,)
                if limit > 0:
                    sql += " LIMIT ?"
                    params = (list_id, limit)
                return conn.execute(sql, params).fetchall()

    async def copy_contact_list_to_campaign(self, list_id: int, vendor_id: int, campaign_id: int, limit: int = 0) -> dict:
        async with self._lock:
            with self.connect() as conn:
                owner = conn.execute(
                    "SELECT id FROM contact_lists WHERE id = ? AND vendor_id = ?",
                    (list_id, vendor_id),
                ).fetchone()
                campaign = conn.execute(
                    "SELECT id FROM campaigns WHERE id = ? AND vendor_id = ?",
                    (campaign_id, vendor_id),
                ).fetchone()
                if not owner or not campaign:
                    raise ValueError("Lista ou campanha nao encontrada.")

                rows = conn.execute(
                    """
                    SELECT name, phone
                    FROM contact_list_contacts
                    WHERE list_id = ?
                    ORDER BY id
                    """ + (" LIMIT ?" if limit > 0 else ""),
                    (list_id, limit) if limit > 0 else (list_id,),
                ).fetchall()

                phones = {row["phone"] for row in rows if row["phone"]}
                blacklisted = self._collect_blacklist_hits(conn, phones)
                accepted = [(row["name"], row["phone"]) for row in rows if row["phone"] not in blacklisted]
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO contacts (campaign_id, row_index, name, phone)
                    VALUES (?, ?, ?, ?)
                    """,
                    [(campaign_id, index, name, phone) for index, (name, phone) in enumerate(accepted)],
                )
                total = conn.execute("SELECT COUNT(*) FROM contacts WHERE campaign_id = ?", (campaign_id,)).fetchone()[0]
                conn.execute("UPDATE campaigns SET total_contacts = ? WHERE id = ?", (total, campaign_id))
                return {"total": int(total), "blacklisted": len(blacklisted)}

    async def create_contact_list_snapshot(self, list_id: int, vendor_id: int, reason: str) -> int:
        async with self._lock:
            with self.connect() as conn:
                contact_list = conn.execute(
                    "SELECT * FROM contact_lists WHERE id = ? AND vendor_id = ?",
                    (list_id, vendor_id),
                ).fetchone()
                if not contact_list:
                    raise ValueError("Lista nao encontrada.")
                cur = conn.execute(
                    """
                    INSERT INTO contact_list_snapshots (list_id, vendor_id, list_name, reason)
                    VALUES (?, ?, ?, ?)
                    """,
                    (list_id, vendor_id, contact_list["name"], reason[:120]),
                )
                snapshot_id = int(cur.lastrowid)
                rows = conn.execute(
                    "SELECT name, phone FROM contact_list_contacts WHERE list_id = ? ORDER BY id",
                    (list_id,),
                ).fetchall()
                conn.executemany(
                    """
                    INSERT INTO contact_list_snapshot_contacts (snapshot_id, name, phone, row_index)
                    VALUES (?, ?, ?, ?)
                    """,
                    [(snapshot_id, row["name"], row["phone"], index) for index, row in enumerate(rows)],
                )
                return snapshot_id

    async def list_contact_list_snapshots(self, list_id: int, vendor_id: int):
        async with self._lock:
            with self.connect() as conn:
                return conn.execute(
                    """
                    SELECT
                        contact_list_snapshots.*,
                        COUNT(contact_list_snapshot_contacts.id) AS total_contacts
                    FROM contact_list_snapshots
                    LEFT JOIN contact_list_snapshot_contacts
                        ON contact_list_snapshot_contacts.snapshot_id = contact_list_snapshots.id
                    WHERE contact_list_snapshots.list_id = ?
                      AND contact_list_snapshots.vendor_id = ?
                    GROUP BY contact_list_snapshots.id
                    ORDER BY contact_list_snapshots.created_at DESC, contact_list_snapshots.id DESC
                    """,
                    (list_id, vendor_id),
                ).fetchall()

    async def restore_contact_list_snapshot(self, snapshot_id: int, vendor_id: int) -> int:
        async with self._lock:
            with self.connect() as conn:
                snapshot = conn.execute(
                    """
                    SELECT * FROM contact_list_snapshots
                    WHERE id = ? AND vendor_id = ?
                    """,
                    (snapshot_id, vendor_id),
                ).fetchone()
                if not snapshot:
                    raise ValueError("Backup nao encontrado.")
                contact_list = conn.execute(
                    "SELECT id FROM contact_lists WHERE id = ? AND vendor_id = ?",
                    (snapshot["list_id"], vendor_id),
                ).fetchone()
                if not contact_list:
                    raise ValueError("Lista do backup nao existe mais.")

                current = conn.execute(
                    "SELECT name, phone FROM contact_list_contacts WHERE list_id = ? ORDER BY id",
                    (snapshot["list_id"],),
                ).fetchall()
                cur = conn.execute(
                    """
                    INSERT INTO contact_list_snapshots (list_id, vendor_id, list_name, reason)
                    VALUES (?, ?, ?, ?)
                    """,
                    (snapshot["list_id"], vendor_id, snapshot["list_name"], "antes de restaurar backup"),
                )
                current_snapshot_id = int(cur.lastrowid)
                conn.executemany(
                    """
                    INSERT INTO contact_list_snapshot_contacts (snapshot_id, name, phone, row_index)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (current_snapshot_id, row["name"], row["phone"], index)
                        for index, row in enumerate(current)
                    ],
                )

                conn.execute("DELETE FROM contact_list_contacts WHERE list_id = ?", (snapshot["list_id"],))
                rows = conn.execute(
                    """
                    SELECT name, phone
                    FROM contact_list_snapshot_contacts
                    WHERE snapshot_id = ?
                    ORDER BY row_index
                    """,
                    (snapshot_id,),
                ).fetchall()
                conn.executemany(
                    "INSERT INTO contact_list_contacts (list_id, name, phone) VALUES (?, ?, ?)",
                    [(snapshot["list_id"], row["name"], row["phone"]) for row in rows],
                )
                conn.execute(
                    "UPDATE contact_lists SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (snapshot["list_id"],),
                )
                return len(rows)

    async def remove_contacts_from_list(self, list_id: int, vendor_id: int, phones: Iterable[str]) -> int:
        unique_phones = sorted({phone for phone in phones if phone})
        if not unique_phones:
            return 0
        async with self._lock:
            with self.connect() as conn:
                owner = conn.execute(
                    "SELECT id FROM contact_lists WHERE id = ? AND vendor_id = ?",
                    (list_id, vendor_id),
                ).fetchone()
                if not owner:
                    raise ValueError("Lista nao encontrada.")
                removed = 0
                for phone in unique_phones:
                    cur = conn.execute(
                        "DELETE FROM contact_list_contacts WHERE list_id = ? AND phone = ?",
                        (list_id, phone),
                    )
                    removed += cur.rowcount
                conn.execute("UPDATE contact_lists SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (list_id,))
                return int(removed)

    async def prune_contact_list_snapshots(self, keep_per_list: int, max_age_days: int):
        async with self._lock:
            with self.connect() as conn:
                if max_age_days > 0:
                    conn.execute(
                        """
                        DELETE FROM contact_list_snapshots
                        WHERE created_at < datetime('now', ?)
                        """,
                        (f"-{max_age_days} days",),
                    )
                if keep_per_list > 0:
                    rows = conn.execute("SELECT id FROM contact_lists").fetchall()
                    for row in rows:
                        old = conn.execute(
                            """
                            SELECT id
                            FROM contact_list_snapshots
                            WHERE list_id = ?
                            ORDER BY created_at DESC, id DESC
                            LIMIT -1 OFFSET ?
                            """,
                            (row["id"], keep_per_list),
                        ).fetchall()
                        if old:
                            conn.executemany(
                                "DELETE FROM contact_list_snapshots WHERE id = ?",
                                [(item["id"],) for item in old],
                            )

    async def add_to_blacklist(
        self,
        phone: str,
        reason_code: str,
        reason_note: Optional[str] = None,
        source: str = "manual",
        added_by_user_id: Optional[int] = None,
        added_by_vendor_id: Optional[int] = None,
    ) -> dict:
        """Adiciona um telefone a blacklist global. Idempotente.

        Retorna dict com:
          - added: True se foi inserido agora, False se ja existia
          - phone: telefone normalizado
          - removed_pending: contagem de contatos removidos das filas de campanhas em andamento
        """
        clean = (phone or "").strip()
        if not clean:
            raise ValueError("Telefone vazio.")
        clean_note = (reason_note or "").strip()[:500] or None
        async with self._lock:
            with self.connect() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO blacklist
                        (phone, reason_code, reason_note, source, added_by_user_id, added_by_vendor_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (clean, reason_code, clean_note, source, added_by_user_id, added_by_vendor_id),
                )
                added = cur.rowcount > 0
                removed_pending = 0
                if added:
                    removed_pending = self._purge_phone_from_active_queues(conn, clean)
                return {"added": added, "phone": clean, "removed_pending": removed_pending}

    async def remove_from_blacklist(self, phone: str) -> bool:
        clean = (phone or "").strip()
        if not clean:
            return False
        async with self._lock:
            with self.connect() as conn:
                cur = conn.execute("DELETE FROM blacklist WHERE phone = ?", (clean,))
                return cur.rowcount > 0

    async def is_phone_blacklisted(self, phone: str) -> bool:
        clean = (phone or "").strip()
        if not clean:
            return False
        async with self._lock:
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM blacklist WHERE phone = ? LIMIT 1",
                    (clean,),
                ).fetchone()
                return row is not None

    async def get_blacklist_entry(self, phone: str) -> Optional[sqlite3.Row]:
        clean = (phone or "").strip()
        if not clean:
            return None
        async with self._lock:
            with self.connect() as conn:
                return conn.execute(
                    "SELECT * FROM blacklist WHERE phone = ?",
                    (clean,),
                ).fetchone()

    async def filter_blacklisted(self, phones: Iterable[str]) -> set[str]:
        """Devolve o subconjunto dos telefones que estao na blacklist."""
        unique = sorted({p for p in (phones or []) if p})
        if not unique:
            return set()
        async with self._lock:
            with self.connect() as conn:
                hits: set[str] = set()
                for chunk_start in range(0, len(unique), 500):
                    chunk = unique[chunk_start : chunk_start + 500]
                    placeholders = ",".join("?" * len(chunk))
                    rows = conn.execute(
                        f"SELECT phone FROM blacklist WHERE phone IN ({placeholders})",
                        chunk,
                    ).fetchall()
                    for row in rows:
                        hits.add(row["phone"])
                return hits

    async def list_blacklist(self, offset: int = 0, limit: int = 20) -> dict:
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 200))
        async with self._lock:
            with self.connect() as conn:
                total = int(conn.execute("SELECT COUNT(*) FROM blacklist").fetchone()[0])
                rows = conn.execute(
                    """
                    SELECT id, phone, reason_code, reason_note, source, added_at,
                           added_by_user_id, added_by_vendor_id
                    FROM blacklist
                    ORDER BY added_at DESC, id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
                return {"total": total, "rows": rows, "offset": offset, "limit": limit}

    async def last_processed_contact_for_campaign(self, campaign_id: int) -> Optional[sqlite3.Row]:
        """Retorna o ultimo contato que ja foi processado (sent/failed) na campanha."""
        async with self._lock:
            with self.connect() as conn:
                return conn.execute(
                    """
                    SELECT id, name, phone, status, error, row_index
                    FROM contacts
                    WHERE campaign_id = ? AND status IN ('sent', 'failed')
                    ORDER BY row_index DESC, id DESC
                    LIMIT 1
                    """,
                    (campaign_id,),
                ).fetchone()

    def _purge_phone_from_active_queues(self, conn: sqlite3.Connection, phone: str) -> int:
        """Remove o telefone das filas pendentes de campanhas em andamento.

        Retorna a quantidade de linhas afetadas. Atualiza tambem os contadores das
        campanhas tocadas para que `total_contacts` reflita a fila real.
        """
        affected_campaigns = conn.execute(
            """
            SELECT DISTINCT contacts.campaign_id
            FROM contacts
            JOIN campaigns ON campaigns.id = contacts.campaign_id
            WHERE contacts.phone = ?
              AND contacts.status = 'pending'
              AND campaigns.status IN ('draft', 'ready', 'running', 'paused')
            """,
            (phone,),
        ).fetchall()

        if not affected_campaigns:
            return 0

        cur = conn.execute(
            """
            DELETE FROM contacts
            WHERE phone = ?
              AND status = 'pending'
              AND campaign_id IN (
                  SELECT id FROM campaigns
                  WHERE status IN ('draft', 'ready', 'running', 'paused')
              )
            """,
            (phone,),
        )
        affected_rows = cur.rowcount

        for row in affected_campaigns:
            self._refresh_campaign_counter(conn, int(row["campaign_id"]))

        return int(affected_rows)

    # ------------------------------------------------------------------
    # contact_health
    # ------------------------------------------------------------------

    async def record_send_attempt(self, vendor_id: int, phone: str, chat_lid: Optional[str] = None):
        """Registra que se enviou para esse contato. Incrementa o streak de
        'sem entrega' (resetado por record_event('delivered'))."""
        clean = (phone or "").strip()
        if not clean:
            return
        async with self._lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO contact_health (vendor_id, phone, chat_lid, last_sent_at, consecutive_no_delivery, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(vendor_id, phone) DO UPDATE SET
                        last_sent_at = CURRENT_TIMESTAMP,
                        chat_lid = COALESCE(excluded.chat_lid, contact_health.chat_lid),
                        consecutive_no_delivery = contact_health.consecutive_no_delivery + 1,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (vendor_id, clean, chat_lid),
                )

    async def record_event(
        self,
        vendor_id: int,
        phone: Optional[str],
        event: str,
        chat_lid: Optional[str] = None,
        text: Optional[str] = None,
    ):
        """Marca um evento ('delivered'|'read'|'replied') para o contato.

        Se `phone` for None (so vem `chat_lid`), tenta resolver pelo chat_lid existente.
        Se nada for encontrado, ignora silenciosamente (caso @lid-only sem mapeamento).
        """
        if event not in ("delivered", "read", "replied"):
            return
        clean_phone = (phone or "").strip()
        clean_lid = (chat_lid or "").strip() or None

        async with self._lock:
            with self.connect() as conn:
                resolved_phone = clean_phone or self._resolve_phone_by_lid(conn, vendor_id, clean_lid)
                if not resolved_phone:
                    return

                column = {
                    "delivered": "last_delivered_at",
                    "read": "last_read_at",
                    "replied": "last_replied_at",
                }[event]

                # Garante a linha (caso o evento chegue antes de qualquer record_send_attempt).
                conn.execute(
                    """
                    INSERT INTO contact_health (vendor_id, phone, chat_lid, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(vendor_id, phone) DO NOTHING
                    """,
                    (vendor_id, resolved_phone, clean_lid),
                )

                if event == "delivered":
                    conn.execute(
                        f"""
                        UPDATE contact_health
                        SET {column} = CURRENT_TIMESTAMP,
                            chat_lid = COALESCE(?, chat_lid),
                            consecutive_no_delivery = 0,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE vendor_id = ? AND phone = ?
                        """,
                        (clean_lid, vendor_id, resolved_phone),
                    )
                elif event == "replied":
                    conn.execute(
                        f"""
                        UPDATE contact_health
                        SET {column} = CURRENT_TIMESTAMP,
                            last_reply_text = ?,
                            chat_lid = COALESCE(?, chat_lid),
                            consecutive_no_delivery = 0,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE vendor_id = ? AND phone = ?
                        """,
                        ((text or "")[:500], clean_lid, vendor_id, resolved_phone),
                    )
                else:  # read
                    conn.execute(
                        f"""
                        UPDATE contact_health
                        SET {column} = CURRENT_TIMESTAMP,
                            chat_lid = COALESCE(?, chat_lid),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE vendor_id = ? AND phone = ?
                        """,
                        (clean_lid, vendor_id, resolved_phone),
                    )

    def _resolve_phone_by_lid(self, conn: sqlite3.Connection, vendor_id: int, chat_lid: Optional[str]) -> Optional[str]:
        if not chat_lid:
            return None
        row = conn.execute(
            "SELECT phone FROM contact_health WHERE vendor_id = ? AND chat_lid = ? LIMIT 1",
            (vendor_id, chat_lid),
        ).fetchone()
        return row["phone"] if row else None

    async def link_chat_lid(self, vendor_id: int, phone: str, chat_lid: str):
        """Backfill da relacao phone <-> @lid quando descobrimos via whatsappNumbers."""
        if not phone or not chat_lid:
            return
        async with self._lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO contact_health (vendor_id, phone, chat_lid, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(vendor_id, phone) DO UPDATE SET
                        chat_lid = excluded.chat_lid,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (vendor_id, phone, chat_lid),
                )

    async def get_contact_health(self, vendor_id: int, phone: str) -> Optional[sqlite3.Row]:
        async with self._lock:
            with self.connect() as conn:
                return conn.execute(
                    "SELECT * FROM contact_health WHERE vendor_id = ? AND phone = ?",
                    (vendor_id, phone),
                ).fetchone()

    async def classify_phones(
        self,
        vendor_id: int,
        phones: Iterable[str],
        hot_reply_days: int = 90,
        hot_read_days: int = 30,
        warm_delivered_days: int = 180,
        cold_no_delivery_streak: int = 2,
    ) -> dict:
        """Classifica cada telefone como 'hot' | 'warm' | 'cold' | 'unknown'.

        - hot:    respondeu em <= hot_reply_days OU leu em <= hot_read_days
        - warm:   tem ao menos uma entrega registrada e nao se enquadra em hot/cold
        - cold:   streak de envios sem delivery >= cold_no_delivery_streak,
                  ou ultima entrega foi ha mais de warm_delivered_days
        - unknown: nunca foi tocado nesse vendor (sem registro em contact_health)
        """
        unique = sorted({p for p in phones if p})
        if not unique:
            return {}

        async with self._lock:
            with self.connect() as conn:
                rows: dict[str, sqlite3.Row] = {}
                for chunk_start in range(0, len(unique), 500):
                    chunk = unique[chunk_start : chunk_start + 500]
                    placeholders = ",".join("?" * len(chunk))
                    fetched = conn.execute(
                        f"""
                        SELECT phone, last_sent_at, last_delivered_at, last_read_at,
                               last_replied_at, consecutive_no_delivery
                        FROM contact_health
                        WHERE vendor_id = ?
                          AND phone IN ({placeholders})
                        """,
                        (vendor_id, *chunk),
                    ).fetchall()
                    for row in fetched:
                        rows[row["phone"]] = row

        from datetime import datetime

        def _parse(ts: Optional[str]):
            if not ts:
                return None
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                try:
                    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    return None

        now = datetime.utcnow()
        result: dict[str, str] = {}
        for phone in unique:
            row = rows.get(phone)
            if row is None:
                result[phone] = "unknown"
                continue

            replied = _parse(row["last_replied_at"])
            read = _parse(row["last_read_at"])
            delivered = _parse(row["last_delivered_at"])
            streak = int(row["consecutive_no_delivery"] or 0)

            if (replied and (now - replied).days <= hot_reply_days) or (
                read and (now - read).days <= hot_read_days
            ):
                result[phone] = "hot"
                continue

            cold = False
            if delivered and (now - delivered).days > warm_delivered_days:
                cold = True
            elif streak >= cold_no_delivery_streak and not delivered:
                cold = True

            if cold:
                result[phone] = "cold"
            elif delivered:
                result[phone] = "warm"
            else:
                # Tem registro mas sem delivery confirmado e streak baixo: trata como warm cauteloso.
                result[phone] = "warm"

        return result

    def _collect_blacklist_hits(self, conn: sqlite3.Connection, phones: Iterable[str]) -> set[str]:
        """Versao sincrona de filter_blacklisted, para usar dentro de uma transacao aberta."""
        unique = sorted({p for p in (phones or []) if p})
        if not unique:
            return set()
        hits: set[str] = set()
        for chunk_start in range(0, len(unique), 500):
            chunk = unique[chunk_start : chunk_start + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT phone FROM blacklist WHERE phone IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                hits.add(row["phone"])
        return hits

    def _cleanup_orphan_records(self, conn: sqlite3.Connection):
        conn.execute(
            """
            DELETE FROM sent_messages
            WHERE campaign_id NOT IN (SELECT id FROM campaigns)
               OR contact_id NOT IN (SELECT id FROM contacts)
            """
        )
        conn.execute("DELETE FROM media WHERE campaign_id NOT IN (SELECT id FROM campaigns)")
        conn.execute("DELETE FROM contacts WHERE campaign_id NOT IN (SELECT id FROM campaigns)")
        conn.execute("DELETE FROM contact_list_contacts WHERE list_id NOT IN (SELECT id FROM contact_lists)")
        conn.execute("DELETE FROM contact_list_snapshots WHERE list_id NOT IN (SELECT id FROM contact_lists)")
        conn.execute(
            """
            DELETE FROM contact_list_snapshot_contacts
            WHERE snapshot_id NOT IN (SELECT id FROM contact_list_snapshots)
            """
        )

    def _refresh_campaign_counters(self, conn: sqlite3.Connection):
        conn.execute(
            """
            UPDATE campaigns
            SET
                total_contacts = (
                    SELECT COUNT(*) FROM contacts WHERE contacts.campaign_id = campaigns.id
                ),
                sent_count = (
                    SELECT COUNT(*) FROM contacts
                    WHERE contacts.campaign_id = campaigns.id AND contacts.status = 'sent'
                ),
                current_index = (
                    SELECT COUNT(*) FROM contacts
                    WHERE contacts.campaign_id = campaigns.id AND contacts.status IN ('sent', 'failed')
                )
            """
        )

    def _refresh_campaign_counter(self, conn: sqlite3.Connection, campaign_id: int):
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN status IN ('sent', 'failed') THEN 1 ELSE 0 END) AS processed
            FROM contacts
            WHERE campaign_id = ?
            """,
            (campaign_id,),
        ).fetchone()
        conn.execute(
            """
            UPDATE campaigns
            SET total_contacts = ?, sent_count = ?, current_index = ?
            WHERE id = ?
            """,
            (
                int(row["total"] or 0),
                int(row["sent"] or 0),
                int(row["processed"] or 0),
                campaign_id,
            ),
        )


def _generic_name(name: Optional[str]) -> bool:
    return not name or name.strip().lower() in ("cliente", "contato", "sem nome")
