"""
Abstract base class that every platform must implement.
"""

from abc import ABC, abstractmethod
from bridge.utils.config import bridge_sources, bridge_targets, bridge_all_platforms

class BasePlatform(ABC):
    """Interface contract for a bridge platform."""

    name: str
    def __init__(self, name: str, platform_config: dict, bridges: list[dict], router=None):
        """
        Parameters
        ----------
        name : str
            Platform key as it appears in ``settings.yaml`` (e.g. ``"telegram"``).
        platform_config : dict
            The platform-specific credentials / options from ``settings["platforms"][name]``.
        bridges : list[dict]
            The full list of bridge definitions. Each platform should filter
            for bridges that include itself.
        router : Router | None
            The central message router.  Set after construction via
            :pyattr:`router` so that the router can be created after all
            platforms are instantiated.
        """
        self.name = name
        self.platform_config = platform_config
        self.bridges = bridges
        self.router = router

    # Lifecycle
    @abstractmethod
    async def start(self) -> None:
        """Connect / authenticate and begin receiving events.

        This coroutine should run **indefinitely** (e.g. keep an event-loop
        alive) so that it can be used with ``asyncio.gather``.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully disconnect."""

    # Outbound messaging
    @abstractmethod
    async def send_text(
        self,
        chat_id,
        content: str,
        sender: str,
        reply_to_native_id=None,
    ) -> int | None:
        """Send a text message to *chat_id*.
        Returns the **native message ID** of the sent message, or ``None``
        if sending failed.
        """

    @abstractmethod
    async def send_file(
        self,
        chat_id,
        file_path: str,
        file_ext: str,
        sender: str,
        reply_to_native_id=None,
    ) -> int | None:
        """Send a file/attachment to *chat_id*.
        Returns the **native message ID** of the sent message, or ``None``
        if sending failed.
        """

    # Outbound text safety
    def escape_user_text(self, s: str) -> str:
        """Escape *s* for safe interpolation into this platform's outbound
        message format.

        Default: identity (the platform's send_text doesn't render markup,
        or its rendering is benign). Telegram overrides this with HTML
        escaping because its outbound parse mode is HTML and untrusted
        text could otherwise inject tags or markdown link syntax.
        """
        return s

    def format_digest_body(self, entries: list[tuple[float, str, str]]) -> str:
        """Render digest entries as a body string in this platform's native
        markup. Default is platform-agnostic plain text. Subclasses override
        to produce richer formatting safely (with user content escaped).

        Consecutive messages from the same sender are grouped under a
        single ``Name · HH:MM`` header — keeps the digest readable when
        someone sends a burst of messages at once."""
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
        out: list[str] = []
        for sender, ts, lines in groups:
            ts_str = time.strftime("%H:%M", time.localtime(ts))
            out.append(f"{sender} · {ts_str}")
            for line in lines:
                out.append(f"  {line}")
            out.append("")
        return "\n".join(out).rstrip()

    # Hot-reload hook
    def apply_config(self, platform_config: dict) -> None:
        """Refresh mutable platform settings from a re-read YAML file.

        Default implementation only updates the stored ``platform_config``.
        Subclasses should override to reapply settings such as filters.
        Credentials cannot be reloaded — those are baked into the client.
        """
        self.platform_config = platform_config

    # Helpers
    def bridge_name_for_chat(self, chat_id) -> str | None:
        """Return the bridge name that involves *chat_id* on this platform —
        regardless of direction. Used by outbound formatting paths that just
        need to know which bridge a chat belongs to."""
        for b in self.bridges:
            if chat_id in bridge_all_platforms(b).get(self.name, []): return b["name"]
        return None

    def bridge_name_for_source_chat(self, chat_id) -> str | None:
        """Return the bridge name where *chat_id* is a **source** for this
        platform. Used by inbound dispatch — messages on a target-only chat
        have no bridge to forward into."""
        for b in self.bridges:
            if chat_id in bridge_sources(b).get(self.name, []): return b["name"]
        return None

    def chat_ids_for_bridge(self, bridge_name: str) -> list:
        """Return this platform's *target* chat IDs for a given bridge."""
        for b in self.bridges:
            if b["name"] == bridge_name: return list(bridge_targets(b).get(self.name, []))
        return []

    def my_chat_ids(self) -> list:
        """Return all chat IDs this platform should listen to (its source chats)."""
        ids: list = []
        for b in self.bridges:
            ids.extend(bridge_sources(b).get(self.name, []))
        return ids

    def find_bridge_by_name(self, name: str) -> dict | None:
        """Return the bridge dict by name, or ``None`` if not found."""
        for b in self.bridges:
            if b["name"] == name: return b
        return None
