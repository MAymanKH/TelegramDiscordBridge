"""
Telegram platform — receives messages from Telegram and sends to Telegram.
"""

import asyncio
import html
import os
from pyrogram import Client, enums, types
from pyrogram.enums import ParseMode
from bridge import database
from bridge.platforms.base import BasePlatform
from bridge.utils.config import (
    platform_media_dir, normalize_mention_filters, is_mention_only,
    normalize_user_ids, parse_upload_limit_mb,
    effective_mention_filters, effective_always_forward_user_ids,
    effective_destination_size_limit_bytes,
    bridge_digest_config,
)
from bridge.utils.media import classify_attachment, get_unique_filepath, safe_basename
from bridge.utils.transcode import compress_to_fit
from bridge.utils.logger import get_logger, safe_for_log

DEFAULT_DESTINATION_SIZE_LIMIT_MB = 50  # Telegram bot API hard cap when this platform is the bridge destination

# Pre-download cap for incoming media (when this platform is the source).
# Source-side downloads are skipped for files larger than this multiple of
# the platform's own destination_size_limit_bytes. Generous enough that the
# transcoder still has room to shrink things, hard enough to refuse the
# obvious DoS (someone sending the bot a 4 GB file).
INCOMING_DOWNLOAD_FACTOR = 4

logger = get_logger("telegram")

# Track processed media groups to avoid duplicate downloads
_processed_media_groups: set[str] = set()

MESSAGE_CHUNK_LIMIT = 1800

def _format_user_name(user, fallback: str) -> str:
    """Render a Pyrogram User object as a display name."""
    try:
        first = user.first_name or ""
        last = user.last_name or ""
        name = f"{first} {last}".strip()
        return name if name else (user.username or fallback)
    except AttributeError:
        return fallback


def _get_sender_name(message: types.Message, fallback: str = "Unknown") -> str:
    """Extract a display name from a Pyrogram message.

    For forwarded messages the ORIGINAL author's name is returned (so the
    bridge faithfully attributes the content, not the relayer). Telegram
    exposes this via ``forward_from`` (a User), ``forward_from_chat`` (a
    channel/group), or ``forward_sender_name`` (a string when the original
    author has hidden their account). Falls back to the direct sender."""
    # Forward attribution takes priority.
    fwd_user = getattr(message, "forward_from", None)
    if fwd_user is not None:
        name = _format_user_name(fwd_user, "")
        if name: return name
    fwd_chat = getattr(message, "forward_from_chat", None)
    if fwd_chat is not None:
        title = getattr(fwd_chat, "title", None)
        if title: return title
    fwd_sender_name = getattr(message, "forward_sender_name", None)
    if fwd_sender_name: return fwd_sender_name
    # Direct sender.
    if message.from_user is not None:
        return _format_user_name(message.from_user, fallback)
    return fallback

def _get_media_size(msg: types.Message) -> int | None:
    """Best-effort byte size of *msg*'s attachment, if any.

    Returns None when the message has no media or the size attribute isn't
    populated (some sticker/voice variants). Used pre-download to refuse
    obviously-too-big files without spending bandwidth on them."""
    for attr in ("document", "video", "audio", "voice", "photo", "sticker", "animation"):
        media = getattr(msg, attr, None)
        if media is None: continue
        size = getattr(media, "file_size", None)
        if size: return int(size)
    return None


