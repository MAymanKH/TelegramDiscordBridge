"""
X / Twitter enricher.

Uses the free community fxtwitter API (https://github.com/FixTweet/FxTwitter)
to fetch a single post's text + media URLs without auth or the paid X API.
Media is downloaded directly from Twitter's CDN (validated host). No full
thread unroll — single post only (MVP scope).
"""

import os
import re
from urllib.parse import urlparse

from bridge.enrichers.base import BaseEnricher, EnrichResult
from bridge.utils.media import get_unique_filepath
from bridge.utils.logger import get_logger

logger = get_logger("enrich.twitter")

_STATUS_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/([A-Za-z0-9_]+)/status/(\d+)",
    re.IGNORECASE,
)
_FX_API = "https://api.fxtwitter.com/{screen}/status/{tid}"
# Media must come from Twitter's own CDN — defense in depth so a tampered
# API response can't make us fetch an arbitrary host.
_ALLOWED_MEDIA_HOSTS = ("pbs.twimg.com", "video.twimg.com")
_FETCH_TIMEOUT = 20.0
_DOWNLOAD_TIMEOUT = 120.0
_CHUNK = 64 * 1024


class TwitterEnricher(BaseEnricher):
    name = "twitter"
    icon = "🐦"

    def matches(self, url: str) -> bool:
        return bool(_STATUS_RE.search(url))

    async def enrich(self, url: str, cfg: dict, dest_dir: str) -> "EnrichResult | None":
        m = _STATUS_RE.search(url)
        if not m: return None
        screen, tid = m.group(1), m.group(2)
        data = await self._fetch_json(screen, tid)
        if not data: return None
        tweet = data.get("tweet") or {}
        author_obj = tweet.get("author") or {}
        author = author_obj.get("name") or author_obj.get("screen_name") or screen
        text = (tweet.get("text") or "").strip()

        media: list = []
        if cfg.get("download_media"):
            media = await self._download_media(tweet, cfg, dest_dir)

        if not text and not media:
            return None
        return EnrichResult(author=author, text=text, media=media, source_url=url, icon=self.icon)

    async def _fetch_json(self, screen: str, tid: str):
        import aiohttp
        api_url = _FX_API.format(screen=screen, tid=tid)
        try:
            timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(api_url) as r:
                    if r.status != 200:
                        logger.info("fxtwitter returned %d for %s/%s", r.status, screen, tid)
                        return None
                    return await r.json()
        except Exception as exc:
            logger.warning("fxtwitter fetch failed for %s/%s: %s", screen, tid, exc)
            return None

    async def _download_media(self, tweet: dict, cfg: dict, dest_dir: str) -> list:
        import aiohttp
        media = tweet.get("media") or {}
        candidates: list[tuple[str, str]] = []
        for photo in (media.get("photos") or []):
            u = photo.get("url")
            if u: candidates.append((u, ".jpg"))
        for video in (media.get("videos") or []):
            u = video.get("url")
            if u: candidates.append((u, ".mp4"))
        if not candidates: return []

        max_bytes = int(cfg.get("max_media_mb", 50) * 1024 * 1024)
        os.makedirs(dest_dir, exist_ok=True)
        out: list = []
        timeout = aiohttp.ClientTimeout(total=_DOWNLOAD_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            for u, ext in candidates:
                if not self._allowed_media_host(u):
                    logger.warning("Refusing non-CDN media URL: %s", u)
                    continue
                path = get_unique_filepath(dest_dir, "x_media", ext)
                if await self._download_one(sess, u, path, max_bytes):
                    out.append((path, ext))
        return out

    @staticmethod
    def _allowed_media_host(u: str) -> bool:
        host = (urlparse(u).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in _ALLOWED_MEDIA_HOSTS)

    async def _download_one(self, sess, url: str, path: str, max_bytes: int) -> bool:
        try:
            async with sess.get(url) as r:
                if r.status != 200:
                    logger.info("media GET %s -> %d", url, r.status)
                    return False
                total = 0
                with open(path, "wb") as fh:
                    async for chunk in r.content.iter_chunked(_CHUNK):
                        total += len(chunk)
                        if total > max_bytes:
                            logger.warning("media exceeds %d bytes, aborting: %s", max_bytes, url)
                            try: os.remove(path)
                            except OSError: pass
                            return False
                        fh.write(chunk)
                return True
        except Exception as exc:
            logger.warning("media download failed %s: %s", url, exc)
            try: os.remove(path)
            except OSError: pass
            return False
