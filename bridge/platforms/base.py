"""
Abstract base class that every platform must implement.
"""

from abc import ABC, abstractmethod

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

    # Helpers
    def bridge_name_for_chat(self, chat_id) -> str | None:
        """Return the bridge name that includes *chat_id* for this platform."""
        for b in self.bridges:
            platforms = b.get("platforms", {})
            if self.name in platforms and platforms[self.name] == chat_id: return b["name"]
        return None

    def chat_id_for_bridge(self, bridge_name: str):
        """Return this platform's chat/channel ID for a given bridge, or ``None``."""
        for b in self.bridges:
            if b["name"] == bridge_name: return b.get("platforms", {}).get(self.name)
        return None

    def my_chat_ids(self) -> list:
        """Return all chat IDs this platform should listen to."""
        ids = []
        for b in self.bridges:
            cid = b.get("platforms", {}).get(self.name)
            if cid is not None: ids.append(cid)
        return ids
