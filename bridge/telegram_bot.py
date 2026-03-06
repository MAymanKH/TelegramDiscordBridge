"""
Telegram bot — receives messages from Telegram and forwards Discord→Telegram.
"""

import asyncio
import os
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
            await database.save_attachment_to_db(config.TELEGRAM_DB, message.id, file_path, file_type, sender, chname, message.reply_to_message_id)
        if message.caption:
            await database.save_text_to_db(config.TELEGRAM_DB, message.id, message.caption, sender, chname, message.reply_to_message_id)

    # Single attachment
    elif message.media:
        file_name, file_type = get_media_info(message)
        file_path = media.get_unique_filepath(config.TELEGRAM_DIR, file_name, file_type)
        await client.download_media(message, file_path)
        await database.save_attachment_to_db(config.TELEGRAM_DB, message.id, file_path, file_type, sender, chname, message.reply_to_message_id)
        if message.caption:
            await database.save_text_to_db(config.TELEGRAM_DB, message.id, message.caption, sender, chname, message.reply_to_message_id)

    else:
        await database.save_text_to_db(
            config.TELEGRAM_DB, message.id, message.text, sender, chname,
            message.reply_to_message_id,
        )

# Reaction Handling
@app.on_message_reaction_updated(filters.chat(SOURCE_CHATS))
async def on_telegram_reaction(client: Client, update: types.MessageReactions):
    chname = _bridge_name_for_chat(update.chat.id)
    if chname is None:
        return

    # User who reacted
    user = getattr(update, "from_user", None)
    if user and user.is_bot:
        return
    
    sender = f"{user.first_name or ''} {user.last_name or ''}".strip() if user else "Someone"
    if not sender and user and user.username:
        sender = user.username
    elif not sender:
        sender = "Someone"

    # update.new_reaction gives the new list of reactions. 
    if not hasattr(update, "new_reaction") or not update.new_reaction:
        return # Reactions were removed
    
    reaction = update.new_reaction[0]
    emoji = getattr(reaction, "emoji", None)
    if not emoji:
        emoji = getattr(reaction, "custom_emoji_id", "a custom emoji")

    # Determine Discord message to reply to
    telegram_msg_id = getattr(update, "message_id", getattr(update, "id", None))
    if telegram_msg_id is None:
        return

    discord_msg_id = None

    # Was this message originally from Telegram?
    forwarded_id = await database.get_forwarded_id(config.TELEGRAM_DB, telegram_msg_id)
    if forwarded_id:
        discord_msg_id = forwarded_id
    else:
        # Was this message originally from Discord?
        source_id = await database.get_source_id(config.DISCORD_DB, telegram_msg_id)
        if source_id:
            discord_msg_id = source_id

    if discord_msg_id:
        # Queue the reaction reply to be sent by Discord bot
        # We'll save a special text message locally
        content = f"> {sender} reacted with {emoji}"
        await database.save_text_to_db(
            config.TELEGRAM_DB, 
            telegram_msg_id + hash(f"{sender}{emoji}") % 100000, # Fake ID
            content, 
            chname, # Make sender equal to chat name so Discord displays it purely as content without prefix
            chname,
            telegram_msg_id # Pass this side's native ID so the other side can translate it normally
        )


# Outgoing callbacks (Discord → Telegram)
MESSAGE_CHUNK_LIMIT = 1800
async def _send_text(internal_id, source_message_id, replied_to_message_id, content, sender, chat):
    """Callback for :func:`polling.poll_text_db` — send text to Telegram."""
    chat_id = _telegram_chat_id_for(chat)
    if chat_id is None:
        logger.warning("No Telegram chat found for bridge '%s'", chat)
        return

    reply_to = None
    if replied_to_message_id:
        reply_to = await database.get_source_id(config.TELEGRAM_DB, replied_to_message_id)
        if not reply_to:
            reply_to = await database.get_forwarded_id(config.DISCORD_DB, replied_to_message_id)
        # Fallback for reactions: the ID passed might already be a native Telegram message ID
        if not reply_to and str(content).startswith("> "):
            reply_to = replied_to_message_id

    text = content if sender == chat else f"**{sender}:**\n{content}"

    sent_msg = None
    if len(content) > MESSAGE_CHUNK_LIMIT:
        for chunk in (content[i:i + MESSAGE_CHUNK_LIMIT] for i in range(0, len(content), MESSAGE_CHUNK_LIMIT)):
            chunk_text = chunk if sender == chat else f"**{sender}:**\n{chunk}"
            sent_msg = await app.send_message(chat_id, chunk_text, reply_to_message_id=reply_to)
            reply_to = None # Only reply to the first chunk
    else:
        sent_msg = await app.send_message(chat_id, text, reply_to_message_id=reply_to)

    if sent_msg:
        await database.update_message_forwarded_id(config.DISCORD_DB, internal_id, sent_msg.id)


async def _send_file(source_message_id, replied_to_message_id, file_path, file_extension, sender, chat):
    """Callback for :func:`polling.poll_attachments_db` — send a file to Telegram."""
    chat_id = _telegram_chat_id_for(chat)
    if chat_id is None:
        logger.warning("No Telegram chat found for bridge '%s'", chat)
        return

    caption = "" if sender == chat else f"**{sender}:**"

    if os.path.getsize(file_path) > 8_388_608:
        msg = "File size is over 8MB, can't send it."
        text = msg if sender == chat else f"**{sender}:** {msg}"
        await app.send_message(chat_id, text)
        return

    reply_to = None
    if replied_to_message_id:
        reply_to = await database.get_source_id(config.TELEGRAM_DB, replied_to_message_id)
        if not reply_to:
            reply_to = await database.get_forwarded_id(config.DISCORD_DB, replied_to_message_id)

    sent_msg = None
    if file_extension in media.PHOTO_EXTENSIONS:
        sent_msg = await app.send_photo(chat_id, file_path, caption=caption, reply_to_message_id=reply_to)
    elif file_extension == ".mp4":
        sent_msg = await app.send_video(chat_id, file_path, caption=caption, reply_to_message_id=reply_to)
    elif file_extension == ".mp3":
        sent_msg = await app.send_audio(chat_id, file_path, caption=caption, reply_to_message_id=reply_to)
    elif file_extension == ".ogg":
        sent_msg = await app.send_voice(chat_id, file_path, caption=caption, reply_to_message_id=reply_to)
    elif file_extension == ".webp":
        sent_msg = await app.send_document(chat_id, file_path, caption=caption, reply_to_message_id=reply_to)
    elif file_extension in (".pdf", ".apk"):
        sent_msg = await app.send_document(chat_id, file_path, caption=caption, reply_to_message_id=reply_to)

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
