"""
Discord platform — receives messages from Discord and sends to Discord.
"""

import asyncio
import os
import discord
from discord.ext import commands
from bridge.platforms.base import BasePlatform
from bridge.utils.config import platform_media_dir
from bridge.utils.media import get_unique_filepath
from bridge.utils.logger import get_logger

logger = get_logger("discord")

MESSAGE_CHUNK_LIMIT = 1800

class DiscordPlatform(BasePlatform):
    """discord.py-based Discord bridge platform."""

    def __init__(self, platform_config: dict, bridges: list[dict], router=None):
        super().__init__("discord", platform_config, bridges, router)
        self._token = platform_config["token"]
        app_id = platform_config["app_id"]
        self._bot = commands.Bot(
            command_prefix=".",
            intents=discord.Intents.all(),
            application_id=app_id,
        )
        self._ready_event = asyncio.Event()
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Wire up discord.py event handlers."""

        @self._bot.event
        async def on_ready():
            logger.info("Discord bot is ready")
            self._ready_event.set()

        @self._bot.event
        async def on_message(message: discord.Message):
            if message.author == self._bot.user: return
            await self._handle_incoming_message(message)

        @self._bot.event
        async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
            await self._handle_reaction(payload)

    # Incoming events
    async def _handle_incoming_message(self, message: discord.Message) -> None:
        bridge_name = self.bridge_name_for_chat(message.channel.id)
        if bridge_name is None: return

        sender = message.author.display_name
        logger.info("Message from %s in %s", sender, bridge_name)
        media_dir = platform_media_dir(self.name)

        replied_to_id = None
        if message.reference and isinstance(message.reference.message_id, int):
            replied_to_id = message.reference.message_id

        # Download ALL attachments
        for attachment in message.attachments:
            original_name = attachment.filename
            file_name, file_type = os.path.splitext(original_name)
            if file_type.lower() == ".webp":
                file_type = ".png"
            file_path = get_unique_filepath(media_dir, file_name, file_type)
            await attachment.save(fp=file_path)
            await self.router.on_file(
                self.name, bridge_name, message.id,
                file_path, file_type, sender,
                replied_to_msg_id=replied_to_id,
            )

        # Text content
        if message.content:
            await self.router.on_message(
                self.name, bridge_name, message.id,
                message.content, sender,
                replied_to_msg_id=replied_to_id,
            )

    async def _handle_reaction(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.member and payload.member.bot: return
        if payload.user_id == self._bot.user.id: return

        channel = self._bot.get_channel(payload.channel_id)
        if channel is None: return

        bridge_name = self.bridge_name_for_chat(channel.id)
        if bridge_name is None: return

        user = self._bot.get_user(payload.user_id)
        sender = user.display_name if user else "Someone"
        emoji = payload.emoji.name

        await self.router.on_reaction(self.name, bridge_name, payload.message_id, emoji, sender)

    # Outbound messaging
    async def send_text(self, chat_id, content: str, sender: str, reply_to_native_id=None) -> int | None:
        channel = self._bot.get_channel(chat_id)
        if channel is None:
            logger.warning("Discord channel %s not found", chat_id)
            return None

        bridge = self.bridge_name_for_chat(chat_id)

        async def _reply_or_send(text: str) -> discord.Message:
            if reply_to_native_id:
                try:
                    target_msg = channel.get_partial_message(reply_to_native_id)
                    return await target_msg.reply(text)
                except discord.HTTPException: pass
            return await channel.send(text)

        formatted = content if sender == bridge else f"*{sender}:*\n{content}"

        sent_msg = None
        if len(content) > MESSAGE_CHUNK_LIMIT:
            for chunk in (content[i:i + MESSAGE_CHUNK_LIMIT] for i in range(0, len(content), MESSAGE_CHUNK_LIMIT)):
                text = chunk if sender == bridge else f"*{sender}:*\n{chunk}"
                sent_msg = await _reply_or_send(text)
                reply_to_native_id = None
        else:
            sent_msg = await _reply_or_send(formatted)

        return sent_msg.id if sent_msg else None

    async def send_file(self, chat_id, file_path: str, file_ext: str, sender: str, reply_to_native_id=None) -> int | None:
        channel = self._bot.get_channel(chat_id)
        if channel is None:
            logger.warning("Discord channel %s not found", chat_id)
            return None

        bridge = self.bridge_name_for_chat(chat_id)
        file_name = os.path.basename(file_path)

        async def _send_with_reply(content=None, file=None) -> discord.Message | None:
            if reply_to_native_id:
                try:
                    target_msg = channel.get_partial_message(reply_to_native_id)
                    return await target_msg.reply(content=content, file=file)
                except discord.HTTPException: pass
            if content and file: return await channel.send(content=content, file=file)
            elif content: return await channel.send(content=content)
            elif file: return await channel.send(file=file)
            return None

        sent_msg = None
        if os.path.getsize(file_path) > 8_388_608:
            msg = "File size is over 8MB, so I can't send it. Sorry :("
            text = msg if sender == bridge else f"*{sender}:* {msg}"
            await _send_with_reply(content=text)
            return None
        else:
            if sender == bridge: sent_msg = await _send_with_reply(file=discord.File(file_path))
            else: sent_msg = await _send_with_reply(file=discord.File(file_path), content=f"*{sender}:* [{file_name}]")

        return sent_msg.id if sent_msg else None

    # Lifecycle
    async def start(self) -> None:
        logger.info("Discord platform starting…")
        await self._bot.start(self._token)

    async def stop(self) -> None:
        await self._bot.close()