def _get_media_info(msg: types.Message) -> tuple[str, str]:
    """Return ``(file_name, file_extension)`` from a Pyrogram message.

    The document branch sanitizes the user-supplied ``file_name`` through
    :func:`safe_basename` so a malicious sender cannot inject path-traversal
    sequences into the eventual write path."""
    if msg.document:
        raw = msg.document.file_name or "document"
        name, ext = os.path.splitext(raw)
        return safe_basename(name, fallback="document"), ext  # ext gets re-sanitized downstream
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
        self._mention_filters = normalize_mention_filters(platform_config.get("mention_filter"))
        logger.info("Telegram mention_filters loaded: %r", self._mention_filters)
        self._always_forward_user_ids = normalize_user_ids(platform_config.get("always_forward_user_ids"))
        logger.info("Telegram always_forward_user_ids loaded: %r", sorted(self._always_forward_user_ids))
        self._destination_size_limit_bytes = parse_upload_limit_mb(platform_config.get("destination_size_limit_mb"), DEFAULT_DESTINATION_SIZE_LIMIT_MB)
        logger.info("Telegram destination size limit: %d bytes (%.1f MB)",
                    self._destination_size_limit_bytes, self._destination_size_limit_bytes / (1024 * 1024))

        if bot_token: self._app = Client("my_bot", api_id=api_id, api_hash=api_hash, bot_token=bot_token)
        elif phone_number: self._app = Client("my_bot", api_id=api_id, api_hash=api_hash, phone_number=phone_number)
        else: self._app = Client("my_bot", api_id=api_id, api_hash=api_hash)

        self._register_handlers()

    def escape_user_text(self, s: str) -> str:
        """HTML-escape; Telegram outbound runs in HTML parse mode."""
        return html.escape(s)

    def format_digest_body(self, entries: list[tuple[float, str, str]]) -> str:
        """Render the digest body as Telegram-flavored HTML.

        Renders per-sender blocks with a bold name + timestamp header and a
        ``<blockquote>`` for the content. Consecutive messages from the same
        sender share one header so a burst doesn't repeat ``Name · HH:MM``
        on every line. User-supplied sender names and content are passed
        through ``html.escape`` so they cannot inject tags or Markdown link
        syntax."""
        import time
        # group: list of [sender, first_ts, [content_lines]]
        groups: list[list] = []
        for ts, sender, content in entries:
            if not content or not content.strip(): continue
            lines = content.splitlines() or [content]
            if groups and groups[-1][0] == sender:
                groups[-1][2].extend(lines)
            else:
                groups.append([sender, ts, list(lines)])
        blocks: list[str] = []
        for sender, ts, lines in groups:
            ts_str = time.strftime("%H:%M", time.localtime(ts))
            header = f"<b>{html.escape(sender)}</b> · {ts_str}"
            quote_lines = [html.escape(line) for line in lines]
            quoted = "<blockquote>" + "\n".join(quote_lines) + "</blockquote>"
            blocks.append(f"{header}\n{quoted}")
        return "\n\n".join(blocks)

    def apply_config(self, platform_config: dict) -> None:
        super().apply_config(platform_config)
        new_filters = normalize_mention_filters(platform_config.get("mention_filter"))
        if new_filters != self._mention_filters:
            logger.info("Telegram mention_filters updated: %r → %r", self._mention_filters, new_filters)
            self._mention_filters = new_filters
        new_users = normalize_user_ids(platform_config.get("always_forward_user_ids"))
        if new_users != self._always_forward_user_ids:
            logger.info(
                "Telegram always_forward_user_ids updated: %r → %r",
                sorted(self._always_forward_user_ids), sorted(new_users),
            )
            self._always_forward_user_ids = new_users
        new_limit = parse_upload_limit_mb(platform_config.get("destination_size_limit_mb"), DEFAULT_DESTINATION_SIZE_LIMIT_MB)
        if new_limit != self._destination_size_limit_bytes:
            logger.info("Telegram destination size limit updated: %d → %d bytes", self._destination_size_limit_bytes, new_limit)
            self._destination_size_limit_bytes = new_limit

    def _register_handlers(self) -> None:
        """Wire up Pyrogram event handlers.

        No static chat allowlist — the handler dispatches via
        ``bridge_name_for_source_chat`` so adding/removing chats from
        ``settings.yaml`` takes effect on reload without re-registering.
        """

        async def _on_message_inner(client: Client, message: types.Message) -> None:
            bridge_name = self.bridge_name_for_source_chat(message.chat.id)
            if bridge_name is None: return
            bridge = self.find_bridge_by_name(bridge_name) or {}

            # Digest mode: bypass all per-user / mention filters so the
            # buffered thread captures the full conversation. The router
            # decides what to do with on_message (immediate vs. buffered).
            if bridge_digest_config(bridge):
                await self._handle_incoming_message(client, message)
                return

            # always_forward_user_ids is a BYPASS list — users on it skip
            # the mention_filter check. An empty list does NOT disable the
            # filter; mention_filter still applies to all senders.
            eff_always_forward = effective_always_forward_user_ids(bridge, self._always_forward_user_ids)
            sender_id = getattr(message.from_user, "id", None) if message.from_user else None
            if sender_id is not None and sender_id in eff_always_forward:
                logger.info("Always-forward user %s — bypassing mention filter", sender_id)
                await self._handle_incoming_message(client, message)
                return

            # Apply mention_filter if configured.
            eff_filters = effective_mention_filters(bridge, self._mention_filters)
            if eff_filters and message.chat.type != enums.ChatType.PRIVATE:
                text = (message.text or message.caption or "").lower()
                reply = message.reply_to_message
                reply_text = (reply.text or reply.caption or "").lower() if reply else ""
                text_has_mention = any(f in text for f in eff_filters)
                parent_has_mention = any(f in reply_text for f in eff_filters)
                if not text_has_mention and not parent_has_mention:
                    logger.info("Filtered out (no mention in text or reply): %r", text[:120])
                    return
                logger.info("Mention filter matched (text or reply context): %r", text[:120])
                if text_has_mention and reply:
                    await self._maybe_forward_parent(client, reply)
                if text_has_mention and not message.media and is_mention_only(text, eff_filters):
                    logger.info("Skipping mention-only message %s (no content beyond mention)", message.id)
                    return
            await self._handle_incoming_message(client, message)

        @self._app.on_message()
        async def _on_message(client: Client, message: types.Message):
            try:
                await _on_message_inner(client, message)
            except Exception as exc:
                # Last-resort guard. Pyrogram already logs handler
                # exceptions, but having ours under the bridge logger
                # makes monitoring/grep simpler.
                logger.error(
                    "Telegram on_message crashed for chat %s: %s",
                    getattr(message.chat, "id", "?"), exc, exc_info=True,
                )

        @self._app.on_message_reaction_updated()
        async def _on_reaction(client: Client, update: types.MessageReactions):
            try:
                if self.bridge_name_for_source_chat(update.chat.id) is None: return
                await self._handle_reaction(client, update)
            except Exception as exc:
                logger.error("Telegram on_reaction crashed: %s", exc, exc_info=True)

    # Incoming events
    async def _maybe_forward_parent(self, client: Client, parent: types.Message) -> None:
        """Forward *parent* (the message that was replied to) so Discord has
        context, unless it's already been bridged in either direction."""
        bridge_name = self.bridge_name_for_source_chat(parent.chat.id)
        if bridge_name is None: return
        if self.router is None: return
        if await database.is_message_bridged(self.router.db_path, bridge_name, self.name, parent.id):
            logger.info("Parent msg %s already bridged, skipping retro-forward", parent.id)
            return
        logger.info("Retro-forwarding parent msg %s for reply context", parent.id)
        await self._handle_incoming_message(client, parent)

    def _incoming_cap_for(self, bridge: dict) -> int:
        """Per-bridge hard cap on inbound file size.

        Derived from the bridge-effective ``destination_size_limit_bytes``
        (which honors per-bridge overrides) multiplied by
        :data:`INCOMING_DOWNLOAD_FACTOR`. A single setting covers both
        directions: above this size we refuse to download. The transcoder
        is invoked later for files that fit but still exceed a destination's
        limit, so the factor leaves room for shrinking.
        """
        eff = effective_destination_size_limit_bytes(bridge, self._destination_size_limit_bytes)
        return eff * INCOMING_DOWNLOAD_FACTOR

    async def _handle_incoming_message(self, client: Client, message: types.Message) -> None:
        bridge_name = self.bridge_name_for_source_chat(message.chat.id)
        if bridge_name is None: return

        sender = _get_sender_name(message, fallback=bridge_name)
        logger.info("Message from %s in %s", safe_for_log(sender), bridge_name)
        media_dir = platform_media_dir(self.name)
        # For caption-skip below, use the bridge-effective filter set so a
        # per-bridge mention_filter is honored even on media captions.
        bridge = self.find_bridge_by_name(bridge_name) or {}
        eff_filters = effective_mention_filters(bridge, self._mention_filters)
        # Source-platform timestamp for the digest ordering. message.date is
        # a datetime in UTC; pass as epoch seconds so cross-platform sorts
        # share a units. Falls back to wall-clock if Pyrogram didn't fill it in.
        source_ts = message.date.timestamp() if message.date else None

        # Media group (deduplicate)
        if message.media_group_id:
            if message.media_group_id in _processed_media_groups: return
            _processed_media_groups.add(message.media_group_id)
            if len(_processed_media_groups) > 1000: _processed_media_groups.clear()

            media_group = await self._app.get_media_group(message.chat.id, message.id)
            cap = self._incoming_cap_for(bridge)
            for i, item in enumerate(media_group):
                size = _get_media_size(item) or 0
                if size > cap:
                    logger.warning(
                        "Skipping oversized item %d in media group from %s: %.1f MB > %.1f MB cap",
                        i, safe_for_log(sender), size / (1024 * 1024), cap / (1024 * 1024),
                    )
                    continue
                await asyncio.sleep(0.5)
                file_name, file_type = _get_media_info(item)
                # Route through get_unique_filepath so the path-traversal guard
                # applies — never os.path.join with raw filenames here.
                file_path = get_unique_filepath(media_dir, f"{file_name}_({i})", file_type)
                # Download can fail (disk full, network blip). Skip the
                # offending item and keep going on the rest of the album.
                try:
                    await client.download_media(item, file_name=file_path)
                except Exception as exc:
                    logger.error(
                        "Failed to download Telegram album item %d from %s: %s — skipping",
                        i, safe_for_log(sender), exc,
                    )
                    try: os.remove(file_path)
                    except OSError: pass
                    continue
                # Verify actual disk size against the cap (the pre-download
                # check trusted protocol-reported metadata; verify reality).
                actual = os.path.getsize(file_path) if os.path.isfile(file_path) else 0
                if actual > cap:
                    logger.warning(
                        "Album item %d from %s downloaded %.1f MB > cap %.1f MB; discarding",
                        i, safe_for_log(sender),
                        actual / (1024 * 1024), cap / (1024 * 1024),
                    )
                    try: os.remove(file_path)
                    except OSError: pass
                    continue
                await self.router.on_file(
                    self.name, bridge_name, message.id,
                    file_path, file_type, sender,
                    replied_to_msg_id=message.reply_to_message_id,
                )
            if message.caption and not is_mention_only(message.caption.lower(), eff_filters):
                await self.router.on_message(
                    self.name, bridge_name, message.id,
                    message.caption, sender,
                    replied_to_msg_id=message.reply_to_message_id,
                )

        # Single attachment
        elif message.media:
            cap = self._incoming_cap_for(bridge)
            size = _get_media_size(message) or 0
            if size > cap:
                logger.warning(
                    "Skipping oversized attachment from %s: %.1f MB > %.1f MB cap (no download)",
                    safe_for_log(sender), size / (1024 * 1024), cap / (1024 * 1024),
                )
                # Still allow the caption to flow if it has real content.
                if message.caption and not is_mention_only(message.caption.lower(), eff_filters):
                    await self.router.on_message(
                        self.name, bridge_name, message.id,
                        message.caption, sender,
                        replied_to_msg_id=message.reply_to_message_id,
                    )
                return
            file_name, file_type = _get_media_info(message)
            file_path = get_unique_filepath(media_dir, file_name, file_type)
            # Download can fail (disk full, network). Drop the attachment
            # silently rather than crashing the handler. Caption (if any)
            # still flows through so context isn't lost.
            try:
                await client.download_media(message, file_path)
            except Exception as exc:
                logger.error(
                    "Failed to download Telegram attachment from %s: %s — skipping",
                    safe_for_log(sender), exc,
                )
                try: os.remove(file_path)
                except OSError: pass
                if message.caption and not is_mention_only(message.caption.lower(), eff_filters):
                    await self.router.on_message(
                        self.name, bridge_name, message.id,
                        message.caption, sender,
                        replied_to_msg_id=message.reply_to_message_id,
                    )
                return
            # Verify actual disk size against the cap; the pre-download
            # check trusted protocol-reported metadata.
            actual = os.path.getsize(file_path) if os.path.isfile(file_path) else 0
            if actual > cap:
                logger.warning(
                    "Attachment from %s downloaded %.1f MB > cap %.1f MB; discarding",
                    safe_for_log(sender),
                    actual / (1024 * 1024), cap / (1024 * 1024),
                )
                try: os.remove(file_path)
                except OSError: pass
                # Still let any caption through (it has real content).
                if message.caption and not is_mention_only(message.caption.lower(), eff_filters):
                    await self.router.on_message(
                        self.name, bridge_name, message.id,
                        message.caption, sender,
                        replied_to_msg_id=message.reply_to_message_id,
                    )
                return
            await self.router.on_file(
                self.name, bridge_name, message.id,
                file_path, file_type, sender,
                replied_to_msg_id=message.reply_to_message_id,
                source_ts=source_ts,
            )
            if message.caption and not is_mention_only(message.caption.lower(), eff_filters):
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
                source_ts=source_ts,
            )

    async def _handle_reaction(self, client: Client, update: types.MessageReactions) -> None:
        bridge_name = self.bridge_name_for_source_chat(update.chat.id)
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
        """Send text to a Telegram chat in HTML parse mode.

        For external user content (``sender != bridge_name``) we wrap in
        ``<b>sender:</b>`` and ``html.escape`` both fields — this neutralizes
        Markdown / link injection attacks (a user setting their display name
        to ``[Support](https://phish)`` would otherwise render as a
        clickable phishing link in the destination chat).

        For internal-composed content (digest bodies, reaction notices)
        ``sender == bridge_name`` — the caller has already produced HTML-safe
        text and we send it as-is.
        """
        bridge_name = self.bridge_name_for_chat(chat_id)
        sender_is_bridge = sender == bridge_name

        def _wrap(text_chunk: str) -> str:
            if sender_is_bridge: return text_chunk
            return f"<b>{html.escape(sender)}:</b>\n{html.escape(text_chunk)}"

        sent_msg = None
        try:
            if len(content) > MESSAGE_CHUNK_LIMIT:
                for chunk in (content[i:i + MESSAGE_CHUNK_LIMIT] for i in range(0, len(content), MESSAGE_CHUNK_LIMIT)):
                    sent_msg = await self._app.send_message(
                        chat_id, _wrap(chunk),
                        reply_to_message_id=reply_to_native_id,
                        parse_mode=ParseMode.HTML,
                    )
                    reply_to_native_id = None
            else:
                sent_msg = await self._app.send_message(
                    chat_id, _wrap(content),
                    reply_to_message_id=reply_to_native_id,
                    parse_mode=ParseMode.HTML,
                )
        except Exception as exc:
            logger.error("Telegram send_text to %s failed: %s", chat_id, exc, exc_info=True)
            return None

        return sent_msg.id if sent_msg else None

    async def send_file(self, chat_id, file_path: str, file_ext: str, sender: str, reply_to_native_id=None) -> int | None:
        bridge = self.bridge_name_for_chat(chat_id)
        # HTML-escape the user-supplied sender name; same rationale as in send_text.
        caption = "" if sender == bridge else f"<b>{html.escape(sender)}:</b>"

        actual_path = file_path
        transcoded_path: str | None = None
        original_size = os.path.getsize(file_path)
        # Resolve bridge-effective destination size limit so per-bridge
        # overrides of destination_size_limit_mb apply (e.g. boosted Discord
        # server vs. unboosted, or different Telegram chats with different
        # quotas).
        bridge_dict = self.find_bridge_by_name(bridge) or {} if bridge else {}
        limit = effective_destination_size_limit_bytes(bridge_dict, self._destination_size_limit_bytes)

        if original_size > limit:
            transcoded_path = await compress_to_fit(file_path, target_bytes=limit)
            if transcoded_path:
                new_size = os.path.getsize(transcoded_path)
                logger.info(
                    "Transcoded %s (%.2f MB → %.2f MB) to fit Telegram destination size limit",
                    os.path.basename(file_path),
                    original_size / (1024 * 1024), new_size / (1024 * 1024),
                )
                actual_path = transcoded_path
            else:
                limit_mb = limit // (1024 * 1024)
                msg = f"File is {original_size / (1024 * 1024):.1f} MB, over the {limit_mb} MB limit, and couldn't be transcoded."
                text = msg if sender == bridge else f"<b>{html.escape(sender)}:</b> {html.escape(msg)}"
                await self._app.send_message(chat_id, text, parse_mode=ParseMode.HTML)
                return None

        attachment_kind = classify_attachment(actual_path, file_ext)
        sent_msg = None
        try:
            if attachment_kind == "photo": sent_msg = await self._app.send_photo(chat_id, actual_path, caption=caption, parse_mode=ParseMode.HTML, reply_to_message_id=reply_to_native_id)
            elif attachment_kind == "video": sent_msg = await self._app.send_video(chat_id, actual_path, caption=caption, parse_mode=ParseMode.HTML, reply_to_message_id=reply_to_native_id)
            elif attachment_kind == "audio": sent_msg = await self._app.send_audio(chat_id, actual_path, caption=caption, parse_mode=ParseMode.HTML, reply_to_message_id=reply_to_native_id)
            elif attachment_kind == "voice": sent_msg = await self._app.send_voice(chat_id, actual_path, caption=caption, parse_mode=ParseMode.HTML, reply_to_message_id=reply_to_native_id)
            else: sent_msg = await self._app.send_document(chat_id, actual_path, caption=caption, parse_mode=ParseMode.HTML, reply_to_message_id=reply_to_native_id)
        except Exception as exc:
            logger.error("Telegram send_file to %s failed: %s", chat_id, exc, exc_info=True)
            return None
        finally:
            if transcoded_path:
                try: os.remove(transcoded_path)
                except OSError: pass

        return sent_msg.id if sent_msg else None

    # Lifecycle
    async def start(self) -> None:
        logger.info("Telegram platform starting…")
        async with self._app:
            self._secure_session_files()
            logger.info("Telegram platform is alive")
            await asyncio.Future()

    def _secure_session_files(self) -> None:
        """Restrict the Pyrogram session file to owner read/write only.

        ``my_bot.session`` (created by Pyrogram in CWD) holds the auth
        keys — anyone reading it can fully impersonate this bot. Pyrogram
        creates it with the process umask (typically ``0o644``); we
        tighten to ``0o600`` after the file exists. POSIX-only."""
        if os.name != "posix": return
        for name in ("my_bot.session", "my_bot.session-journal"):
            try:
                if os.path.isfile(name):
                    os.chmod(name, 0o600)
            except OSError as exc:
                logger.warning("chmod %s failed: %s", name, exc)

    async def stop(self) -> None:
        await self._app.stop()
