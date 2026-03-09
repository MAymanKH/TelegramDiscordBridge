"""
Central message router — dispatches incoming events to all other platforms
in the same bridge.
"""

import os
from bridge import database
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

    def __init__(self, platforms: dict[str, BasePlatform], bridges: list[dict], db_path: str):
        self.platforms = platforms
        self.bridges = bridges
        self.db_path = db_path

    # Helpers
    def _other_platforms_in_bridge(self, bridge: dict, source_name: str):
        """Yield ``(platform_key, chat_id, platform_instance)`` for every
        platform in *bridge* that is not *source_name*."""
        for pname, chat_id in bridge.get("platforms", {}).items():
            if pname == source_name: continue
            instance = self.platforms.get(pname)
            if instance is None:
                logger.warning("Platform '%s' referenced in bridge '%s' but not registered", pname, bridge["name"])
                continue
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
    ) -> None:
        """A text message arrived on *source_platform*.  Forward it to
        every other platform in the bridge."""
        bridge = self._find_bridge(bridge_name)
        if bridge is None:
            logger.warning("on_message: unknown bridge '%s'", bridge_name)
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

    async def on_file(
        self,
        source_platform: str,
        bridge_name: str,
        source_msg_id: int,
        file_path: str,
        file_ext: str,
        sender: str,
        replied_to_msg_id: int | None = None,
    ) -> None:
        """An attachment arrived on *source_platform*.  Forward it to
        every other platform in the bridge, then clean up the temp file."""
        bridge = self._find_bridge(bridge_name)
        if bridge is None:
            logger.warning("on_file: unknown bridge '%s'", bridge_name)
            return

        for tgt_name, chat_id, tgt_platform in self._other_platforms_in_bridge(bridge, source_platform):
            reply_native_id = None
            if replied_to_msg_id is not None:
                reply_native_id = await database.resolve_native_id(
                    self.db_path, bridge_name, source_platform, replied_to_msg_id, tgt_name,
                )

            sent_id = await tgt_platform.send_file(chat_id, file_path, file_ext, sender, reply_to_native_id=reply_native_id)

            if sent_id is not None:
                await database.save_message_mapping(
                    self.db_path, bridge_name,
                    source_platform, source_msg_id,
                    tgt_name, sent_id,
                    sender=sender,
                )

        # Clean up the temporary file after all platforms have received it
        if os.path.isfile(file_path): os.remove(file_path)

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
        bridge = self._find_bridge(bridge_name)
        if bridge is None: return

        content = f"> {sender} reacted with {emoji}"

        for tgt_name, chat_id, tgt_platform in self._other_platforms_in_bridge(bridge, source_platform):
            reply_native_id = await database.resolve_native_id(
                self.db_path, bridge_name, source_platform, source_msg_id, tgt_name,
            )
            await tgt_platform.send_text(chat_id, content, bridge_name, reply_to_native_id=reply_native_id)
