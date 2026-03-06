"""
Discord bot — receives messages from Discord and forwards Telegram→Discord.

Only Discord-specific logic lives here; shared polling, DB, media, and
config helpers are imported from the ``bridge`` package.
"""

import asyncio
import os

import discord
from discord.ext import commands

from bridge import config, database, media, polling
from bridge.logger import get_logger

logger = get_logger("discord")

# ── Configuration ──────────────────────────────────────────────────────────

settings = config.load_settings()
bridges = config.get_bridges(settings)


# ── Bot class ──────────────────────────────────────────────────────────────


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
            polling.poll_new_files(config.TELEGRAM_DIR, config.TELEGRAM_ATTACHMENTS_JSON, _send_file),
        )


bot = BridgeBot()


# ── Helpers ────────────────────────────────────────────────────────────────


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


# ── Incoming Discord messages ──────────────────────────────────────────────


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
        media.save_attachment_json(config.DISCORD_ATTACHMENTS_JSON, file_path, sender, chname)
        await attachment.save(fp=file_path)

    # Save text messages to DB
    if message.content:
        replied_to_text = None
        replied_to_sender = None
        ref = message.reference
        if ref and getattr(ref, "resolved", None) and isinstance(ref.resolved, discord.Message):
            replied_to_text = ref.resolved.content
            replied_to_sender = ref.resolved.author.display_name
        await database.save_text_to_db(
            config.DISCORD_DB, message.content, sender, chname,
            replied_to_text, replied_to_sender,
        )


# ── Outgoing callbacks (Telegram → Discord) ───────────────────────────────

MESSAGE_CHUNK_LIMIT = 1800


async def _send_text(content, sender, chat, replied_to_text, replied_to_sender):
    """Callback for :func:`polling.poll_text_db` — send text to Discord."""
    channel = _discord_channel_for(chat)
    if channel is None:
        logger.warning("No Discord channel found for bridge '%s'", chat)
        return

    async def _reply_or_send(text: str) -> None:
        if replied_to_text is None:
            await channel.send(text)
        else:
            async for msg in channel.history(limit=100):
                if msg.content == replied_to_text or msg.content == f"*{replied_to_sender}:*\n{replied_to_text}":
                    await msg.reply(text)
                    break
            else:
                await channel.send(text)

    formatted = content if sender == chat else f"*{sender}:*\n{content}"

    if len(content) > MESSAGE_CHUNK_LIMIT:
        for chunk in (content[i:i + MESSAGE_CHUNK_LIMIT] for i in range(0, len(content), MESSAGE_CHUNK_LIMIT)):
            text = chunk if sender == chat else f"*{sender}:*\n{chunk}"
            await _reply_or_send(text)
    else:
        await _reply_or_send(formatted)


async def _send_file(file_path, file_extension, sender, chat):
    """Callback for :func:`polling.poll_new_files` — send a file to Discord."""
    channel = _discord_channel_for(chat)
    if channel is None:
        logger.warning("No Discord channel found for bridge '%s'", chat)
        return

    file_name = os.path.basename(file_path)

    if os.path.getsize(file_path) > 8_388_608:
        msg = "File size is over 8MB, so I can't send it. Sorry :("
        if sender == chat:
            await channel.send(msg)
        else:
            await channel.send(f"*{sender}:* {msg}")
    else:
        if sender == chat:
            await channel.send(file=discord.File(file_path))
        else:
            await channel.send(file=discord.File(file_path), content=f"*{sender}:* [{file_name}]")


# ── Entry point ────────────────────────────────────────────────────────────


async def run() -> None:
    """Start the Discord bot (blocking)."""
    token = settings["discord"]["token"]
    logger.info("Discord bot starting…")
    await bot.start(token)
