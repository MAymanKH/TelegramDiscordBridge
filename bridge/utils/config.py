"""
Configuration loader and path constants.
"""

import os
import yaml

# Base directory for all platform message storage
MESSAGES_DIR = "messages"

# Central database for message mapping
BRIDGE_DB = os.path.join(MESSAGES_DIR, "bridge.db")

def platform_dir(name: str) -> str:
    """Return the message-storage directory for a given platform."""
    return os.path.join(MESSAGES_DIR, name)

def platform_media_dir(name: str) -> str:
    """Return the media directory for a given platform (for downloaded files)."""
    return platform_dir(name)

# Settings helpers
def load_settings(path: str = "settings.yaml") -> dict:
    """Load and return the YAML settings file."""
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)

def get_enabled_platforms(settings: dict) -> list[str]:
    """Return a sorted list of platform keys defined in *settings*."""
    return sorted(settings.get("platforms", {}).keys())

def get_platform_config(settings: dict, platform_name: str) -> dict:
    """Return the config dict for a single platform."""
    return settings["platforms"][platform_name]

def get_bridges(settings: dict) -> list[dict]:
    """Return the list of bridge definitions from *settings*.
    Each bridge has::
        {"name": "...", "platforms": {"telegram": chat_id, "discord": channel_id, ...}}
    """
    return settings.get("bridges", [])

# Bootstrap
def ensure_directories(settings: dict) -> None:
    """Create required message directories for every enabled platform."""
    os.makedirs(MESSAGES_DIR, exist_ok=True)
    for name in get_enabled_platforms(settings):
        os.makedirs(platform_dir(name), exist_ok=True)
