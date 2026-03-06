"""
Shared polling loops for watching incoming messages from the other platform.
"""

import asyncio
import os
from collections.abc import Awaitable, Callable
import aiosqlite
from bridge.logger import get_logger

logger = get_logger("polling")

# Type aliases for the callbacks each bot must supply.
TextCallback = Callable[..., Awaitable[None]]
FileCallback = Callable[..., Awaitable[None]]

async def poll_text_db(
    db_path: str,
    on_new_message: TextCallback,
) -> None:
    """Poll *db_path* for new rows and invoke *on_new_message* for each.

    Parameters passed to *on_new_message*::

        on_new_message(internal_id, source_message_id, replied_to_message_id, content, sender, chat)
    """
    last_id = 0

    while True:
        await asyncio.sleep(0.2)
        try:
            async with aiosqlite.connect(db_path) as db:
                async with db.execute("SELECT MAX(id) FROM messages") as cur:
                    row = await cur.fetchone()
                    max_id = row[0]
                    if max_id is None or max_id <= last_id:
                        continue
                    last_id = max_id

                async with db.execute(
                    "SELECT id, source_message_id, replied_to_message_id, content, sender, chat "
                    "FROM messages WHERE id = ?",
                    (last_id,),
                ) as cur:
                    row = await cur.fetchone()
                    if row is None:
                        continue
                    internal_id, source_message_id, replied_to_message_id, content, sender, chat = row
        except Exception:
            logger.debug("Polling %s — DB not ready or transient error", db_path, exc_info=True)
            continue

        if not content:
            continue

        logger.info("New text in %s from %s (chat=%s)", db_path, sender, chat)
        await on_new_message(internal_id, source_message_id, replied_to_message_id, content, sender, chat)


async def poll_attachments_db(
    db_path: str,
    on_new_file: FileCallback,
) -> None:
    """Poll the attachments table in *db_path* for new rows.

    Parameters passed to *on_new_file*::

        on_new_file(source_message_id, replied_to_message_id, file_path, file_ext, sender, chat)

    The attachment row and the file on disk are deleted after the callback
    returns (or raises).
    """
    from bridge.database import delete_attachment

    last_id = 0

    while True:
        await asyncio.sleep(0.5)
        try:
            async with aiosqlite.connect(db_path) as db:
                async with db.execute("SELECT MAX(id) FROM attachments") as cur:
                    row = await cur.fetchone()
                    max_id = row[0]
                    if max_id is None or max_id <= last_id:
                        continue
                    last_id = max_id

                async with db.execute(
                    "SELECT id, source_message_id, replied_to_message_id, file_path, file_ext, sender, chat "
                    "FROM attachments WHERE id = ?",
                    (last_id,),
                ) as cur:
                    row = await cur.fetchone()
                    if row is None:
                        continue
                    att_id, source_message_id, replied_to_message_id, file_path, file_ext, sender, chat = row
        except Exception:
            logger.debug("Polling %s attachments — DB not ready or transient error", db_path, exc_info=True)
            continue

        if not file_path or not os.path.isfile(file_path):
            logger.warning("Attachment file missing: %s", file_path)
            await delete_attachment(db_path, att_id)
            continue

        logger.info("New attachment %s from %s (chat=%s)", file_path, sender, chat)
        try:
            await on_new_file(source_message_id, replied_to_message_id, file_path, file_ext, sender, chat)
        finally:
            await delete_attachment(db_path, att_id)
            if os.path.isfile(file_path):
                os.remove(file_path)
