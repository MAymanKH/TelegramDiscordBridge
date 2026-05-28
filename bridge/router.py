"""
Central message router — dispatches incoming events to all other platforms
in the same bridge.
"""

import asyncio
import os
import time
from bridge import database
from bridge.utils.config import (
    bridge_targets, is_directional_bridge, bridge_digest_config,
    bridge_enrich_config, enrich_media_dir,
)
from bridge.utils.logger import get_logger
from bridge.platforms.base import BasePlatform

logger = get_logger("router")

class Router:
    """Fan-out dispatcher for bridge messages.

    The router holds references to every active :class:`BasePlatform`
    instance and the list of bridge definitions.  When a platform calls
    one of the ``on_*`` methods the router forwards the event to every
    *other* platform that participates in the same bridge.
    """

    def __init__(self, platforms: dict[str, BasePlatform], bridges: list[dict], db_path: str, forward_reactions: bool = False):
        self.platforms = platforms
        self.bridges = bridges
        self.db_path = db_path
        self.forward_reactions = forward_reactions
        # Per-(bridge_name, source_platform) digest buffers — populated
        # lazily when a bridge has digest mode enabled and a message arrives.
        self._digest_buffers: dict[tuple[str, str], _DigestBuffer] = {}

    # Helpers
    def _other_platforms_in_bridge(self, bridge: dict, source_name: str):
        """Yield ``(platform_key, chat_id, platform_instance)`` for every
        target chat in *bridge*.

        Each platform's target value may be a single chat ID or a list, so
        a single bridge can fan out to multiple chats on the same platform.
        For bidirectional bridges, the source platform is excluded to avoid
        echoing back. For directional bridges, the user has explicitly listed
        targets in ``to:`` — same-platform fan-out is allowed.
        """
        directional = is_directional_bridge(bridge)
        for pname, chat_ids in bridge_targets(bridge).items():
            if not directional and pname == source_name: continue
            instance = self.platforms.get(pname)
            if instance is None:
                logger.warning("Platform '%s' referenced in bridge '%s' but not registered", pname, bridge["name"])
                continue
            for chat_id in chat_ids:
                yield pname, chat_id, instance

    def _find_bridge(self, bridge_name: str) -> dict | None:
        for b in self.bridges:
            if b["name"] == bridge_name: return b
        return None

    # Public dispatch methods
    async def on_message(
        self,
        source_platform: str,
        bridge_name: str,
        source_msg_id: int,
        content: str,
        sender: str,
        replied_to_msg_id: int | None = None,
        source_ts: float | None = None,
    ) -> None:
        """A text message arrived on *source_platform*.  Forward it to
        every other platform in the bridge."""
        bridge = self._find_bridge(bridge_name)
        if bridge is None:
            logger.warning("on_message: unknown bridge '%s'", bridge_name)
            return

        # Digest mode: buffer text messages, flush as a formatted thread.
        # Media (on_file) and reactions are NOT buffered — only on_message.
        digest_cfg = bridge_digest_config(bridge)
        if digest_cfg:
            await self._enqueue_digest(bridge, source_platform, sender, content, digest_cfg, source_ts=source_ts)
            return

        for tgt_name, chat_id, tgt_platform in self._other_platforms_in_bridge(bridge, source_platform):
            reply_native_id = None
            if replied_to_msg_id is not None:
                reply_native_id = await database.resolve_native_id(
                    self.db_path, bridge_name, source_platform, replied_to_msg_id, tgt_name,
                )

            sent_id = await tgt_platform.send_text(chat_id, content, sender, reply_to_native_id=reply_native_id)

            if sent_id is not None:
                await database.save_message_mapping(
                    self.db_path, bridge_name,
                    source_platform, source_msg_id,
                    tgt_name, sent_id,
                    sender=sender,
                )

        # Link enrichment runs AFTER the original is delivered ("keep link +
        # append enrichment"). Non-digest bridges only — digest bridges skip
        # enrichment for now (the digest already buffers everything).
        enrich_cfg = bridge_enrich_config(bridge)
        if enrich_cfg and content:
            await self._enrich_and_dispatch(bridge, source_platform, source_msg_id, content, enrich_cfg)

    async def on_file(
        self,
        source_platform: str,
        bridge_name: str,
        source_msg_id: int,
        file_path: str,
        file_ext: str,
        sender: str,
        replied_to_msg_id: int | None = None,
        source_ts: float | None = None,
        save_mapping: bool = True,
    ) -> None:
        """An attachment arrived on *source_platform*.

        Three modes, depending on the bridge config:
          1. No digest        → fan out immediately, then delete the file.
          2. Digest, no buffer_media → fan out immediately, delete the file,
             AND drop a placeholder line into the digest body.
          3. Digest + buffer_media → DO NOT fan out, DO NOT delete; defer
             both file and a placeholder line to the digest flush.
        """
        bridge = self._find_bridge(bridge_name)
        if bridge is None:
            logger.warning("on_file: unknown bridge '%s'", bridge_name)
            return

        digest_cfg = bridge_digest_config(bridge)
        media_label = _media_label(file_path, file_ext)

        # Mode 3: defer the file alongside the digest text.
        if digest_cfg and digest_cfg.get("buffer_media"):
            await self._enqueue_digest(bridge, source_platform, sender, media_label, digest_cfg, source_ts=source_ts)
            await self._enqueue_digest_file(
                bridge, source_platform, sender, file_path, file_ext,
                source_msg_id, replied_to_msg_id, digest_cfg, source_ts=source_ts,
            )
            return

        # Modes 1 & 2: immediate fan-out
        for tgt_name, chat_id, tgt_platform in self._other_platforms_in_bridge(bridge, source_platform):
            reply_native_id = None
            if replied_to_msg_id is not None:
                reply_native_id = await database.resolve_native_id(
                    self.db_path, bridge_name, source_platform, replied_to_msg_id, tgt_name,
                )

            sent_id = await tgt_platform.send_file(chat_id, file_path, file_ext, sender, reply_to_native_id=reply_native_id)

            if sent_id is not None and save_mapping:
                await database.save_message_mapping(
                    self.db_path, bridge_name,
                    source_platform, source_msg_id,
                    tgt_name, sent_id,
                    sender=sender,
                )

        if os.path.isfile(file_path): os.remove(file_path)

        # Mode 2: also note the upload in the digest body.
        if digest_cfg:
            await self._enqueue_digest(bridge, source_platform, sender, media_label, digest_cfg, source_ts=source_ts)

    # Link enrichment ----------------------------------------------------

    async def _enrich_and_dispatch(self, bridge: dict, source_platform: str,
                                   source_msg_id, content: str, cfg: dict) -> None:
        """Scan *content* for supported links, enrich each, and append the
        result (text + media) to the other platforms in the bridge.

        When ``quote_original`` is set, the enriched follow-up is sent as a
        reply to the bridged copy of the original message on each
        destination — so the enrichment is visually attached to the link
        that triggered it."""
        from bridge.enrichers import extract_urls, get_enricher
        urls = extract_urls(content)
        if not urls: return
        dest_dir = enrich_media_dir()
        bridge_name = bridge["name"]
        quote = cfg.get("quote_original", True)
        enriched = 0
        for url in urls:
            if enriched >= cfg["max_links"]: break
            enricher = get_enricher(url, cfg["providers"])
            if enricher is None: continue
            enriched += 1
            try:
                result = await enricher.enrich(url, cfg, dest_dir)
            except Exception as exc:
                logger.warning("Enrichment failed for %s: %s", url, exc, exc_info=True)
                continue
            if result is None: continue

            # Text follow-up — per-target escaped (raw site content is
            # untrusted) and optionally a reply to the bridged original.
            if result.text or result.author:
                for tgt_name, chat_id, tgt_platform in self._other_platforms_in_bridge(bridge, source_platform):
                    body = _format_enrichment(result, tgt_platform.escape_user_text)
                    if not body: continue
                    reply_id = None
                    if quote:
                        reply_id = await database.resolve_native_id(
                            self.db_path, bridge_name, source_platform, source_msg_id, tgt_name,
                        )
                    await tgt_platform.send_text(chat_id, body, bridge_name, reply_to_native_id=reply_id)

            # Media follow-up — route through on_file to reuse the size caps,
            # transcoding, and cleanup. `save_mapping=False` keeps enriched
            # media out of the reply-resolution table (it isn't itself a
            # reply target). `replied_to_msg_id=source_msg_id` makes it quote
            # the bridged original.
            for media_path, media_ext in result.media:
                await self.on_file(
                    source_platform, bridge_name, source_msg_id,
                    media_path, media_ext, bridge_name,
                    replied_to_msg_id=(source_msg_id if quote else None),
                    save_mapping=False,
                )

    async def on_reaction(
        self,
        source_platform: str,
        bridge_name: str,
        source_msg_id: int,
        emoji: str,
        sender: str,
    ) -> None:
        """A reaction was added on *source_platform*.  Forward it as a
        text notification to every other platform in the bridge."""
        if not self.forward_reactions: return
        bridge = self._find_bridge(bridge_name)
        if bridge is None: return

        for tgt_name, chat_id, tgt_platform in self._other_platforms_in_bridge(bridge, source_platform):
            # Per-target escape so user-supplied display names can't inject
            # markup or link syntax into the destination platform.
            content = f"> {tgt_platform.escape_user_text(sender)} reacted with {tgt_platform.escape_user_text(emoji)}"
            reply_native_id = await database.resolve_native_id(
                self.db_path, bridge_name, source_platform, source_msg_id, tgt_name,
            )
            await tgt_platform.send_text(chat_id, content, bridge_name, reply_to_native_id=reply_native_id)

    # Digest buffering ---------------------------------------------------

    def _digest_buffer_for(self, bridge: dict, source_platform: str, cfg: dict) -> "_DigestBuffer":
        """Lazily create / fetch the digest buffer for a ``(bridge, source)`` pair."""
        bridge_name = bridge["name"]
        key = (bridge_name, source_platform)
        buf = self._digest_buffers.get(key)
        if buf is None:
            async def _on_flush(entries, pending_files) -> None:
                await self._flush_digest(bridge_name, source_platform, entries, pending_files)
            buf = _DigestBuffer(
                _on_flush,
                cfg["wait_seconds"],
                max_wait_seconds=cfg.get("max_wait_seconds", 0.0),
            )
            self._digest_buffers[key] = buf
        return buf

    async def _enqueue_digest(self, bridge: dict, source_platform: str, sender: str, content: str, cfg: dict,
                              source_ts: float | None = None) -> None:
        """Append a text entry to the digest buffer for ``(bridge, source)``."""
        self._digest_buffer_for(bridge, source_platform, cfg).add(sender, content, ts=source_ts)

    async def _enqueue_digest_file(self, bridge: dict, source_platform: str, sender: str,
                                   file_path: str, file_ext: str, source_msg_id,
                                   replied_to_msg_id, cfg: dict,
                                   source_ts: float | None = None) -> None:
        """Defer a media file until the digest flush. Caller must NOT delete
        *file_path* — the buffer takes ownership and removes it after sending."""
        self._digest_buffer_for(bridge, source_platform, cfg).add_file(
            sender, file_path, file_ext, source_msg_id, replied_to_msg_id, ts=source_ts,
        )

    async def _flush_digest(self, bridge_name: str, source_platform: str,
                            entries: list[tuple[float, str, str]],
                            pending_files: list[dict]) -> None:
        """Send all pending files first, then the aggregated text thread."""
        if not entries and not pending_files: return
        bridge = self._find_bridge(bridge_name)
        if bridge is None:
            logger.warning("flush_digest: bridge '%s' no longer exists", bridge_name)
            for pf in pending_files:
                try: os.remove(pf["file_path"])
                except OSError: pass
            return

        logger.info(
            "Digest flush: %d msg(s) + %d file(s) from %s on bridge '%s'",
            len(entries), len(pending_files), source_platform, bridge_name,
        )

        # 1. Pending files — fan out and clean up
        for pf in pending_files:
            for tgt_name, chat_id, tgt_platform in self._other_platforms_in_bridge(bridge, source_platform):
                try:
                    sent_id = await tgt_platform.send_file(
                        chat_id, pf["file_path"], pf["file_ext"], pf["sender"],
                        reply_to_native_id=None,
                    )
                    if sent_id is not None:
                        await database.save_message_mapping(
                            self.db_path, bridge_name,
                            source_platform, pf["source_msg_id"],
                            tgt_name, sent_id, sender=pf["sender"],
                        )
                except Exception as exc:
                    logger.error("Digest flush — file %s failed: %s", pf["file_path"], exc, exc_info=True)
            try: os.remove(pf["file_path"])
            except OSError: pass

        # 2. Aggregated text — formatted PER-TARGET via the platform's own
        # ``format_digest_body`` so each side renders in its native markup
        # with user content escaped. Without per-target escaping, a hostile
        # display name like "[Support](https://phish)" could inject
        # phishing links into the digest body on the destination side.
        if entries:
            for tgt_name, chat_id, tgt_platform in self._other_platforms_in_bridge(bridge, source_platform):
                body = tgt_platform.format_digest_body(entries)
                if body:
                    await tgt_platform.send_text(chat_id, body, bridge_name)


