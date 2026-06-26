"""
Discord platform — receives messages from Discord and sends to Discord.
"""

import asyncio
import os
import random
import re
import discord
from discord.ext import commands
from bridge import database
from bridge.platforms.base import BasePlatform
from bridge.utils.config import (
    platform_media_dir, normalize_mention_filters, is_mention_only,
    normalize_user_ids, parse_upload_limit_mb,
    effective_mention_filters, effective_always_forward_user_ids,
    effective_destination_size_limit_bytes,
    bridge_digest_config, platform_voice_transcription_config,
)
from bridge.transcription import get_transcriber
from bridge.utils.media import get_unique_filepath
from bridge.utils.transcode import compress_to_fit
from bridge.utils.logger import get_logger, safe_for_log

DEFAULT_DESTINATION_SIZE_LIMIT_MB = 10  # Discord upload limit when this platform is the bridge destination

# Pre-download cap for incoming attachments (when this platform is the source).
# Source-side attachment downloads are skipped for files larger than this
# multiple of the platform's destination_size_limit_bytes. Generous enough
# for the transcoder to shrink reasonable files; refuses obvious DoS.
INCOMING_DOWNLOAD_FACTOR = 4

logger = get_logger("discord")

MESSAGE_CHUNK_LIMIT = 1800

class DiscordPlatform(BasePlatform):
    """discord.py-based Discord bridge platform."""

    def __init__(self, platform_config: dict, bridges: list[dict], router=None):
        super().__init__("discord", platform_config, bridges, router)
        self._token = platform_config["token"]
        app_id = platform_config["app_id"]
        self._mention_filters = normalize_mention_filters(platform_config.get("mention_filter"))
        logger.info("Discord mention_filters loaded: %r", self._mention_filters)
        self._always_forward_user_ids = normalize_user_ids(platform_config.get("always_forward_user_ids"))
        logger.info("Discord always_forward_user_ids loaded: %r", sorted(self._always_forward_user_ids))
        self._destination_size_limit_bytes = parse_upload_limit_mb(platform_config.get("destination_size_limit_mb"), DEFAULT_DESTINATION_SIZE_LIMIT_MB)
        logger.info("Discord destination size limit: %d bytes (%.1f MB)",
                    self._destination_size_limit_bytes, self._destination_size_limit_bytes / (1024 * 1024))
        self._bot = commands.Bot(
            command_prefix=".",
            intents=discord.Intents.all(),
            application_id=app_id,
        )
        self._ready_event = asyncio.Event()
        self._register_handlers()

    def format_digest_body(self, entries: list[tuple[float, str, str]]) -> str:
        """Render the digest body as Discord-flavored Markdown.

        Consecutive messages from the same sender share one header.
        Discord doesn't auto-link ``[text](url)`` in regular message
        content, so injection from there is benign — but we still escape
        the most disruptive Markdown chars in user-supplied sender names
        to stop visual confusion (someone setting their nick to ``**evil``)."""
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
            safe_sender = sender.replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")
            header = f"**{safe_sender}** · {ts_str}"
            quoted = "\n".join(f"> {line}" for line in lines)
            blocks.append(f"{header}\n{quoted}")
        return "\n\n".join(blocks)

    def apply_config(self, platform_config: dict) -> None:
        super().apply_config(platform_config)
        new_filters = normalize_mention_filters(platform_config.get("mention_filter"))
        if new_filters != self._mention_filters:
            logger.info("Discord mention_filters updated: %r → %r", self._mention_filters, new_filters)
            self._mention_filters = new_filters
        new_users = normalize_user_ids(platform_config.get("always_forward_user_ids"))
        if new_users != self._always_forward_user_ids:
            logger.info(
                "Discord always_forward_user_ids updated: %r → %r",
                sorted(self._always_forward_user_ids), sorted(new_users),
            )
            self._always_forward_user_ids = new_users
        new_limit = parse_upload_limit_mb(platform_config.get("destination_size_limit_mb"), DEFAULT_DESTINATION_SIZE_LIMIT_MB)
        if new_limit != self._destination_size_limit_bytes:
            logger.info("Discord destination size limit updated: %d → %d bytes", self._destination_size_limit_bytes, new_limit)
            self._destination_size_limit_bytes = new_limit

    def _register_handlers(self) -> None:
        """Wire up discord.py event handlers."""

        @self._bot.event
        async def on_ready():
            logger.info("Discord bot is ready")
            self._ready_event.set()

        async def _on_message_safe(message: discord.Message) -> None:
            if message.author == self._bot.user: return

            bridge_name = self._resolve_bridge_for_message(message)
            bridge = self.find_bridge_by_name(bridge_name) if bridge_name else None
            bridge = bridge or {}

            # Voice transcription runs BEFORE the mention/whitelist gate so
            # reply_with_transcript also fires for voice messages that the
            # filter would otherwise drop.
            transcript = await self._maybe_transcribe(message)

            # Digest mode: bypass per-user / mention filters so the buffered
            # thread captures every message in the chat.
            if bridge_name and bridge_digest_config(bridge):
                await self._handle_incoming_message(message, transcript=transcript)
                return

            # always_forward_user_ids is a BYPASS list — users on it skip
            # the mention_filter check. An empty list does NOT disable the
            # filter; mention_filter still applies to all senders.
            eff_always_forward = effective_always_forward_user_ids(bridge, self._always_forward_user_ids)
            if message.author.id in eff_always_forward:
                logger.info("Always-forward user %s — bypassing mention filter", message.author.id)
                await self._handle_incoming_message(message, transcript=transcript)
                return

            # Apply mention_filter if configured.
            eff_filters = effective_mention_filters(bridge, self._mention_filters)
            if eff_filters and not isinstance(message.channel, discord.DMChannel):
                text = (message.content or "").lower()
                parent = await self._resolve_parent(message)
                parent_text = (parent.content or "").lower() if parent else ""
                text_has_mention = any(f in text for f in eff_filters)
                parent_has_mention = any(f in parent_text for f in eff_filters)
                if not text_has_mention and not parent_has_mention:
                    logger.info("Filtered out (no mention in text or reply): %r", text[:120])
                    return
                logger.info("Mention filter matched (text or reply context): %r", text[:120])
                if text_has_mention and parent:
                    await self._maybe_forward_parent(parent)
                if text_has_mention and not message.attachments and is_mention_only(text, eff_filters):
                    logger.info("Skipping mention-only message %s (no content beyond mention)", message.id)
                    return
            await self._handle_incoming_message(message, transcript=transcript)

        @self._bot.event
        async def on_message(message: discord.Message):
            try:
                await _on_message_safe(message)
            except Exception as exc:
                # Last-resort guard: any unhandled exception during message
                # handling is logged but doesn't propagate. discord.py would
                # log it itself, but we want a single, consistent log line
                # under our logger so monitoring tools see one stream.
                logger.error(
                    "Discord on_message crashed for %s: %s",
                    safe_for_log(getattr(message.author, "display_name", "?")),
                    exc, exc_info=True,
                )

        @self._bot.event
        async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
            try:
                await self._handle_reaction(payload)
            except Exception as exc:
                logger.error("Discord on_reaction crashed: %s", exc, exc_info=True)

    # Incoming events
    async def _resolve_parent(self, message: discord.Message) -> discord.Message | None:
        """Return the message *message* is replying to, or ``None``."""
        ref = message.reference
        if not ref or not isinstance(ref.message_id, int): return None
        if isinstance(ref.resolved, discord.Message): return ref.resolved
        try:
            return await message.channel.fetch_message(ref.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    # Discord stores mentions as raw markup tokens. Forwarded as plain text
    # to Telegram, "<@123456789>" is unreadable; resolve to "@displayname"
    # (or fall back to the ID if not cached).
    _USER_MENTION_RE = re.compile(r"<@!?(\d+)>")
    _CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
    _ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
    _EMOJI_RE = re.compile(r"<a?:([^:]+):\d+>")

    def _humanize_mentions(self, text: str) -> str:
        """Replace Discord raw mention markup with readable text."""
        if not text: return text

        def user_repl(m: re.Match) -> str:
            uid = int(m.group(1))
            user = self._bot.get_user(uid)
            if user is not None:
                return "@" + (getattr(user, "display_name", None) or getattr(user, "name", str(uid)))
            return f"@{uid}"

        def channel_repl(m: re.Match) -> str:
            cid = int(m.group(1))
            ch = self._bot.get_channel(cid)
            name = getattr(ch, "name", None) if ch is not None else None
            return f"#{name}" if name else f"#{cid}"

        def role_repl(m: re.Match) -> str:
            # We don't have a global role index; show the raw id so it's at
            # least obvious this is a role mention, not a user.
            return f"@role:{m.group(1)}"

        def emoji_repl(m: re.Match) -> str:
            return f":{m.group(1)}:"

        text = self._USER_MENTION_RE.sub(user_repl, text)
        text = self._CHANNEL_MENTION_RE.sub(channel_repl, text)
        text = self._ROLE_MENTION_RE.sub(role_repl, text)
        text = self._EMOJI_RE.sub(emoji_repl, text)
        return text

    @staticmethod
    def _resolve_sender_name(message: "discord.Message") -> str:
        """Return the display name to attribute *message* to.

        For "Forward Message" forwards we prefer the ORIGINAL author from
        the snapshot when discord.py exposes one (Discord's API omits the
        snapshot's author in some cases — when unavailable, we fall back
        to the forwarder so the bridge always has *some* attribution).
        For regular messages, just the author's display name."""
        snapshots = getattr(message, "message_snapshots", None) or []
        if snapshots:
            snap = snapshots[0]
            snap_author = getattr(snap, "author", None)
            if snap_author is not None:
                # discord.py exposes display_name on User/Member; some snapshot
                # author objects only carry .name (global username). Try both.
                original = (
                    getattr(snap_author, "display_name", None)
                    or getattr(snap_author, "global_name", None)
                    or getattr(snap_author, "name", None)
                )
                if original: return original
        return message.author.display_name

    def _resolve_bridge_for_message(self, message: discord.Message) -> str | None:
        """Return the bridge name that *message* belongs to.

        For guild channel messages this is keyed by ``channel.id``. For DMs
        we fall back to ``author.id`` so users can configure a DM bridge by
        putting their own Discord user ID in ``settings.yaml`` — the DM
        channel ID is not exposed in the Discord UI."""
        bridge_name = self.bridge_name_for_source_chat(message.channel.id)
        if bridge_name is None and isinstance(message.channel, discord.DMChannel):
            bridge_name = self.bridge_name_for_source_chat(message.author.id)
        return bridge_name

    async def _maybe_forward_parent(self, parent: discord.Message) -> None:
        """Forward *parent* (the message that was replied to) so the other
        side has context, unless it's already been bridged in either direction."""
        bridge_name = self._resolve_bridge_for_message(parent)
        if bridge_name is None: return
        if self.router is None: return
        if await database.is_message_bridged(self.router.db_path, bridge_name, self.name, parent.id):
            logger.info("Parent msg %s already bridged, skipping retro-forward", parent.id)
            return
        logger.info("Retro-forwarding parent msg %s for reply context", parent.id)
        await self._handle_incoming_message(parent)

    @staticmethod
    def _voice_attachment(message: discord.Message) -> "discord.Attachment | None":
        """Return the Discord voice-message attachment, or None.

        Discord marks voice messages by populating ``duration_secs`` on the
        attachment (regular audio uploads have it unset). Defensive: also
        accept ``audio/ogg`` content_type when duration_secs isn't present,
        for older clients.

        A "Forward Message" leaves message.attachments empty and stores the
        payload in message.message_snapshots[0].attachments — same place
        the bridge unpacks below. We check both so forwarded voice notes
        still get transcribed."""
        candidates = list(message.attachments or [])
        snapshots = getattr(message, "message_snapshots", None) or []
        if snapshots:
            candidates += list(getattr(snapshots[0], "attachments", []) or [])
        for att in candidates:
            if getattr(att, "duration_secs", None) is not None:
                return att
            ctype = (getattr(att, "content_type", "") or "").lower()
            if ctype.startswith("audio/ogg") and (att.filename or "").startswith("voice-message"):
                return att
        return None

    async def _maybe_transcribe(self, message: discord.Message) -> str | None:
        """Transcribe a Discord voice message if voice_transcription is on.

        Same shape as the Telegram helper: fires reply_with_transcript
        locally; returns the transcript only when ``bridge_with_transcript``
        is set so the caller can attach it to the bridged copy."""
        att = self._voice_attachment(message)
        if att is None: return None
        vt_cfg = platform_voice_transcription_config(self.platform_config)
        if vt_cfg is None: return None

        dur = getattr(att, "duration_secs", None) or 0
        max_dur = vt_cfg["max_duration_seconds"]
        if max_dur and dur > max_dur:
            logger.info("Voice from %s exceeds %.0fs max — skipping transcription",
                        safe_for_log(getattr(message.author, "display_name", "?")), max_dur)
            return None

        transcriber = get_transcriber(vt_cfg)
        if transcriber is None: return None

        media_dir = platform_media_dir(self.name)
        os.makedirs(media_dir, exist_ok=True)
        tmp_path = get_unique_filepath(media_dir, "voice_t", ".ogg")
        try:
            await att.save(fp=tmp_path)
        except (OSError, discord.HTTPException, discord.NotFound) as exc:
            logger.warning("Failed to download Discord voice for transcription: %s", exc)
            try: os.remove(tmp_path)
            except OSError: pass
            return None

        try:
            transcript = await transcriber.transcribe(tmp_path, language=vt_cfg.get("language"))
        finally:
            try: os.remove(tmp_path)
            except OSError: pass

        if not transcript: return None

        if vt_cfg.get("reply_with_transcript"):
            # Local reply on the same Discord channel. discord.py escapes
            # nothing on send, but message.content is rendered as plain
            # markdown — we leave the user-supplied transcript as-is here
            # (it came from Whisper, not from a third party).
            try:
                await message.reply(f"🎙️ {transcript[:1900]}", mention_author=False)
            except (discord.HTTPException, discord.NotFound) as exc:
                logger.warning("voice transcript local reply failed: %s", exc)

        return transcript if vt_cfg.get("bridge_with_transcript") else None

    async def _handle_incoming_message(
        self,
        message: discord.Message,
        transcript: str | None = None,
    ) -> None:
        bridge_name = self._resolve_bridge_for_message(message)
        if bridge_name is None: return

        sender = self._resolve_sender_name(message)
        logger.info("Message from %s in %s", safe_for_log(sender), bridge_name)
        media_dir = platform_media_dir(self.name)
        # Looked up once and reused below for size limits + filter resolution.
        bridge = self.find_bridge_by_name(bridge_name) or {}

        replied_to_id = None
        if message.reference and isinstance(message.reference.message_id, int):
            replied_to_id = message.reference.message_id

        # Discord's "Forward Message" puts an empty .content on the visible
        # message and the original payload in .message_snapshots. Pull from
        # the snapshot if the top-level fields are empty. Some forward
        # variants place the text in embeds[].description or in stickers /
        # components rather than .content directly, so we also walk those.
        content = message.content
        attachments = list(message.attachments)
        snapshots = getattr(message, "message_snapshots", None) or []
        if snapshots and (not content or not attachments):
            snap = snapshots[0]
            snap_content = getattr(snap, "content", "") or ""
            snap_attachments = list(getattr(snap, "attachments", []) or [])
            snap_embeds = list(getattr(snap, "embeds", []) or [])
            snap_stickers = list(getattr(snap, "sticker_items", []) or [])

            if not content: content = snap_content
            if not attachments: attachments = snap_attachments

            # Fallback 1: text was in an embed (common for "rich" forwards).
            if not content and snap_embeds:
                pieces: list[str] = []
                for e in snap_embeds:
                    for attr in ("title", "description"):
                        v = getattr(e, attr, None)
                        if v: pieces.append(str(v))
                if pieces: content = "\n".join(pieces)

            # Fallback 2: pure-sticker forwards have neither content nor
            # files — at least surface the sticker name so the destination
            # has *something* to read.
            if not content and not attachments and snap_stickers:
                names = [getattr(s, "name", "<sticker>") for s in snap_stickers]
                content = f"[sticker: {', '.join(names)}]"

            logger.info(
                "Discord forward unpacked: content=%r attachments=%d "
                "(snap raw: content=%r attachments=%d embeds=%d stickers=%d)",
                content[:120], len(attachments),
                snap_content[:120], len(snap_attachments),
                len(snap_embeds), len(snap_stickers),
            )

        # Normalize Discord mention markup to readable text. Applies to both
        # the regular and the unwrapped-from-snapshot content paths.
        content = self._humanize_mentions(content)

        # Source-platform timestamp for digest ordering. message.created_at
        # is a datetime; we pass epoch seconds. For forwards, prefer the
        # snapshot's timestamp (the original message's send time) so the
        # digest reflects when the content was actually authored.
        source_ts = None
        if snapshots:
            snap_ts = getattr(snapshots[0], "timestamp", None) or getattr(snapshots[0], "created_at", None)
            if snap_ts is not None: source_ts = snap_ts.timestamp()
        if source_ts is None and message.created_at is not None:
            source_ts = message.created_at.timestamp()

        # Download attachments — refuse anything over the incoming size cap
        # (= effective destination_size_limit × INCOMING_DOWNLOAD_FACTOR)
        # without spending bandwidth or disk on it. The effective limit
        # honors any per-bridge override of destination_size_limit_mb.
        eff_dest_size = effective_destination_size_limit_bytes(bridge, self._destination_size_limit_bytes)
        cap = eff_dest_size * INCOMING_DOWNLOAD_FACTOR
        for attachment in attachments:
            size = getattr(attachment, "size", 0) or 0
            if size > cap:
                logger.warning(
                    "Skipping oversized Discord attachment %s from %s: %.1f MB > %.1f MB cap (no download)",
                    safe_for_log(getattr(attachment, "filename", "<unnamed>")),
                    safe_for_log(sender),
                    size / (1024 * 1024), cap / (1024 * 1024),
                )
                continue
            original_name = attachment.filename
            file_name, file_type = os.path.splitext(original_name)
            if file_type.lower() == ".webp":
                file_type = ".png"
            file_path = get_unique_filepath(media_dir, file_name, file_type)
            # Download can fail for any number of reasons — disk full,
            # transient I/O error, Discord CDN hiccup. Catch and continue
            # so the bot doesn't crash on an event handler exception.
            try:
                await attachment.save(fp=file_path)
            except (OSError, discord.HTTPException, discord.NotFound) as exc:
                logger.error(
                    "Failed to save Discord attachment %s from %s: %s — skipping",
                    safe_for_log(original_name), safe_for_log(sender), exc,
                )
                try: os.remove(file_path)
                except OSError: pass
                continue
            # The pre-download check relied on Discord's reported size. Verify
            # the actual disk size now and discard if a hostile/buggy client
            # understated it to bypass the cap.
            try:
                actual_size = os.path.getsize(file_path)
            except OSError:
                actual_size = 0
            if actual_size > cap:
                logger.warning(
                    "Discord attachment %s claimed %.1f MB but downloaded %.1f MB > cap %.1f MB; discarding",
                    safe_for_log(original_name),
                    size / (1024 * 1024),
                    actual_size / (1024 * 1024),
                    cap / (1024 * 1024),
                )
                try: os.remove(file_path)
                except OSError: pass
                continue
            await self.router.on_file(
                self.name, bridge_name, message.id,
                file_path, file_type, sender,
                replied_to_msg_id=replied_to_id,
                source_ts=source_ts,
            )

        # Text content — skip if it's nothing but a mention tag (use the
        # bridge-effective filter set so per-bridge overrides apply here too)
        eff_filters = effective_mention_filters(bridge, self._mention_filters)
        if content and not is_mention_only(content.lower(), eff_filters):
            await self.router.on_message(
                self.name, bridge_name, message.id,
                content, sender,
                replied_to_msg_id=replied_to_id,
                source_ts=source_ts,
            )
        # Voice transcript companion (sent only when bridge_with_transcript
        # is on and we actually got a transcript). Drops into the digest
        # buffer in digest mode like any other on_message call.
        if transcript:
            await self.router.on_message(
                self.name, bridge_name, message.id,
                f"🎙️ {transcript}", sender,
                replied_to_msg_id=replied_to_id,
                source_ts=source_ts,
            )
        if not content and not attachments and not transcript:
            logger.info("Discord message %s has no content and no attachments — nothing to bridge", message.id)

    async def _handle_reaction(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.member and payload.member.bot: return
        if payload.user_id == self._bot.user.id: return

        channel = self._bot.get_channel(payload.channel_id)
        if channel is None: return

        bridge_name = self.bridge_name_for_source_chat(channel.id)
        if bridge_name is None and isinstance(channel, discord.DMChannel):
            bridge_name = self.bridge_name_for_source_chat(payload.user_id)
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

        actual_path = file_path
        transcoded_path: str | None = None
        original_size = os.path.getsize(file_path)
        # Resolve bridge-effective destination size limit so per-bridge overrides
        # of destination_size_limit_mb apply (e.g. boosted server vs. unboosted).
        bridge_dict = self.find_bridge_by_name(bridge) or {} if bridge else {}
        limit = effective_destination_size_limit_bytes(bridge_dict, self._destination_size_limit_bytes)

        # If over the upload limit, try to transcode media down to fit.
        if original_size > limit:
            transcoded_path = await compress_to_fit(file_path, target_bytes=limit)
            if transcoded_path:
                new_size = os.path.getsize(transcoded_path)
                logger.info(
                    "Transcoded %s (%.2f MB → %.2f MB) to fit Discord destination size limit",
                    file_name, original_size / (1024 * 1024), new_size / (1024 * 1024),
                )
                actual_path = transcoded_path
            else:
                limit_mb = limit // (1024 * 1024)
                msg = f"File is {original_size / (1024 * 1024):.1f} MB, over the {limit_mb} MB limit, and couldn't be transcoded. Sorry :("
                text = msg if sender == bridge else f"*{sender}:* {msg}"
                await _send_with_reply(content=text)
                return None

        sent_msg = None
        try:
            if sender == bridge: sent_msg = await _send_with_reply(file=discord.File(actual_path))
            else: sent_msg = await _send_with_reply(file=discord.File(actual_path), content=f"*{sender}:* [{file_name}]")
        finally:
            if transcoded_path:
                try: os.remove(transcoded_path)
                except OSError: pass

        return sent_msg.id if sent_msg else None

    # Lifecycle — backoff is linear (additive), the gentlest pattern that
    # still progresses. Each failed attempt extends the wait by INCREMENT,
    # capped at MAX. The Retry-After header from Discord is always honored
    # if it asks for longer than our schedule. Jitter prevents synchronized
    # retries.
    INITIAL_WAIT = 120        # seconds — first wait after a failure
    WAIT_INCREMENT = 60       # seconds added per subsequent failure
    MAX_WAIT = 300            # 5 minutes — plateau ceiling
    JITTER_RATIO = 0.2        # ±20% randomization

    async def start(self) -> None:
        logger.info("Discord platform starting…")
        attempt = 0
        while True:
            attempt += 1
            try:
                await self._bot.start(self._token)
                return  # bot.start runs the event loop until close
            except discord.LoginFailure as e:
                logger.error("Discord login failed (token invalid?): %s — giving up", e)
                raise
            except discord.HTTPException as e:
                base = self._linear_wait(attempt)
                retry_after = self._extract_retry_after(e)
                wait = max(base, retry_after or 0)
                wait = self._with_jitter(wait)
                logger.warning(
                    "Discord HTTP error on start (attempt %d, status=%s, code=%s, retry-after=%s) — sleeping %ds: %s",
                    attempt, getattr(e, "status", "?"), getattr(e, "code", "?"),
                    retry_after if retry_after is not None else "n/a",
                    wait, e,
                )
            except Exception as e:
                wait = self._with_jitter(self._linear_wait(attempt))
                logger.error(
                    "Discord unexpected error on start (attempt %d) — sleeping %ds: %s",
                    attempt, wait, e, exc_info=True,
                )
            await self._safe_close_bot()
            await asyncio.sleep(wait)
            self._recreate_bot()

    @classmethod
    def _linear_wait(cls, attempt: int) -> int:
        """Linear (additive) backoff schedule, capped at MAX_WAIT."""
        seconds = cls.INITIAL_WAIT + max(0, attempt - 1) * cls.WAIT_INCREMENT
        return min(seconds, cls.MAX_WAIT)

    @classmethod
    def _with_jitter(cls, seconds: int) -> int:
        """Apply ±JITTER_RATIO randomization so retries don't synchronize."""
        factor = 1.0 + random.uniform(-cls.JITTER_RATIO, cls.JITTER_RATIO)
        return max(1, int(seconds * factor))

    @staticmethod
    def _extract_retry_after(exc: discord.HTTPException) -> int | None:
        """Pull a Retry-After hint from the response, in seconds."""
        response = getattr(exc, "response", None)
        if response is None: return None
        headers = getattr(response, "headers", None) or {}
        ra = headers.get("Retry-After") or headers.get("X-RateLimit-Reset-After")
        if ra:
            try: return int(float(ra))
            except (TypeError, ValueError): return None
        return None

    async def _safe_close_bot(self) -> None:
        try: await self._bot.close()
        except Exception: pass

    def _recreate_bot(self) -> None:
        """Rebuild the client — the previous session is unusable after a
        failed login — and re-attach handlers."""
        self._bot = commands.Bot(
            command_prefix=".",
            intents=discord.Intents.all(),
            application_id=self.platform_config["app_id"],
        )
        self._ready_event = asyncio.Event()
        self._register_handlers()

    async def stop(self) -> None:
        await self._bot.close()
