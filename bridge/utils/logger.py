"""
Centralized logging configuration for the bridge application.
"""

import logging
import os
import re
from logging.handlers import RotatingFileHandler

# Strip control chars (NUL, BS, LF, CR, ESC, etc.) — the building blocks of
# log forging and terminal-escape injection. Keep TAB; everything else in
# the C0 range plus DEL becomes a visible \xNN escape so the byte still
# shows up but can't break out of the log line.
_LOG_UNSAFE_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def safe_for_log(s, max_len: int = 200) -> str:
    """Sanitize untrusted text for inclusion in log lines.

    Replaces control characters and CRLF — used by hostile senders to
    forge fake log entries or inject terminal escape sequences when the
    log is viewed via ``docker logs`` — with visible ``\\xNN`` escapes.
    Truncates at *max_len* characters."""
    if s is None: return ""
    if not isinstance(s, str): s = str(s)
    cleaned = _LOG_UNSAFE_RE.sub(lambda m: f"\\x{ord(m.group()):02x}", s)
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len] + "…"
    return cleaned

_LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_configured = False

def setup_logging(*, level: int = logging.INFO, log_dir: str = "logs") -> None:
    """Configure the root ``bridge`` logger.

    * Console handler — always added.
    * Rotating file handler — writes to ``<log_dir>/bridge.log``
        (5 MB per file, 3 backups).
    """
    global _configured
    if _configured: return
    _configured = True

    root = logging.getLogger("bridge")
    root.setLevel(level)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # Console
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    # File (rotating)
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "bridge.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``bridge`` namespace.
    Example::
        logger = get_logger("telegram")  # → logger named "bridge.telegram"
    """
    return logging.getLogger(f"bridge.{name}")
