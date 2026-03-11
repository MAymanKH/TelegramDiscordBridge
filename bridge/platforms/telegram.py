"""
Telegram platform — receives messages from Telegram and sends to Telegram.
"""

import asyncio
import os
from pyrogram import Client, filters, types
from bridge.platforms.base import BasePlatform
from bridge.utils.config import platform_media_dir
from bridge.utils.media import classify_attachment, get_unique_filepath
from bridge.utils.logger import get_logger

logger = get_logger("telegram")

# Track processed media groups to avoid duplicate downloads
_processed_media_groups: set[str] = set()

MESSAGE_CHUNK_LIMIT = 1800

def _get_sender_name(message: types.Message, fallback: str = "Unknown") -> str:
    """Extract a display name from a Pyrogram message."""
    try:
        first = message.from_user.first_name or ""
        last = message.from_user.last_name or ""
        name = f"{first} {last}".strip()
        return name if name else (message.from_user.username or fallback)
    except AttributeError:
        return fallback

def _get_media_info(msg: types.Message) -> tuple[str, str]:
    """Return ``(file_name, file_extension)`` from a Pyrogram message."""
    if msg.document: return os.path.splitext(msg.document.file_name)
    if msg.photo: return "image", ".png"
    if msg.video: return "video", ".mp4"
    if msg.audio: return "audio", ".mp3"
    if msg.voice: return "voice", ".mp3"
    if msg.sticker: return "sticker", ".webp"
    return "file", ""

