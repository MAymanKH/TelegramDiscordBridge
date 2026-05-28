"""
TikTok enricher — public videos via yt-dlp (often watermark-free).

Handles full URLs and the vm./vt. short links (yt-dlp follows the
redirect). Pulls the video plus its caption.
"""

import re

from bridge.enrichers.ytdlp import YtDlpEnricher


class TikTokEnricher(YtDlpEnricher):
    name = "tiktok"
    icon = "🎵"
    author_label = "TikTok"
    host_re = re.compile(
        r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/\S+",
        re.IGNORECASE,
    )
    fetch_caption = True
