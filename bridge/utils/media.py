"""
Media / attachment helpers and file classification.
"""

import mimetypes
import os
from bridge.utils.logger import get_logger

logger = get_logger("media")

def classify_attachment(file_path: str, file_ext: str = "") -> str:
    """Return the outbound attachment kind for *file_path*.

    The result is one of: ``photo``, ``video``, ``audio``, ``voice``,
    ``sticker``, or ``document``.
    """
    ext = (file_ext or os.path.splitext(file_path)[1]).lower()
    mime_type, _ = mimetypes.guess_type(file_path)

    if ext == ".webp": return "sticker"
    if ext == ".ogg": return "voice"

    if mime_type:
        primary_type = mime_type.split("/", 1)[0]
        if primary_type == "image": return "photo"
        if primary_type == "video": return "video"
        if primary_type == "audio": return "audio"

    return "document"

# Helpers
def get_unique_filepath(directory: str, file_name: str, file_type: str) -> str:
    """Generate a unique file path, appending a counter if the file already exists.
    *file_type* should include the leading dot, e.g. ``'.png'``.
    """
    file_path = os.path.join(directory, f"{file_name}{file_type}")
    if not os.path.isfile(file_path): return file_path
    counter = 2
    while True:
        candidate = os.path.join(directory, f"{file_name}_({counter}){file_type}")
        if not os.path.isfile(candidate): return candidate
        counter += 1
