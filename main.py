import asyncio
import os
import sys
import time
import yaml
from bridge import __version__
from bridge.utils.config import load_settings, ensure_directories, get_bridges, get_enabled_platforms, get_platform_config, BRIDGE_DB
from bridge.database import init_db
from bridge.router import Router
from bridge.utils.logger import setup_logging, get_logger
from bridge.utils.watch import SettingsWatcher
from bridge.utils.janitor import MediaJanitor
from bridge.platforms.telegram import TelegramPlatform
from bridge.platforms.discord import DiscordPlatform
from bridge.platforms.whatsapp import WhatsAppPlatform

# Where to read the YAML config from. Defaults to ``settings.yaml`` in
# CWD (works for local dev with `python main.py`). Override via env when
# you'd rather mount a config directory than a single file — e.g. the
# bundled docker-compose.yml sets this to ``/app/dis_to_tg/settings.yaml``
# so it can mount ``./dis_to_tg/`` and bind multiple per-deploy artifacts
# next to settings.yaml.
SETTINGS_PATH = os.environ.get("BRIDGER_SETTINGS_PATH") or "settings.yaml"

def _version_string() -> str:
    """Compose ``v<semver>`` plus optional ``+<sha>`` if a build supplied
    ``BRIDGER_GIT_SHA`` (set via Docker --build-arg or env)."""
    sha = os.environ.get("BRIDGER_GIT_SHA", "").strip()
    return f"v{__version__}" + (f"+{sha[:8]}" if sha else "")

logger = get_logger("main")

PLATFORM_REGISTRY: dict[str, type] = {
    "telegram": TelegramPlatform,
    "discord": DiscordPlatform,
    "whatsapp": WhatsAppPlatform,
}

def _apply_timezone(settings: dict) -> None:
    """Apply ``settings["timezone"]`` (e.g. ``"Europe/Berlin"``) so digest
    timestamps and other ``time.localtime``-based output reflect the user's
    chosen zone.

    No-op if the key is unset or empty (system default applies — UTC inside
    a stock Docker container). Safe to call multiple times — used both at
    startup and on settings reload.
    """
    tz = (settings.get("timezone") or "").strip()
    if not tz: return
    os.environ["TZ"] = tz
    # time.tzset is POSIX-only. The container is Linux, so this works in
    # production; on Windows dev machines we silently skip.
    if hasattr(time, "tzset"):
        time.tzset()
    logger.info("Timezone set to %s (sample now: %s)", tz, time.strftime("%Y-%m-%d %H:%M:%S %Z"))


def _warn_if_settings_world_readable(path: str) -> None:
    """Best-effort POSIX permission check on the settings file.

    settings.yaml stores API tokens. If the file is group- or
    world-readable, anyone with shell access on the host can exfiltrate
    your bot credentials. We warn loudly but don't block startup —
    Windows hosts (Container Station builds run on a NAS, but the file
    is sometimes edited from a Windows share) get skipped because POSIX
    mode bits don't translate."""
    if os.name != "posix": return
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return
    other = mode & 0o007
    group = mode & 0o070
    if other or group:
        logger.warning(
            "%s is mode 0o%03o — readable by %s. It contains API tokens; "
            "consider `chmod 600 %s` to restrict to owner only.",
            path, mode,
            "world" if other else "group",
            path,
        )