class TelegramPlatform(BasePlatform):
    """Pyrogram-based Telegram bridge platform."""

    def __init__(self, platform_config: dict, bridges: list[dict], router=None):
        super().__init__("telegram", platform_config, bridges, router)
        api_id = platform_config["api_id"]
        api_hash = platform_config["api_hash"]
        bot_token = platform_config.get("bot_token")
        phone_number = platform_config.get("phone")

        if bot_token: self._app = Client("my_bot", api_id=api_id, api_hash=api_hash, bot_token=bot_token)
        elif phone_number: self._app = Client("my_bot", api_id=api_id, api_hash=api_hash, phone_number=phone_number)
        else: self._app = Client("my_bot", api_id=api_id, api_hash=api_hash)

        self._source_chats = self.my_chat_ids()
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Wire up Pyrogram event handlers."""

        @self._app.on_message(filters.chat(self._source_chats))
        async def _on_message(client: Client, message: types.Message):
            await self._handle_incoming_message(client, message)

        if self._source_chats:
            @self._app.on_message_reaction_updated(filters.chat(self._source_chats))
            async def _on_reaction(client: Client, update: types.MessageReactions):
                await self._handle_reaction(client, update)

    # Incoming events
    async def _handle_incoming_message(self, client: Client, message: types.Message) -> None:
        bridge_name = self.bridge_name_for_chat(message.chat.id)
        if bridge_name is None: return

        sender = _get_sender_name(message, fallback=bridge_name)
        logger.info("Message from %s in %s", sender, bridge_name)
        media_dir = platform_media_dir(self.name)

        # Media group (deduplicate)
        if message.media_group_id:
            if message.media_group_id in _processed_media_groups: return
            _processed_media_groups.add(message.media_group_id)
            if len(_processed_media_groups) > 1000: _processed_media_groups.clear()

            media_group = await self._app.get_media_group(message.chat.id, message.id)
            for i, item in enumerate(media_group):
                await asyncio.sleep(0.5)
                file_name, file_type = _get_media_info(item)
                file_path = os.path.join(media_dir, f"{file_name}_({i}){file_type}")
                await client.download_media(item, file_name=file_path)
                await self.router.on_file(
                    self.name, bridge_name, message.id,
                    file_path, file_type, sender,
                    replied_to_msg_id=message.reply_to_message_id,
                )
            if message.caption:
                await self.router.on_message(
                    self.name, bridge_name, message.id,
                    message.caption, sender,
                    replied_to_msg_id=message.reply_to_message_id,
                )

        # Single attachment
        elif message.media:
            file_name, file_type = _get_media_info(message)
            file_path = get_unique_filepath(media_dir, file_name, file_type)
            await client.download_media(message, file_path)
            await self.router.on_file(
                self.name, bridge_name, message.id,
                file_path, file_type, sender,
                replied_to_msg_id=message.reply_to_message_id,
            )
            if message.caption:
                await self.router.on_message(
                    self.name, bridge_name, message.id,
                    message.caption, sender,
                    replied_to_msg_id=message.reply_to_message_id,
                )

        # Plain text
        else:
            await self.router.on_message(
                self.name, bridge_name, message.id,
                message.text, sender,
                replied_to_msg_id=message.reply_to_message_id,
            )

    async def _handle_reaction(self, client: Client, update: types.MessageReactions) -> None:
        bridge_name = self.bridge_name_for_chat(update.chat.id)
        if bridge_name is None: return

        user = getattr(update, "from_user", None)
        if user and user.is_bot: return

        sender = f"{user.first_name or ''} {user.last_name or ''}".strip() if user else "Someone"
        if not sender and user and user.username: sender = user.username
        elif not sender: sender = "Someone"

        if not hasattr(update, "new_reaction") or not update.new_reaction: return

        reaction = update.new_reaction[0]
        emoji = getattr(reaction, "emoji", None)
        if not emoji: emoji = getattr(reaction, "custom_emoji_id", "a custom emoji")

        msg_id = getattr(update, "message_id", getattr(update, "id", None))
        if msg_id is None: return

        await self.router.on_reaction(self.name, bridge_name, msg_id, emoji, sender)

    # Outbound messaging
    async def send_text(self, chat_id, content: str, sender: str, reply_to_native_id=None) -> int | None:
        text = content if sender == self.bridge_name_for_chat(chat_id) else f"**{sender}:**\n{content}"

        sent_msg = None
        if len(content) > MESSAGE_CHUNK_LIMIT:
            for chunk in (content[i:i + MESSAGE_CHUNK_LIMIT] for i in range(0, len(content), MESSAGE_CHUNK_LIMIT)):
                chunk_text = chunk if sender == self.bridge_name_for_chat(chat_id) else f"**{sender}:**\n{chunk}"
                sent_msg = await self._app.send_message(chat_id, chunk_text, reply_to_message_id=reply_to_native_id)
                reply_to_native_id = None
        else: sent_msg = await self._app.send_message(chat_id, text, reply_to_message_id=reply_to_native_id)

        return sent_msg.id if sent_msg else None

    async def send_file(self, chat_id, file_path: str, file_ext: str, sender: str, reply_to_native_id=None) -> int | None:
        bridge = self.bridge_name_for_chat(chat_id)
        caption = "" if sender == bridge else f"**{sender}:**"
        attachment_kind = classify_attachment(file_path, file_ext)

        if os.path.getsize(file_path) > 8_388_608:
            msg = "File size is over 8MB, can't send it."
            text = msg if sender == bridge else f"**{sender}:** {msg}"
            await self._app.send_message(chat_id, text)
            return None

        sent_msg = None
        if attachment_kind == "photo": sent_msg = await self._app.send_photo(chat_id, file_path, caption=caption, reply_to_message_id=reply_to_native_id)
        elif attachment_kind == "video": sent_msg = await self._app.send_video(chat_id, file_path, caption=caption, reply_to_message_id=reply_to_native_id)
        elif attachment_kind == "audio": sent_msg = await self._app.send_audio(chat_id, file_path, caption=caption, reply_to_message_id=reply_to_native_id)
        elif attachment_kind == "voice": sent_msg = await self._app.send_voice(chat_id, file_path, caption=caption, reply_to_message_id=reply_to_native_id)
        else: sent_msg = await self._app.send_document(chat_id, file_path, caption=caption, reply_to_message_id=reply_to_native_id)

        return sent_msg.id if sent_msg else None

    # Lifecycle
    async def start(self) -> None:
        logger.info("Telegram platform starting…")
        async with self._app:
            logger.info("Telegram platform is alive")
            await asyncio.Future()

    async def stop(self) -> None:
        await self._app.stop()