class _DigestBuffer:
    """Aggregate messages (and optionally pending files) for a single
    ``(bridge, source)`` pair.

    Scheduling is **debounce**: each new message resets the wait timer.
    The digest only flushes after ``wait_seconds`` of *silence* — so a
    burst of replies that arrive close together get bundled into the same
    digest. If ``max_wait_seconds > 0`` it acts as a hard safety cap from
    the time of the first message in the current cycle: a continuously
    active channel still gets a digest at most once every
    ``max_wait_seconds``."""

    def __init__(self, on_flush, wait_seconds: float, max_wait_seconds: float = 0.0):
        self._on_flush = on_flush
        self._wait_seconds = float(wait_seconds)
        self._max_wait_seconds = float(max_wait_seconds)
        self._entries: list[tuple[float, str, str]] = []  # (ts, sender, content)
        self._pending_files: list[dict] = []
        self._task: asyncio.Task | None = None
        # Wall-clock start of the current cycle. Set on the first add of
        # a cycle, cleared on flush. Used to enforce max_wait_seconds.
        self._first_entry_ts: float | None = None

    def add(self, sender: str, content: str, ts: float | None = None) -> None:
        # *ts* is the source-platform message timestamp when available;
        # falling back to wall-clock dispatch time. Stored for ordering at
        # flush time so async handler concurrency can't reorder messages
        # relative to their actual send order.
        if ts is None: ts = time.time()
        self._entries.append((ts, sender, content))
        self._reschedule()

    def add_file(self, sender: str, file_path: str, file_ext: str,
                 source_msg_id, replied_to_msg_id, ts: float | None = None) -> None:
        if ts is None: ts = time.time()
        self._pending_files.append({
            "ts": ts,
            "sender": sender,
            "file_path": file_path,
            "file_ext": file_ext,
            "source_msg_id": source_msg_id,
            "replied_to_msg_id": replied_to_msg_id,
        })
        self._reschedule()

    def _reschedule(self) -> None:
        """Cancel any pending flush and re-arm the timer.

        Computes the next-flush delay as ``min(wait_seconds, remaining
        max budget)`` so the debounce timer is automatically clamped if
        the channel has been chatting non-stop past the max-wait cap."""
        now = time.time()
        if self._first_entry_ts is None:
            self._first_entry_ts = now
        wait = self._wait_seconds
        if self._max_wait_seconds > 0:
            elapsed = now - self._first_entry_ts
            wait = min(wait, max(0.0, self._max_wait_seconds - elapsed))
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._wait_then_flush(wait))

    async def _wait_then_flush(self, wait: float) -> None:
        try:
            if wait > 0:
                await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return
        # Past the sleep — drain the buffer locally and clear, then
        # dispatch. Any further add() calls hit a clean state and start
        # a fresh cycle.
        entries = self._entries
        files = self._pending_files
        self._entries = []
        self._pending_files = []
        self._first_entry_ts = None
        self._task = None
        if not (entries or files): return
        # Sort by source-message timestamp so the digest reads in true
        # chronological order even if async handlers added items out of
        # order (e.g. a slow media download finishing after a later text).
        entries.sort(key=lambda e: e[0])
        files.sort(key=lambda f: f["ts"])
        try:
            await self._on_flush(entries, files)
        except Exception as exc:
            logger.error("Digest flush failed: %s", exc, exc_info=True)


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
_AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".oga", ".wav", ".opus", ".aac", ".flac"}
_DOC_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt"}