async def main() -> int:
    """Returns a process exit code: 0 on clean shutdown, non-zero on
    fatal startup errors that the operator needs to fix.
    """
    logger.info("Bridger %s starting", _version_string())
    _warn_if_settings_world_readable(SETTINGS_PATH)

    try:
        settings = load_settings(SETTINGS_PATH)
    except FileNotFoundError:
        logger.error(
            "Settings file not found at %s. Copy example.settings.yaml to "
            "%s and fill in your credentials. (Inside Docker, this usually "
            "means the volume mount is missing or wrong — check the "
            "compose file's volumes:.)",
            SETTINGS_PATH, SETTINGS_PATH,
        )
        return 2
    except PermissionError as exc:
        logger.error("Cannot read %s: %s. Check file permissions.", SETTINGS_PATH, exc)
        return 2
    except yaml.YAMLError as exc:
        logger.error("settings.yaml has invalid YAML: %s. Fix the syntax error and restart.", exc)
        return 2

    if not isinstance(settings, dict):
        logger.error("settings.yaml must be a YAML mapping at the top level (got %s)",
                     type(settings).__name__)
        return 2

    _apply_timezone(settings)

    try:
        ensure_directories(settings)
    except PermissionError as exc:
        logger.error(
            "Cannot create message directories: %s. The container needs write "
            "access to /app/messages — check the compose volume mapping and "
            "host directory ownership.", exc,
        )
        return 2
    except OSError as exc:
        logger.error("Cannot create message directories: %s", exc)
        return 2

    # Initialize the unified database
    try:
        await init_db(BRIDGE_DB)
    except (OSError, PermissionError) as exc:
        logger.error(
            "Cannot initialize the message-mapping database at %s: %s. "
            "Check write access on the messages volume.",
            BRIDGE_DB, exc,
        )
        return 2
    except Exception as exc:
        logger.error("Database init failed: %s", exc, exc_info=True)
        return 2
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
        return 2

    # Create the router and wire it into each platform
    forward_reactions = bool(settings.get("forward_reactions", False))
    router = Router(platforms, bridges, db_path=BRIDGE_DB, forward_reactions=forward_reactions)
    logger.info("Reaction forwarding: %s", "ON" if forward_reactions else "OFF")
    for p in platforms.values():
        p.router = router

    def reload(new_settings: dict) -> None:
        """Apply a re-read settings file in-place. Bridges and per-platform
        mention filters update live; credentials and platform set do not."""
        _apply_timezone(new_settings)
        new_bridges = get_bridges(new_settings)
        new_enabled = set(get_enabled_platforms(new_settings))
        if new_enabled != set(platforms.keys()):
            logger.warning(
                "Platform set changed in settings.yaml (was %s, now %s) — restart required for that to take effect",
                sorted(platforms.keys()), sorted(new_enabled),
            )
        router.bridges = new_bridges
        new_forward_reactions = bool(new_settings.get("forward_reactions", False))
        if new_forward_reactions != router.forward_reactions:
            logger.info("Reaction forwarding toggled: %s → %s",
                        "ON" if router.forward_reactions else "OFF",
                        "ON" if new_forward_reactions else "OFF")
            router.forward_reactions = new_forward_reactions
        for name, platform in platforms.items():
            platform.bridges = new_bridges
            platform.apply_config(get_platform_config(new_settings, name))
        new_janitor_cfg = new_settings.get("janitor") or {}
        new_max_age = max(60.0, float(new_janitor_cfg.get("max_age_seconds", 3600)))
        new_interval = max(60.0, float(new_janitor_cfg.get("interval_seconds", 1800)))
        new_db_ttl = max(0.0, float(new_janitor_cfg.get("db_ttl_seconds", 30 * 24 * 3600)))
        if (new_max_age != janitor.max_age_seconds or new_interval != janitor.interval_seconds
                or new_db_ttl != janitor.db_ttl_seconds):
            logger.info(
                "Janitor settings updated: max_age %ds → %ds, interval %ds → %ds, db_ttl %ds → %ds",
                int(janitor.max_age_seconds), int(new_max_age),
                int(janitor.interval_seconds), int(new_interval),
                int(janitor.db_ttl_seconds), int(new_db_ttl),
            )
            janitor.max_age_seconds = new_max_age
            janitor.interval_seconds = new_interval
            janitor.db_ttl_seconds = new_db_ttl
        logger.info("Reload complete: %d bridge(s)", len(new_bridges))

    janitor_cfg = settings.get("janitor") or {}
    janitor = MediaJanitor(
        platform_names=platforms.keys(),
        max_age_seconds=float(janitor_cfg.get("max_age_seconds", 3600)),
        interval_seconds=float(janitor_cfg.get("interval_seconds", 1800)),
        db_path=BRIDGE_DB,
        db_ttl_seconds=float(janitor_cfg.get("db_ttl_seconds", 30 * 24 * 3600)),
    )
    await janitor.start()

    watcher = SettingsWatcher(SETTINGS_PATH, reload)
    await watcher.start()

    logger.info(
        "Starting %d platform(s): %s",
        len(platforms), ", ".join(platforms.keys()),
    )
    await asyncio.gather(*(p.start() for p in platforms.values()))
    return 0


if __name__ == "__main__":
    setup_logging()
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down on Ctrl-C")
        rc = 0
    except Exception as exc:
        # Truly unexpected — log a single line + traceback under our
        # logger and exit non-zero so Container Station / Docker
        # restart-policy can decide what to do.
        logger.error("Fatal error in main: %s", exc, exc_info=True)
        rc = 1
    sys.exit(rc or 0)
