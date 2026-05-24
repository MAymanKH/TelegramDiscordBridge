"""
Media / attachment helpers and file classification.
"""

import mimetypes
import os
import re
from bridge.utils.logger import get_logger

logger = get_logger("media")

# Anything that could let a user-supplied filename escape its target directory
# or be misinterpreted by tools we shell out to (ffmpeg flag injection).
_UNSAFE_NAME_CHARS = re.compile(r"[\x00-\x1f<>:\"/\\|?*]")


def safe_basename(name: str, fallback: str = "file") -> str:
    """Sanitize a user-supplied filename component.

    Strips directory separators, parent-traversal markers, control chars,
    and characters that are illegal on common filesystems. Also rewrites
    leading dots and dashes (hidden files / argv-style flags) so the
    result is always a single, controlled filename component.

    Returns *fallback* if the input is empty or sanitizes to nothing.
    """
    if not name: return fallback
    # os.path.basename strips any directory components (incl. "../etc")
    base = os.path.basename(name).strip()
    # Replace remaining dangerous chars with underscore
    base = _UNSAFE_NAME_CHARS.sub("_", base)
    # Collapse runs of dots that could still form ".." after the sub above
    base = re.sub(r"\.\.+", "_", base)
    # Avoid leading dots (hidden files) and dashes (argv flags)
    while base and base[0] in (".", "-"):
        base = "_" + base[1:]
    return base or fallback

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

    Both *file_name* and *file_type* are sanitized through :func:`safe_basename`
    so the result is always a child of *directory* — no path traversal even if
    the originating message contained a malicious filename like ``../../etc/x``.
    A defensive ``commonpath`` check at the end raises ``ValueError`` if the
    sanitization is somehow defeated.
    """
    safe_name = safe_basename(file_name, fallback="file")
    safe_ext = safe_basename(file_type, fallback="").lstrip("_") if file_type else ""
    if safe_ext and not safe_ext.startswith("."): safe_ext = "." + safe_ext

    abs_dir = os.path.abspath(directory)

    def _check(path: str) -> str:
        abs_path = os.path.abspath(path)
        # commonpath on different drives raises ValueError; treat that as escape
        try: common = os.path.commonpath([abs_dir, abs_path])
        except ValueError: common = ""
        if common != abs_dir:
            raise ValueError(f"Refusing to write outside {abs_dir}: {abs_path}")
        return path

    file_path = _check(os.path.join(directory, f"{safe_name}{safe_ext}"))
    if not os.path.isfile(file_path): return file_path
    counter = 2
    while True:
        candidate = _check(os.path.join(directory, f"{safe_name}_({counter}){safe_ext}"))
        if not os.path.isfile(candidate): return candidate
        counter += 1
