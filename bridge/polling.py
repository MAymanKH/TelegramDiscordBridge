"""
Shared polling loops for watching incoming messages from the other platform.
"""

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
import aiosqlite
from bridge.logger import get_logger
from bridge.media import IGNORED_FILES, MEDIA_EXTENSIONS

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

        on_new_message(content, sender, chat,
                       replied_to_text, replied_to_sender)

    ``replied_to_text`` and ``replied_to_sender`` may be ``None`` when the
    source database does not include those columns.
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
                    "SELECT content, sender, chat, replied_to_text, replied_to_sender "
                    "FROM messages WHERE id = ?",
                    (last_id,),
                ) as cur:
                    row = await cur.fetchone()
                    if row is None:
                        continue
                    content, sender, chat, replied_to_text, replied_to_sender = row
        except Exception:
            logger.debug("Polling %s — DB not ready or transient error", db_path, exc_info=True)
            continue

        if not content:
            continue

        logger.info("New text in %s from %s (chat=%s)", db_path, sender, chat)
        await on_new_message(content, sender, chat, replied_to_text, replied_to_sender)


async def poll_new_files(
    directory: str,
    attachments_json: str,
    on_new_file: FileCallback,
) -> None:
    """Poll *directory* for new media files, invoke *on_new_file* for each.

    Parameters passed to *on_new_file*::

        on_new_file(file_path, file_extension, sender, chat)

    The file is deleted after the callback returns (or raises).
    """
    while True:
        await asyncio.sleep(1)
        try:
            entries = os.listdir(directory)
        except FileNotFoundError:
            continue

        for file in entries:
            file_extension = os.path.splitext(file)[1].lower()

            if file_extension == ".temp":
                continue

            if file_extension not in MEDIA_EXTENSIONS:
                if file not in IGNORED_FILES:
                    try:
                        os.remove(os.path.join(directory, file))
                    except OSError:
                        pass
                continue

            # Read attachment metadata
            try:
                with open(attachments_json, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                sender = data["message"]["sender"]
                chat = data["message"]["chat"]
            except (KeyError, json.JSONDecodeError, FileNotFoundError):
                logger.warning("Could not read attachment metadata from %s", attachments_json)
                continue

            file_path = os.path.join(directory, file)
            logger.info("New file %s from %s (chat=%s)", file, sender, chat)

            try:
                await on_new_file(file_path, file_extension, sender, chat)
            finally:
                if os.path.isfile(file_path):
                    os.remove(file_path)
