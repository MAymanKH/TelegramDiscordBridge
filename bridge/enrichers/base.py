"""
Enricher interface + result type.
"""

from dataclasses import dataclass, field


@dataclass
class EnrichResult:
    """Outcome of enriching one URL.

    *author* and *text* are RAW user content from the source site — the
    router escapes them per destination platform before sending. *media*
    is a list of ``(file_path, ext)`` tuples already downloaded to disk;
    the router dispatches each via the normal ``on_file`` path (so size
    caps, transcoding, and cleanup all apply) and then removes the file.
    """
    author: str = ""
    text: str = ""
    media: list = field(default_factory=list)  # [(path, ext), ...]
    source_url: str = ""
    icon: str = "🔗"


class BaseEnricher:
    name = "base"
    icon = "🔗"

    def matches(self, url: str) -> bool:
        """True if this enricher handles *url*'s host. Acts as the SSRF
        allowlist gate — only matching URLs are ever fetched."""
        raise NotImplementedError

    async def enrich(self, url: str, cfg: dict, dest_dir: str) -> "EnrichResult | None":
        """Fetch text and/or download media for *url*.

        Returns None on any failure (deleted post, rate limit, login wall,
        timeout) so the caller falls back to forwarding the bare link.
        Downloaded media goes under *dest_dir*."""
        raise NotImplementedError
