"""
SQLite database helpers for text-message persistence.
"""

import time
import aiosqlite
from bridge.logger import get_logger

logger = get_logger("database")

_CREATE_MESSAGES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    content           TEXT,
    sender            TEXT,
    chat              TEXT,
    replied_to_text   TEXT,
    replied_to_sender TEXT,
    sent_at           INT
)
"""

_CREATE_ATTACHMENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS attachments (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT,
    file_ext  TEXT,
    sender    TEXT,
    chat      TEXT,
    sent_at   INT
)
"""

async def init_db(db_path: str) -> None:
    """Create the messages and attachments tables if they do not exist."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(_CREATE_MESSAGES_TABLE_SQL)
        await db.execute(_CREATE_ATTACHMENTS_TABLE_SQL)
        await db.commit()
    logger.info("Database initialized: %s", db_path)

async def save_text_to_db(
    db_path: str,
    content: str,
    sender: str,
    chat: str,
    replied_to_text: str | None = None,
    replied_to_sender: str | None = None,
) -> None:
    """Insert a single text message into the SQLite database."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO messages "
            "(content, sender, chat, replied_to_text, replied_to_sender, sent_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (content, sender, chat, replied_to_text, replied_to_sender, int(time.time() * 1000)),
        )
        await db.commit()
    logger.debug("Saved text from %s in chat %s", sender, chat)

async def save_attachment_to_db(
    db_path: str,
    file_path: str,
    file_ext: str,
    sender: str,
    chat: str,
) -> None:
    """Insert attachment metadata into the SQLite database."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO attachments (file_path, file_ext, sender, chat, sent_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (file_path, file_ext, sender, chat, int(time.time() * 1000)),
        )
        await db.commit()
    logger.debug("Saved attachment %s from %s in chat %s", file_path, sender, chat)

async def delete_attachment(db_path: str, attachment_id: int) -> None:
    """Delete an attachment row by ID after it has been forwarded."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
        await db.commit()
