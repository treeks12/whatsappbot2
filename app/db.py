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
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    async def setup(self):
        async with self._lock:
            with self.connect() as conn:
                conn.executescript(SCHEMA)

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

    async def set_caption(self, campaign_id: int, caption: str):
        async with self._lock:
            with self.connect() as conn:
                conn.execute("UPDATE campaigns SET caption = ?, status = 'ready' WHERE id = ?", (caption, campaign_id))

    async def add_contacts(self, campaign_id: int, contacts: Iterable[dict]):
        rows = [(campaign_id, item["row_index"], item.get("name"), item["phone"]) for item in contacts]
        async with self._lock:
            with self.connect() as conn:
                conn.executemany(
                    "INSERT OR IGNORE INTO contacts (campaign_id, row_index, name, phone) VALUES (?, ?, ?, ?)",
                    rows,
                )
                total = conn.execute("SELECT COUNT(*) FROM contacts WHERE campaign_id = ?", (campaign_id,)).fetchone()[0]
                conn.execute("UPDATE campaigns SET total_contacts = ? WHERE id = ?", (total, campaign_id))
                return total

    async def add_media(self, campaign_id: int, path: str, mime_type: str, file_name: str):
        async with self._lock:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO media (campaign_id, path, mime_type, file_name) VALUES (?, ?, ?, ?)",
                    (campaign_id, path, mime_type, file_name),
                )

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
                    SELECT * FROM contacts
                    WHERE campaign_id = ? AND status = 'pending'
                    ORDER BY row_index LIMIT 1
                    """,
                    (campaign_id,),
                ).fetchone()

    async def mark_contact_sent(self, campaign_id: int, contact_id: int, phone: str):
        async with self._lock:
            with self.connect() as conn:
                conn.execute("UPDATE contacts SET status = 'sent', error = NULL WHERE id = ?", (contact_id,))
                conn.execute(
                    "INSERT OR IGNORE INTO sent_messages (campaign_id, contact_id, phone) VALUES (?, ?, ?)",
                    (campaign_id, contact_id, phone),
                )
                sent_count = conn.execute(
                    "SELECT COUNT(*) FROM contacts WHERE campaign_id = ? AND status = 'sent'",
                    (campaign_id,),
                ).fetchone()[0]
                conn.execute("UPDATE campaigns SET sent_count = ?, current_index = ? WHERE id = ?", (sent_count, sent_count, campaign_id))

    async def mark_contact_failed(self, contact_id: int, error: str):
        async with self._lock:
            with self.connect() as conn:
                conn.execute("UPDATE contacts SET status = 'failed', error = ? WHERE id = ?", (error[:500], contact_id))

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
