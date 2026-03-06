"""
SQLite database helpers for text-message persistence.
"""

import time
import aiosqlite
from bridge.logger import get_logger

logger = get_logger("database")

_CREATE_MESSAGES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    source_message_id     INT,
    replied_to_message_id INT,
    forwarded_message_id  INT,
    content               TEXT,
    sender                TEXT,
    chat                  TEXT,
    sent_at               INT
)
"""

_CREATE_ATTACHMENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS attachments (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    source_message_id     INT,
    replied_to_message_id INT,
    forwarded_message_id  INT,
    file_path             TEXT,
    file_ext              TEXT,
    sender                TEXT,
    chat                  TEXT,
    sent_at               INT
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
    source_message_id: int,
    content: str,
    sender: str,
    chat: str,
    replied_to_message_id: int | None = None,
) -> None:
    """Insert a single text message into the SQLite database."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO messages "
            "(source_message_id, replied_to_message_id, content, sender, chat, sent_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (source_message_id, replied_to_message_id, content, sender, chat, int(time.time() * 1000)),
        )
        await db.commit()
    logger.debug("Saved text from %s in chat %s", sender, chat)

async def save_attachment_to_db(
    db_path: str,
    source_message_id: int,
    file_path: str,
    file_ext: str,
    sender: str,
    chat: str,
    replied_to_message_id: int | None = None,
) -> None:
    """Insert attachment metadata into the SQLite database."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO attachments (source_message_id, replied_to_message_id, file_path, file_ext, sender, chat, sent_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_message_id, replied_to_message_id, file_path, file_ext, sender, chat, int(time.time() * 1000)),
        )
        await db.commit()
    logger.debug("Saved attachment %s from %s in chat %s", file_path, sender, chat)

async def delete_attachment(db_path: str, attachment_id: int) -> None:
    """Delete an attachment row by ID after it has been forwarded."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
        await db.commit()

async def get_forwarded_id(db_path: str, source_id: int) -> int | None:
    """Get the forwarded_message_id mapped to the given source_message_id."""
    try:
        async with aiosqlite.connect(db_path) as db:
            # Check messages table
            async with db.execute("SELECT forwarded_message_id FROM messages WHERE source_message_id = ? AND forwarded_message_id IS NOT NULL", (source_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    return row[0]
            # Check attachments table
            async with db.execute("SELECT forwarded_message_id FROM attachments WHERE source_message_id = ? AND forwarded_message_id IS NOT NULL", (source_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    return row[0]
    except Exception:
        pass
    return None

async def get_source_id(db_path: str, forwarded_id: int) -> int | None:
    """Get the original source_message_id mapped to the given forwarded_message_id."""
    try:
        async with aiosqlite.connect(db_path) as db:
            # Check messages table
            async with db.execute("SELECT source_message_id FROM messages WHERE forwarded_message_id = ?", (forwarded_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    return row[0]
            # Check attachments table
            async with db.execute("SELECT source_message_id FROM attachments WHERE forwarded_message_id = ?", (forwarded_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    return row[0]
    except Exception:
        pass
    return None

async def update_message_forwarded_id(db_path: str, internal_id: int, forwarded_id: int) -> None:
    """Update the forwarded_message_id for a message in the database."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE messages SET forwarded_message_id = ? WHERE id = ?", (forwarded_id, internal_id))
        await db.commit()
