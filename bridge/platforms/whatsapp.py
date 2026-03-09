"""
WhatsApp platform — skeleton implementation.
"""

import asyncio
from bridge.platforms.base import BasePlatform
from bridge.utils.logger import get_logger

logger = get_logger("whatsapp")

class WhatsAppPlatform(BasePlatform):
    """Stub WhatsApp bridge platform.

    To complete this integration:

    1. Choose a WhatsApp library / API and add it to ``requirements.txt``.
    2. Implement :meth:`start` — connect, authenticate, register message
       handlers that call ``self.router.on_message`` / ``on_file`` / ``on_reaction``.
    3. Implement :meth:`send_text` and :meth:`send_file` — deliver outbound
       messages to the WhatsApp chat and return the native message ID.
    4. Implement :meth:`stop` — gracefully disconnect.
    """

    def __init__(self, platform_config: dict, bridges: list[dict], router=None):
        super().__init__("whatsapp", platform_config, bridges, router)
        self._phone = platform_config.get("phone")
        logger.info("WhatsApp platform configured for %s", self._phone)

    async def start(self) -> None:
        raise NotImplementedError(
            "WhatsApp platform is a stub. Implement start() with your "
            "chosen WhatsApp library to connect and receive messages."
        )

    async def stop(self) -> None:
        raise NotImplementedError("WhatsApp platform is a stub. Implement stop().")

    async def send_text(self, chat_id, content: str, sender: str, reply_to_native_id=None) -> int | None:
        raise NotImplementedError("WhatsApp platform is a stub. Implement send_text().")

    async def send_file(self, chat_id, file_path: str, file_ext: str, sender: str, reply_to_native_id=None) -> int | None:
        raise NotImplementedError("WhatsApp platform is a stub. Implement send_file().")
