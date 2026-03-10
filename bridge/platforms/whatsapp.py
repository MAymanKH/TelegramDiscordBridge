"""
WhatsApp platform — receives messages from WhatsApp and sends to WhatsApp.
"""

import asyncio
import os
import mimetypes
from bridge.platforms.base import BasePlatform
from bridge.utils.config import platform_media_dir
from bridge.utils.media import get_unique_filepath, PHOTO_EXTENSIONS
from bridge.utils.logger import get_logger
from neonize.aioze.client import NewAClient
from neonize.aioze.events import MessageEv, ConnectedEv, PairStatusEv
from neonize.utils import build_jid
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
    Message,
    ContextInfo,
    ExtendedTextMessage,
)

logger = get_logger("whatsapp")

MESSAGE_CHUNK_LIMIT = 4096  # WhatsApp's limit is much higher than TG/Discord
FILE_SIZE_LIMIT = 64 * 1024 * 1024  # 64 MB WhatsApp media limit

def _jid_to_str(jid) -> str:
    """Convert a neonize JID object to a comparable string like '120363...@g.us'."""
    try: return f"{jid.User}@{jid.Server}"
    except AttributeError: return str(jid)

def _get_sender_name(message: MessageEv) -> str:
    """Extract a display name from a neonize MessageEv."""
    try:
        sender_jid = message.Info.MessageSource.Sender
        push_name = message.Info.Pushname
        if push_name: return push_name
        return sender_jid.User or "Someone"
    except AttributeError:
        return "Someone"

def _get_text(message: MessageEv) -> str | None:
    """Extract text content from a MessageEv, handling both plain and extended text."""
    msg = message.Message
    if msg.conversation: return msg.conversation
    if msg.extendedTextMessage and msg.extendedTextMessage.text: return msg.extendedTextMessage.text
    return None

def _get_caption(message: MessageEv) -> str | None:
    """Extract caption from media messages."""
    msg = message.Message
    if msg.imageMessage and msg.imageMessage.caption: return msg.imageMessage.caption
    if msg.videoMessage and msg.videoMessage.caption: return msg.videoMessage.caption
    if msg.documentMessage and msg.documentMessage.caption: return msg.documentMessage.caption
    return None

def _get_quoted_id(message: MessageEv) -> str | None:
    """Extract the quoted/replied-to message ID from context info."""
    msg = message.Message
    ctx = None
    if msg.extendedTextMessage and msg.extendedTextMessage.contextInfo.stanzaID:
        ctx = msg.extendedTextMessage.contextInfo
    elif msg.imageMessage and msg.imageMessage.contextInfo.stanzaID:
        ctx = msg.imageMessage.contextInfo
    elif msg.videoMessage and msg.videoMessage.contextInfo.stanzaID:
        ctx = msg.videoMessage.contextInfo
    elif msg.documentMessage and msg.documentMessage.contextInfo.stanzaID:
        ctx = msg.documentMessage.contextInfo
    elif msg.audioMessage and msg.audioMessage.contextInfo.stanzaID:
        ctx = msg.audioMessage.contextInfo
    if ctx and ctx.stanzaID: return ctx.stanzaID
    return None

def _is_reaction(message: MessageEv) -> bool:
    """Check if this message is a reaction."""
    rm = message.Message.reactionMessage
    return bool(rm and rm.text and rm.key and rm.key.ID)

def _has_media(message: MessageEv) -> bool:
    """Check if the message contains downloadable media."""
    msg = message.Message
    return bool(
        msg.imageMessage.URL
        or msg.videoMessage.URL
        or msg.audioMessage.URL
        or msg.documentMessage.URL
        or msg.stickerMessage.URL
    )

