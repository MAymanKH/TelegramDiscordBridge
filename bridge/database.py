"""
SQLite database helpers for text-message persistence.
"""

import time
import aiosqlite
from bridge.logger import get_logger

logger = get_logger("database")

_CREATE_TABLE_SQL = """
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

async def init_db(db_path: str) -> None:
    """Create the ``messages`` table if it does not exist."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(_CREATE_TABLE_SQL)
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
