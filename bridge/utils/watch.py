"""
Polling file watcher for hot-reloading settings.yaml.
"""

import asyncio
import os
import yaml
from typing import Awaitable, Callable
from bridge.utils.logger import get_logger

logger = get_logger("watch")

class SettingsWatcher:
    """Polls *path* for mtime changes and invokes *on_change(new_settings)*.

    The callback may be sync or async. Parse errors are caught so a malformed
    save doesn't crash the process — the previous settings stay in effect
    until the next clean save.
    """

    def __init__(self, path: str, on_change: Callable[[dict], Awaitable | None], interval: float = 2.0):
        self.path = path
        self.on_change = on_change
        self.interval = interval
        self._mtime: float | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        try: self._mtime = os.path.getmtime(self.path)
        except OSError: self._mtime = None
        self._task = asyncio.create_task(self._loop(), name="settings-watcher")
        logger.info("Watching %s for changes (poll every %.1fs)", self.path, self.interval)

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval)
                try: mtime = os.path.getmtime(self.path)
                except OSError: continue
                if self._mtime is not None and mtime == self._mtime: continue
                self._mtime = mtime
                try:
                    with open(self.path, "r", encoding="utf-8") as fh:
                        new_settings = yaml.safe_load(fh)
                except Exception as exc:
                    logger.error("Failed to parse %s on change: %s", self.path, exc)
                    continue
                logger.info("Settings file changed — applying")
                try:
                    result = self.on_change(new_settings)
                    if asyncio.iscoroutine(result): await result
                except Exception as exc:
                    logger.error("Failed to apply new settings: %s", exc, exc_info=True)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("Watcher loop error: %s", exc, exc_info=True)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try: await self._task
            except (asyncio.CancelledError, Exception): pass
            self._task = None
