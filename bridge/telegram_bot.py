"""
Telegram bot — receives messages from Telegram and forwards Discord→Telegram.
"""

import asyncio
import os

# Pyrogram calls asyncio.get_event_loop() at import time. Python 3.14
try: asyncio.get_event_loop()
except RuntimeError: asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters, types
from bridge import config, database, media, polling
from bridge.logger import get_logger

logger = get_logger("telegram")

# Configuration
settings = config.load_settings()
telegram_info = settings["telegram"]
bridges = config.get_bridges(settings)
api_id = telegram_info["api_id"]
api_hash = telegram_info["api_hash"]
bot_token = telegram_info.get("bot_token")
phone_number = telegram_info.get("phone")

if bot_token:
    app = Client("my_bot", api_id=api_id, api_hash=api_hash, bot_token=bot_token)
elif phone_number:
    app = Client("my_bot", api_id=api_id, api_hash=api_hash, phone_number=phone_number)
else:
    app = Client("my_bot", api_id=api_id, api_hash=api_hash)

SOURCE_CHATS = [b["telegram_chat_id"] for b in bridges]

# Track processed media groups to avoid duplicate downloads (Issue #4 fix)
_processed_media_groups: set[str] = set()

# Telegram-specific helpers
def _bridge_name_for_chat(chat_id: int) -> str | None:
    """Return the bridge name that matches *chat_id*, or ``None``."""
    for b in bridges:
        if chat_id == b["telegram_chat_id"]:
            return b["name"]
    return None

def _telegram_chat_id_for(bridge_name: str) -> int | None:
    """Return the Telegram chat ID for a bridge by name, or ``None``."""
    for b in bridges:
        if bridge_name == b["name"]:
            return b["telegram_chat_id"]
    return None

def get_sender_name(message: types.Message, fallback: str = "Unknown") -> str:
    """Extract a display name from a Pyrogram message."""
    try:
        first = message.from_user.first_name or ""
        last = message.from_user.last_name or ""
        name = f"{first} {last}".strip()
        return name if name else (message.from_user.username or fallback)
    except AttributeError:
        return fallback

def get_media_info(msg: types.Message) -> tuple[str, str]:
    """Return ``(file_name, file_extension)`` from a Pyrogram message.

    *file_extension* includes the leading dot, e.g. ``'.png'``.
    """
    if msg.document:
        return os.path.splitext(msg.document.file_name)
    if msg.photo:
        return "image", ".png"
    if msg.video:
        return "video", ".mp4"
    if msg.audio:
        return "audio", ".mp3"
    if msg.voice:
        return "voice", ".mp3"
    if msg.sticker:
        return "sticker", ".webp"
    return "file", ""

# Incoming Telegram messages
@app.on_message(filters.chat(SOURCE_CHATS))
async def on_telegram_message(client: Client, message: types.Message):
    chname = _bridge_name_for_chat(message.chat.id)
    if chname is None:
        return

    sender = get_sender_name(message, fallback=chname)
    logger.info("Message from %s in %s", sender, chname)

    # Media group (Issue #4 fix: deduplicate)
    if message.media_group_id:
        if message.media_group_id in _processed_media_groups:
            return
        _processed_media_groups.add(message.media_group_id)
        if len(_processed_media_groups) > 1000:
            _processed_media_groups.clear()

        media_group = await app.get_media_group(message.chat.id, message.id)
        for i, item in enumerate(media_group):
            await asyncio.sleep(0.5)
            file_name, file_type = get_media_info(item)
            file_path = os.path.join(config.TELEGRAM_DIR, f"{file_name}_({i}){file_type}")
            await client.download_media(item, file_name=file_path)
            await database.save_attachment_to_db(config.TELEGRAM_DB, file_path, file_type, sender, chname)
        if message.caption:
            await database.save_text_to_db(config.TELEGRAM_DB, message.caption, sender, chname)

    # Single attachment
    elif message.media:
        file_name, file_type = get_media_info(message)
        file_path = media.get_unique_filepath(config.TELEGRAM_DIR, file_name, file_type)
        await client.download_media(message, file_path)
        await database.save_attachment_to_db(config.TELEGRAM_DB, file_path, file_type, sender, chname)
        if message.caption:
            await database.save_text_to_db(config.TELEGRAM_DB, message.caption, sender, chname)

    # Text message
    else:
        replied_to = message.reply_to_message
        replied_to_text = None
        replied_to_sender = None
        if replied_to is not None:
            replied_to_text = replied_to.text
            replied_to_sender = get_sender_name(replied_to, fallback=chname)
        await database.save_text_to_db(
            config.TELEGRAM_DB, message.text, sender, chname,
            replied_to_text, replied_to_sender,
        )

# Outgoing callbacks (Discord → Telegram)
MESSAGE_CHUNK_LIMIT = 1800
async def _send_text(content, sender, chat, _replied_to_text, _replied_to_sender):
    """Callback for :func:`polling.poll_text_db` — send text to Telegram."""
    chat_id = _telegram_chat_id_for(chat)
    if chat_id is None:
        logger.warning("No Telegram chat found for bridge '%s'", chat)
        return

    if len(content) > MESSAGE_CHUNK_LIMIT:
        for chunk in (content[i:i + MESSAGE_CHUNK_LIMIT] for i in range(0, len(content), MESSAGE_CHUNK_LIMIT)):
            await app.send_message(chat_id, f"**{sender}:**\n{chunk}")
    else:
        await app.send_message(chat_id, f"**{sender}:**\n{content}")


async def _send_file(file_path, file_extension, sender, chat):
    """Callback for :func:`polling.poll_attachments_db` — send a file to Telegram."""
    chat_id = _telegram_chat_id_for(chat)
    if chat_id is None:
        logger.warning("No Telegram chat found for bridge '%s'", chat)
        return

    caption = f"**{sender}:**"

    if os.path.getsize(file_path) > 8_388_608:
        await app.send_message(chat_id, f"**{sender}:** File size is over 8MB, can't send it.")
        return

    if file_extension in media.PHOTO_EXTENSIONS:
        await app.send_photo(chat_id, file_path, caption=caption)
    elif file_extension == ".mp4":
        await app.send_video(chat_id, file_path, caption=caption)
    elif file_extension == ".mp3":
        await app.send_audio(chat_id, file_path, caption=caption)
    elif file_extension == ".ogg":
        await app.send_voice(chat_id, file_path, caption=caption)
    elif file_extension == ".webp":
        await app.send_document(chat_id, file_path, caption=caption)
    elif file_extension in (".pdf", ".apk"):
        await app.send_document(chat_id, file_path, caption=caption)

# Entry point
async def run() -> None:
    """Start the Telegram client, initialize the DB, and poll for Discord messages."""
    logger.info("Telegram bot starting…")
    await database.init_db(config.TELEGRAM_DB)
    async with app:
        logger.info("Telegram bot is alive")
        await asyncio.gather(
            polling.poll_text_db(config.DISCORD_DB, _send_text),
            polling.poll_attachments_db(config.DISCORD_DB, _send_file),
        )
