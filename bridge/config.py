"""
Configuration loader and path constants.
"""

import os
import yaml

# Directory / file path constants
MESSAGES_DIR = "messages"
TELEGRAM_DIR = os.path.join(MESSAGES_DIR, "telegram")
DISCORD_DIR = os.path.join(MESSAGES_DIR, "discord")
TELEGRAM_DB = os.path.join(TELEGRAM_DIR, "text.db")
DISCORD_DB = os.path.join(DISCORD_DIR, "text.db")

# Settings
def load_settings(path: str = "settings.yaml") -> dict:
    """Load and return the YAML settings file."""
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)

def get_bridges(settings: dict) -> list[dict]:
    """Return the list of bridge definitions from *settings*."""
    return settings["bridges"]

# Bootstrap
def ensure_directories() -> None:
    """Create required message directories."""
    for folder in (TELEGRAM_DIR, DISCORD_DIR):
        os.makedirs(folder, exist_ok=True)