def _get_media_info(message: MessageEv) -> tuple[str, str]:
    """Return (file_name, file_extension) for a media message."""
    msg = message.Message
    if msg.imageMessage.URL:
        mime = msg.imageMessage.mimetype or "image/jpeg"
        ext = mimetypes.guess_extension(mime) or ".jpg"
        return "image", ext
    if msg.videoMessage.URL:
        mime = msg.videoMessage.mimetype or "video/mp4"
        ext = mimetypes.guess_extension(mime) or ".mp4"
        return "video", ext
    if msg.audioMessage.URL:
        ptt = msg.audioMessage.PTT
        mime = msg.audioMessage.mimetype or "audio/ogg"
        ext = ".ogg" if ptt else (mimetypes.guess_extension(mime) or ".mp3")
        return "voice" if ptt else "audio", ext
    if msg.documentMessage.URL:
        fname = msg.documentMessage.fileName or "document"
        name, ext = os.path.splitext(fname)
        if not ext:
            mime = msg.documentMessage.mimetype or ""
            ext = mimetypes.guess_extension(mime) or ""
        return name, ext
    if msg.stickerMessage.URL: return "sticker", ".webp"
    return "file", ""

class WhatsAppPlatform(BasePlatform):
    """Neonize-based WhatsApp bridge platform."""

    def __init__(self, platform_config: dict, bridges: list[dict], router=None):
        super().__init__("whatsapp", platform_config, bridges, router)
        session_db = "messages/whatsapp/session.db"
        os.makedirs(os.path.dirname(session_db), exist_ok=True)
        self._client = NewAClient(session_db)
        self._own_jid: str | None = None
        self._source_chats = [str(cid) for cid in self.my_chat_ids()]
        # Track message IDs sent by the bridge to prevent echo loops
        self._sent_ids: set[str] = set()
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Wire up neonize event handlers."""

        @self._client.event(ConnectedEv)
        async def _on_connected(client: NewAClient, ev: ConnectedEv):
            try:
                device = await client.get_me()
                self._own_jid = _jid_to_str(device.JID)
                logger.info("WhatsApp connected as %s", self._own_jid)
            except Exception:
                logger.info("WhatsApp connected")

        @self._client.event(PairStatusEv)
        async def _on_paired(client: NewAClient, ev: PairStatusEv):
            self._own_jid = _jid_to_str(ev.ID)
            logger.info("WhatsApp paired as %s", self._own_jid)

        @self._client.event(MessageEv)
        async def _on_message(client: NewAClient, message: MessageEv):
            await self._handle_incoming(client, message)

    # Incoming events
    async def _handle_incoming(self, client: NewAClient, message: MessageEv) -> None:
        try:
            chat_jid = message.Info.MessageSource.Chat
            chat_str = _jid_to_str(chat_jid)
        except AttributeError:
            return

        msg_id = message.Info.ID

        # Skip messages the bridge itself sent (echo prevention)
        if msg_id in self._sent_ids:
            self._sent_ids.discard(msg_id)
            if len(self._sent_ids) > 1000: self._sent_ids.clear()
            return

        bridge_name = self._bridge_name_for_jid(chat_str)
        if bridge_name is None: return

        sender = _get_sender_name(message)
        logger.info("Message from %s in %s (id=%s)", sender, bridge_name, msg_id)

        # Reaction
        if _is_reaction(message):
            await self._handle_reaction(message, bridge_name, sender)
            return

        quoted_id = _get_quoted_id(message)
        media_dir = platform_media_dir(self.name)

        try:
            # Media
            has_media = _has_media(message)
            if has_media:
                try:
                    file_name, file_ext = _get_media_info(message)
                    file_path = get_unique_filepath(media_dir, file_name, file_ext)
                    data = await client.download_any(message.Message)
                    with open(file_path, "wb") as f:
                        f.write(data)
                    logger.info("Downloaded media: %s", file_path)
                    await self.router.on_file(
                        self.name, bridge_name, msg_id,
                        file_path, file_ext, sender,
                        replied_to_msg_id=quoted_id,
                    )
                except Exception:
                    logger.warning("Failed to download media from %s", sender, exc_info=True)

            # Text (or caption for media)
            text = _get_text(message)
            caption = _get_caption(message)
            content = text or caption
            logger.info("Extracted content: text=%r, caption=%r, has_media=%s", text, caption, has_media)
            if content:
                await self.router.on_message(
                    self.name, bridge_name, msg_id,
                    content, sender,
                    replied_to_msg_id=quoted_id,
                )
            elif not has_media: logger.warning("No text or media found in message %s", msg_id)
        except Exception: logger.error("Error processing WhatsApp message %s", msg_id, exc_info=True)

    async def _handle_reaction(self, message: MessageEv, bridge_name: str, sender: str) -> None:
        reaction = message.Message.reactionMessage
        emoji = reaction.text
        reacted_msg_id = reaction.key.ID
        if not emoji or not reacted_msg_id: return
        logger.info("Reaction %s from %s on message %s", emoji, sender, reacted_msg_id)
        await self.router.on_reaction(self.name, bridge_name, reacted_msg_id, emoji, sender)

    def _bridge_name_for_jid(self, jid_str: str) -> str | None:
        """Match a JID string against configured bridge chat IDs."""
        for b in self.bridges:
            platforms = b.get("platforms", {})
            wa_id = platforms.get(self.name)
            if wa_id is not None and str(wa_id) == jid_str: return b["name"]
        return None

    def _jid_for_chat_id(self, chat_id) -> object:
        """Convert a chat_id (string like '120363...@g.us') to a neonize JID."""
        parts = str(chat_id).split("@")
        if len(parts) == 2: return build_jid(parts[0], parts[1])
        return build_jid(str(chat_id))

    # Outbound messaging
    def _build_context_info(self, chat_id, reply_to_native_id) -> ContextInfo:
        """Build a ContextInfo for quoting a message by its native ID."""
        jid_str = str(chat_id)
        # participant = who sent the quoted message; use our own JID since
        # bridged messages are sent by our WhatsApp account.
        participant = self._own_jid or jid_str
        return ContextInfo(
            stanzaID=str(reply_to_native_id),
            participant=participant,
            remoteJID=jid_str,
        )

    async def send_text(self, chat_id, content: str, sender: str, reply_to_native_id=None) -> str | None:
        jid = self._jid_for_chat_id(chat_id)
        bridge = self.bridge_name_for_chat(chat_id)
        text = content if sender == bridge else f"*{sender}:*\n{content}"

        sent = None
        if reply_to_native_id:
            ctx = self._build_context_info(chat_id, reply_to_native_id)
            msg = Message(extendedTextMessage=ExtendedTextMessage(text=text, contextInfo=ctx))
            sent = await self._client.send_message(jid, msg)
        else:
            # Handle chunking for long messages
            if len(text) > MESSAGE_CHUNK_LIMIT:
                for chunk in (text[i:i + MESSAGE_CHUNK_LIMIT] for i in range(0, len(text), MESSAGE_CHUNK_LIMIT)):
                    sent = await self._client.send_message(jid, chunk)
            else:
                sent = await self._client.send_message(jid, text)

        if sent and sent.ID: self._sent_ids.add(sent.ID)
        return sent.ID if sent else None

    async def send_file(self, chat_id, file_path: str, file_ext: str, sender: str, reply_to_native_id=None) -> str | None:
        jid = self._jid_for_chat_id(chat_id)
        bridge = self.bridge_name_for_chat(chat_id)
        caption = "" if sender == bridge else f"*{sender}:*"

        if os.path.getsize(file_path) > FILE_SIZE_LIMIT:
            msg = "File size is over 64MB, can't send it."
            text = msg if sender == bridge else f"*{sender}:* {msg}"
            await self._client.send_message(jid, text)
            return None

        sent = None
        try:
            if file_ext in PHOTO_EXTENSIONS: sent = await self._client.send_image(jid, file_path, caption=caption)
            elif file_ext == ".mp4": sent = await self._client.send_video(jid, file_path, caption=caption)
            elif file_ext in (".mp3", ".ogg"): sent = await self._client.send_audio(jid, file_path, ptt=file_ext == ".ogg")
            elif file_ext == ".webp": sent = await self._client.send_sticker(jid, file_path)
            else:
                filename = os.path.basename(file_path)
                sent = await self._client.send_document(jid, file_path, caption=caption, filename=filename)
        except Exception:
            logger.warning("Failed to send file to WhatsApp", exc_info=True)
            return None

        if sent and sent.ID: self._sent_ids.add(sent.ID)
        return sent.ID if sent else None

    # Lifecycle
    async def start(self) -> None:
        logger.info("WhatsApp platform starting…")
        await self._client.connect()
        await self._client.idle()

    async def stop(self) -> None:
        await self._client.stop()
