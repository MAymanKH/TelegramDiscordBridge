"""
Link enrichment — when a bridged message contains a link to a supported
site, optionally fetch the post's text and/or download its media and
append it to the destination chat.

The original message (including the link) is always forwarded unchanged;
enrichment is sent as a follow-up. Opt-in per bridge via the `enrich:`
config block. See :func:`bridge.utils.config.bridge_enrich_config`.

Only allowlisted hosts are ever fetched — the per-enricher ``matches()``
gate is the SSRF guard. A URL that no enricher matches is never touched.
"""

import re

from bridge.enrichers.twitter import TwitterEnricher
from bridge.enrichers.facebook import FacebookEnricher
from bridge.enrichers.instagram import InstagramEnricher
from bridge.enrichers.tiktok import TikTokEnricher

# Conservative URL grab — stops at whitespace and common closing delimiters.
_URL_RE = re.compile(r"https?://[^\s<>\"'\])}]+")

_ENRICHERS = {
    "twitter": TwitterEnricher(),
    "facebook": FacebookEnricher(),
    "instagram": InstagramEnricher(),
    "tiktok": TikTokEnricher(),
}


def extract_urls(text: str) -> list[str]:
    """Return all http(s) URLs found in *text* (order-preserving, deduped)."""
    if not text: return []
    seen: set[str] = set()
    out: list[str] = []
    for u in _URL_RE.findall(text):
        u = u.rstrip(".,;!?")  # trailing sentence punctuation isn't part of the URL
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def get_enricher(url: str, providers: list[str]):
    """Return the first enabled enricher whose host matches *url*, or None.

    Iterating *providers* (not the full registry) means a bridge only ever
    invokes the enrichers it opted into."""
    for name in providers:
        enricher = _ENRICHERS.get(name)
        if enricher is not None and enricher.matches(url):
            return enricher
    return None