def _format_enrichment(result, escape) -> str:
    """Render an EnrichResult as a destination-safe follow-up message.

    *escape* is the target platform's escape function so untrusted site
    content (author + text) can't inject markup or links. Header line is
    ``<icon> <author>:`` when there's an author; otherwise the icon
    prefixes the text directly."""
    icon = getattr(result, "icon", "") or "🔗"
    author = escape(result.author) if result.author else ""
    text = escape(result.text) if result.text else ""
    if author:
        return f"{icon} {author}:\n{text}" if text else f"{icon} {author}"
    if text:
        return f"{icon} {text}"
    return ""


def _media_label(file_path: str, file_ext: str) -> str:
    """One-line placeholder for a media attachment in a digest thread.

    Picks an emoji based on extension and includes the original basename
    so the reader can correlate with the actual file uploaded above."""
    name = os.path.basename(file_path) if file_path else "attachment"
    ext = (file_ext or os.path.splitext(name)[1] or "").lower()
    if ext in _IMAGE_EXTS: emoji = "🖼"
    elif ext in _VIDEO_EXTS: emoji = "🎬"
    elif ext in _AUDIO_EXTS: emoji = "🎵"
    elif ext in _DOC_EXTS: emoji = "📄"
    else: emoji = "📎"
    return f"[{emoji} {name}]"


# Note: digest body formatting moved to per-platform `format_digest_body`
# methods (in :mod:`bridge.platforms.base` + overrides) so each destination
# can apply its own markup rules with proper user-content escaping. See the
# class-level docstrings on TelegramPlatform.format_digest_body etc.
