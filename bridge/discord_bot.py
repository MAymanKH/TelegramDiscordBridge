"""
Discord bot — receives messages from Discord and forwards Telegram→Discord.
"""

import asyncio
import os
import discord
from discord.ext import commands
from bridge import config, database, media, polling
from bridge.logger import get_logger

logger = get_logger("discord")

# Configuration
settings = config.load_settings()
bridges = config.get_bridges(settings)

# Bot class
class BridgeBot(commands.Bot):
    """A ``commands.Bot`` subclass wired into the bridge system."""

    def __init__(self) -> None:
        app_id = settings["discord"]["app_id"]
        super().__init__(
            command_prefix=".",
            intents=discord.Intents.all(),
            application_id=app_id,
        )

    async def on_ready(self) -> None:
        logger.info("Discord bot is ready")
        await database.init_db(config.DISCORD_DB)
        asyncio.create_task(
            polling.poll_text_db(config.TELEGRAM_DB, _send_text),
        )
        asyncio.create_task(
            polling.poll_attachments_db(config.TELEGRAM_DB, _send_file),
        )

bot = BridgeBot()

# Helpers
def _discord_channel_for(bridge_name: str) -> discord.TextChannel | None:
    """Return the Discord channel object for a bridge by name, or ``None``."""
    for b in bridges:
        if bridge_name == b["name"]:
            return bot.get_channel(b["discord_chat_id"])
    return None

def _bridge_name_for_channel(channel_id: int) -> str | None:
    """Return the bridge name that matches *channel_id*, or ``None``."""
    for b in bridges:
        if channel_id == b["discord_chat_id"]:
            return b["name"]
    return None

# Incoming Discord messages
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    chname = _bridge_name_for_channel(message.channel.id)
    if chname is None:
        return

    sender = message.author.display_name
    logger.info("Message from %s in %s", sender, chname)

    # Download ALL attachments (Issue #5 fix)
    for attachment in message.attachments:
        original_name = attachment.filename
        file_name, file_type = os.path.splitext(original_name)
        # Issue #8 fix: convert webp to png for better Telegram compatibility
        if file_type.lower() == ".webp":
            file_type = ".png"
        file_path = media.get_unique_filepath(config.DISCORD_DIR, file_name, file_type)
        await attachment.save(fp=file_path)
        replied_to_id = None
        if message.reference and isinstance(message.reference.message_id, int):
            replied_to_id = message.reference.message_id
        await database.save_attachment_to_db(config.DISCORD_DB, message.id, file_path, file_type, sender, chname, replied_to_id)

    # Save text messages to DB
    if message.content:
        replied_to_id = None
        if message.reference and isinstance(message.reference.message_id, int):
            replied_to_id = message.reference.message_id
        await database.save_text_to_db(
            config.DISCORD_DB, message.id, message.content, sender, chname,
            replied_to_id,
        )

# Outgoing callbacks (Telegram → Discord)
MESSAGE_CHUNK_LIMIT = 1800
async def _send_text(internal_id, source_message_id, replied_to_message_id, content, sender, chat):
    """Callback for :func:`polling.poll_text_db` — send text to Discord."""
    channel = _discord_channel_for(chat)
    if channel is None:
        logger.warning("No Discord channel found for bridge '%s'", chat)
        return

    reply_to = None
    if replied_to_message_id:
        reply_to = await database.get_source_id(config.DISCORD_DB, replied_to_message_id)
        if not reply_to:
            reply_to = await database.get_forwarded_id(config.TELEGRAM_DB, replied_to_message_id)

    async def _reply_or_send(text: str) -> discord.Message:
        if reply_to:
            try:
                target_msg = channel.get_partial_message(reply_to)
                return await target_msg.reply(text)
            except discord.HTTPException:
                pass
        return await channel.send(text)

    formatted = content if sender == chat else f"*{sender}:*\n{content}"

    sent_msg = None
    if len(content) > MESSAGE_CHUNK_LIMIT:
        for chunk in (content[i:i + MESSAGE_CHUNK_LIMIT] for i in range(0, len(content), MESSAGE_CHUNK_LIMIT)):
            text = chunk if sender == chat else f"*{sender}:*\n{chunk}"
            sent_msg = await _reply_or_send(text)
            reply_to = None # Only reply on the first chunk
    else:
        sent_msg = await _reply_or_send(formatted)

    if sent_msg:
        await database.update_message_forwarded_id(config.TELEGRAM_DB, internal_id, sent_msg.id)


async def _send_file(source_message_id, replied_to_message_id, file_path, file_extension, sender, chat):
    """Callback for :func:`polling.poll_attachments_db` — send a file to Discord."""
    channel = _discord_channel_for(chat)
    if channel is None:
        logger.warning("No Discord channel found for bridge '%s'", chat)
        return

    file_name = os.path.basename(file_path)

    reply_to = None
    if replied_to_message_id:
        reply_to = await database.get_source_id(config.DISCORD_DB, replied_to_message_id)
        if not reply_to:
            reply_to = await database.get_forwarded_id(config.TELEGRAM_DB, replied_to_message_id)

    async def _send_with_reply(content=None, file=None):
        if reply_to:
            try:
                target_msg = channel.get_partial_message(reply_to)
                return await target_msg.reply(content=content, file=file)
            except discord.HTTPException:
                pass
        if content and file:
            return await channel.send(content=content, file=file)
        elif content:
            return await channel.send(content=content)
        elif file:
            return await channel.send(file=file)
        return None

    if os.path.getsize(file_path) > 8_388_608:
        msg = "File size is over 8MB, so I can't send it. Sorry :("
        if sender == chat:
            await _send_with_reply(content=msg)
        else:
            await _send_with_reply(content=f"*{sender}:* {msg}")
    else:
        if sender == chat:
            await _send_with_reply(file=discord.File(file_path))
        else:
            await _send_with_reply(file=discord.File(file_path), content=f"*{sender}:* [{file_name}]")

# Entry point
async def run() -> None:
    """Start the Discord bot (blocking)."""
    token = settings["discord"]["token"]
    logger.info("Discord bot starting…")
    await bot.start(token)
