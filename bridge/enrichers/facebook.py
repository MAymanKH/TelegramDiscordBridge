"""
Facebook enricher — media-only via yt-dlp (public videos / reels).

No free JSON API for Facebook, and most posts are login-walled. Private
or photo-only posts will simply yield nothing and the caller falls back
to forwarding the bare link.
"""

import re

from bridge.enrichers.ytdlp import YtDlpEnricher


class FacebookEnricher(YtDlpEnricher):
    name = "facebook"
    icon = "📘"
    author_label = "Facebook"
    host_re = re.compile(
        r"https?://(?:www\.|m\.|web\.|mbasic\.)?(?:facebook\.com|fb\.watch)/\S+",
        re.IGNORECASE,
    )
    fetch_caption = False  # FB titles via yt-dlp are usually noise
