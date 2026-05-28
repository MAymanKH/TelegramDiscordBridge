"""
Instagram enricher — public posts / reels / IGTV via yt-dlp.

Pulls the video/image plus the caption. Private accounts and some posts
are login-walled; those yield nothing and the caller falls back to the
bare link.
"""

import re

from bridge.enrichers.ytdlp import YtDlpEnricher


class InstagramEnricher(YtDlpEnricher):
    name = "instagram"
    icon = "📸"
    author_label = "Instagram"
    host_re = re.compile(
        r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/[\w-]+",
        re.IGNORECASE,
    )
    fetch_caption = True
