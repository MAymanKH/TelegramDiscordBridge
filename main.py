import asyncio
from bridge.utils.config import load_settings, ensure_directories, get_bridges, get_enabled_platforms, get_platform_config, BRIDGE_DB
from bridge.database import init_db
from bridge.router import Router
from bridge.utils.logger import setup_logging, get_logger
from bridge.platforms.telegram import TelegramPlatform
from bridge.platforms.discord import DiscordPlatform
from bridge.platforms.whatsapp import WhatsAppPlatform

logger = get_logger("main")

PLATFORM_REGISTRY: dict[str, type] = {
    "telegram": TelegramPlatform,
    "discord": DiscordPlatform,
    "whatsapp": WhatsAppPlatform,
}

async def main() -> None:
    settings = load_settings()
    ensure_directories(settings)

    # Initialize the unified database
    await init_db(BRIDGE_DB)
    bridges = get_bridges(settings)
    enabled = get_enabled_platforms(settings)

    platforms: dict[str, object] = {}
    for name in enabled:
        cls = PLATFORM_REGISTRY.get(name)
        if cls is None:
            logger.warning("No implementation found for platform '%s' — skipping", name)
            continue
        pcfg = get_platform_config(settings, name)
        platforms[name] = cls(platform_config=pcfg, bridges=bridges)
        logger.info("Registered platform: %s", name)

    if not platforms:
        logger.error("No platforms enabled — nothing to do.")
        return

    # Create the router and wire it into each platform
    router = Router(platforms, bridges, db_path=BRIDGE_DB)
    for p in platforms.values():
        p.router = router

    logger.info(
        "Starting %d platform(s): %s",
        len(platforms), ", ".join(platforms.keys()),
    )
    await asyncio.gather(*(p.start() for p in platforms.values()))


if __name__ == "__main__":
    setup_logging()
    asyncio.run(main())
