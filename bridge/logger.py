"""
Centralized logging configuration for the bridge application.

Provides a ``get_logger`` helper so every module can obtain a named logger
with a consistent format.  The root ``bridge`` logger is configured once
via ``setup_logging``.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

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
    if _configured:
        return
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
