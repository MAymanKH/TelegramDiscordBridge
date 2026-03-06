"""
Media / attachment helpers and file-type constants.
"""

import json
import os
from bridge.logger import get_logger

logger = get_logger("media")

# File-type sets

PHOTO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif"}
MEDIA_EXTENSIONS = PHOTO_EXTENSIONS | {".webp", ".mp4", ".mp3", ".ogg", ".pdf", ".apk"}
IGNORED_FILES = {"attachments.json", "text.json", "text.db"}

# Helpers

def get_unique_filepath(directory: str, file_name: str, file_type: str) -> str:
    """Generate a unique file path, appending a counter if the file already exists.

    *file_type* should include the leading dot, e.g. ``'.png'``.
    """
    file_path = os.path.join(directory, f"{file_name}{file_type}")
    if not os.path.isfile(file_path):
        return file_path
    counter = 2
    while True:
        candidate = os.path.join(directory, f"{file_name}_({counter}){file_type}")
        if not os.path.isfile(candidate):
            return candidate
        counter += 1

def save_attachment_json(
    json_file_path: str,
    file_path: str,
    sender: str,
    chat: str,
) -> None:
    """Save attachment metadata to a JSON file."""
    with open(json_file_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    data["message"] = {
        "path": file_path,
        "sender": sender,
        "chat": chat,
    }
    with open(json_file_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True, indent=4, ensure_ascii=False)
    logger.debug("Saved attachment metadata: %s from %s", file_path, sender)
