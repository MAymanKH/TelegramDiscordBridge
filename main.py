"""
TelegramDiscordBridge — entry point.

Sets up logging, ensures required directories exist, and starts both the
Telegram and Discord bots **in-process** (no subprocess spawning).
"""

import asyncio

from bridge.config import ensure_directories
from bridge.logger import setup_logging, get_logger

logger = get_logger("main")


async def main() -> None:
    from bridge import telegram_bot, discord_bot

    logger.info("Starting Telegram and Discord bots…")
    await asyncio.gather(
        telegram_bot.run(),
        discord_bot.run(),
    )


if __name__ == "__main__":
    setup_logging()
    ensure_directories()
    asyncio.run(main())
