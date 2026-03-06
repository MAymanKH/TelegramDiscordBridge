"""
Media / attachment helpers and file-type constants.
"""

import os
from bridge.logger import get_logger

logger = get_logger("media")

# File-type sets
PHOTO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif"}
MEDIA_EXTENSIONS = PHOTO_EXTENSIONS | {".webp", ".mp4", ".mp3", ".ogg", ".pdf", ".apk"}

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

