"""
Periodic janitor that sweeps orphaned media files.

Normal happy path deletes downloaded media and transcoded temp files
right after they're sent. If a send raises mid-flight (network blip,
provider 5xx), the cleanup line never runs and the file is left behind.
This module provides :class:`MediaJanitor` — a background task that
periodically scans known media directories and deletes anything older
than ``max_age_seconds``.

Designed to be safe-by-default:

* Only files in the platform media directories (``messages/<name>/``) and
  bridge-owned temp files (``/tmp/bridge_xcode_*``) are considered.
* Database / session files (``*.db``, ``*.session``, ``*.session-journal``,
  ``*.journal``) are explicitly skipped — never delete WhatsApp/Pyrogram
  session state.
"""

import asyncio
import os
import tempfile
import time
from bridge.utils.config import platform_dir
from bridge.utils.logger import get_logger

logger = get_logger("janitor")

_PROTECTED_SUFFIXES = (".db", ".db-journal", ".session", ".session-journal", ".journal")
_TRANSCODE_PREFIX = "bridge_xcode_"

class MediaJanitor:
    """Background sweeper for orphaned media files and old DB rows.

    Two separate thresholds:
      * ``max_age_seconds`` — for filesystem media (default 1 hour). Tight
        because well-behaved deploys clean up media inline; anything still
        around is orphaned.
      * ``db_ttl_seconds`` — for ``message_map`` rows in ``bridge.db``
        (default 30 days). Loose because deleting rows costs the ability
        to resolve replies to that old of a message; not worth churning
        unless the user has been bridging for a long time.

    Set ``db_ttl_seconds=0`` to disable DB pruning entirely (keep rows
    forever) — the historical behavior."""

    def __init__(
        self,
        platform_names,
        max_age_seconds: float = 3600,
        interval_seconds: float = 1800,
        db_path: str | None = None,
        db_ttl_seconds: float = 30 * 24 * 3600,
    ):
        self.platform_names = list(platform_names)
        self.max_age_seconds = max(60.0, float(max_age_seconds))
        self.interval_seconds = max(60.0, float(interval_seconds))
        self.db_path = db_path
        # 0 (or negative) disables DB pruning entirely.
        self.db_ttl_seconds = max(0.0, float(db_ttl_seconds))
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task and not self._task.done(): return
        self._task = asyncio.create_task(self._loop(), name="media-janitor")
        logger.info(
            "Media janitor started — sweep every %.0fs, delete files older than %.0fs",
            self.interval_seconds, self.max_age_seconds,
        )

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval_seconds)
                await self.sweep_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("Janitor loop error: %s", exc, exc_info=True)

    async def sweep_once(self) -> int:
        """Run one sweep. Returns the number of items deleted (files +
        DB rows combined)."""
        cutoff = time.time() - self.max_age_seconds
        deleted = 0
        for name in self.platform_names:
            deleted += _sweep_dir(platform_dir(name), cutoff, predicate=_safe_to_delete_in_platform_dir)
        # Bridge-owned transcode output (current location: messages/transcoded/).
        from bridge.utils.transcode import TRANSCODE_DIR
        deleted += _sweep_dir(TRANSCODE_DIR, cutoff, predicate=_is_bridge_transcode_temp)
        # Legacy: pre-0.2 installs wrote transcoded files to /tmp directly.
        deleted += _sweep_dir(tempfile.gettempdir(), cutoff, predicate=_is_bridge_transcode_temp)
        if deleted:
            logger.info("Janitor swept %d orphaned media file(s)", deleted)

        # Prune old message-id mappings from the DB so the bridge.db file
        # can't grow without bound on a long-lived or chatty bridge.
        if self.db_path and self.db_ttl_seconds > 0:
            from bridge import database
            pruned = await database.prune_old_mappings(self.db_path, self.db_ttl_seconds)
            if pruned:
                logger.info("Janitor pruned %d old message-mapping row(s)", pruned)
                deleted += pruned
        return deleted

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try: await self._task
            except (asyncio.CancelledError, Exception): pass
            self._task = None


def _safe_to_delete_in_platform_dir(entry_name: str) -> bool:
    """Skip protected suffixes (database, session files)."""
    lower = entry_name.lower()
    return not any(lower.endswith(suffix) for suffix in _PROTECTED_SUFFIXES)


def _is_bridge_transcode_temp(entry_name: str) -> bool:
    """Only files this bridge produced via :mod:`bridge.utils.transcode`."""
    return entry_name.startswith(_TRANSCODE_PREFIX)


def _sweep_dir(dir_path: str, cutoff: float, predicate) -> int:
    """Delete files in *dir_path* older than *cutoff* whose name passes
    *predicate*. Returns count of files deleted."""
    if not os.path.isdir(dir_path): return 0
    deleted = 0
    try:
        with os.scandir(dir_path) as it:
            for entry in it:
                try:
                    if not entry.is_file(): continue
                    if not predicate(entry.name): continue
                    if entry.stat().st_mtime >= cutoff: continue
                    os.remove(entry.path)
                    deleted += 1
                    logger.debug("Janitor deleted %s", entry.path)
                except OSError as exc:
                    logger.debug("Janitor skip %s: %s", entry.path, exc)
    except OSError as exc:
        logger.debug("Janitor cannot scan %s: %s", dir_path, exc)
    return deleted
