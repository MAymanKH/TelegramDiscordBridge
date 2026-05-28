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
    # Matches the common post/reel/tv forms, the optional <username>/ prefix
    # (instagram.com/someuser/reel/<id>/), share links, and the instagr.am
    # short domain.
    host_re = re.compile(
        r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)/"
        r"(?:[A-Za-z0-9_.]+/)?(?:p|reel|reels|tv|share)/[\w-]+",
        re.IGNORECASE,
    )
    fetch_caption = True
