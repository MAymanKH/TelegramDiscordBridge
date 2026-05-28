"""
Shared yt-dlp-backed enricher base.

Facebook, Instagram, and TikTok have no convenient free JSON API, so we
lean on yt-dlp to download public media (and, where useful, the caption).
Subclasses just set a host regex, an icon, an author label, and whether
to fetch the caption.
"""

import asyncio
import os
import shutil

from bridge.enrichers.base import BaseEnricher, EnrichResult
from bridge.utils.logger import get_logger

logger = get_logger("enrich.ytdlp")

_YTDLP_TIMEOUT = 180.0


class YtDlpEnricher(BaseEnricher):
    """Base for media-first enrichers that shell out to yt-dlp."""

    name = "ytdlp"
    icon = "🔗"
    author_label = ""
    host_re = None          # subclass: compiled regex, .search() must match the URL
    fetch_caption = False   # subclass: True to also pull the title/caption

    def matches(self, url: str) -> bool:
        return bool(self.host_re.search(url)) if self.host_re else False

    async def enrich(self, url: str, cfg: dict, dest_dir: str) -> "EnrichResult | None":
        if not cfg.get("download_media"):
            return None
        m = self.host_re.search(url) if self.host_re else None
        if not m: return None
        clean_url = m.group(0)

        if shutil.which("yt-dlp") is None:
            logger.warning("yt-dlp not found on PATH — cannot enrich %s media", self.name)
            return None

        os.makedirs(dest_dir, exist_ok=True)
        max_mb = int(cfg.get("max_media_mb", 50))
        out_tmpl = os.path.join(dest_dir, f"{self.name}_media_%(id)s.%(ext)s")
        args = [
            "yt-dlp",
            "--no-playlist",
            "--no-warnings",
            "--max-filesize", f"{max_mb}M",
            "-o", out_tmpl,
        ]
        # Caption (if requested) prints at info-extraction; filepath prints
        # after the file is moved into place. yt-dlp emits them in event
        # order, so the filepath is always the LAST stdout line.
        if self.fetch_caption:
            args += ["--print", "%(title)s"]
        args += ["--print", "after_move:filepath", clean_url]

        path, caption = await self._run(args)
        if not path or not os.path.isfile(path):
            return None
        ext = os.path.splitext(path)[1] or ".mp4"
        return EnrichResult(
            author=self.author_label,
            text=(caption or "").strip(),
            media=[(path, ext)],
            source_url=url,
            icon=self.icon,
        )

    async def _run(self, args: list[str]) -> tuple:
        """Run yt-dlp. Returns ``(filepath_or_None, caption)``."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.warning("failed to launch yt-dlp: %s", exc)
            return None, ""
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_YTDLP_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("yt-dlp timed out after %.0fs (%s) — killing", _YTDLP_TIMEOUT, self.name)
            try: proc.kill()
            except ProcessLookupError: pass
            try: await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError): pass
            return None, ""
        if proc.returncode != 0:
            logger.info("yt-dlp failed for %s (rc=%d): %s", self.name, proc.returncode,
                        stderr[:300].decode("utf-8", errors="replace"))
            return None, ""
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        if not lines:
            return None, ""
        path = lines[-1].strip()
        caption = "\n".join(lines[:-1]).strip() if self.fetch_caption else ""
        return path, caption
