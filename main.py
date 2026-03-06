import asyncio
from bridge.config import ensure_directories
from bridge.logger import setup_logging, get_logger
from bridge import telegram_bot, discord_bot

logger = get_logger("main")

async def main() -> None:
    logger.info("Starting Telegram and Discord bots…")
    await asyncio.gather(
        telegram_bot.run(),
        discord_bot.run(),
    )

if __name__ == "__main__":
    setup_logging()
    ensure_directories()
    asyncio.get_event_loop().run_until_complete(main())
