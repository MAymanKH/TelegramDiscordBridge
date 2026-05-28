"""
Unit tests for the link-enrichment subsystem.

Covers the pure / offline parts: URL extraction, host matching (the SSRF
allowlist gate), config parsing, and the destination-safe formatter.
Network fetches (fxtwitter, yt-dlp) are intentionally NOT exercised here —
they're isolated behind `enrich()` and require live services.
"""

import sys
import unittest
from unittest.mock import MagicMock

# Stub heavy / network deps so the enricher modules import cleanly.
sys.modules.setdefault("aiosqlite", MagicMock())
sys.modules.setdefault("aiohttp", MagicMock())

from bridge.enrichers import extract_urls, get_enricher
from bridge.enrichers.twitter import TwitterEnricher
from bridge.enrichers.facebook import FacebookEnricher
from bridge.enrichers.instagram import InstagramEnricher
from bridge.enrichers.tiktok import TikTokEnricher
from bridge.enrichers.base import EnrichResult
from bridge.router import _format_enrichment
from bridge.utils.config import bridge_enrich_config


class TestExtractUrls(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertEqual(extract_urls(None), [])
        self.assertEqual(extract_urls(""), [])

    def test_single(self):
        self.assertEqual(extract_urls("see https://x.com/a/status/1"), ["https://x.com/a/status/1"])

    def test_multiple_dedup_order(self):
        text = "https://x.com/a/status/1 and https://x.com/a/status/1 then https://fb.watch/xyz"
        self.assertEqual(
            extract_urls(text),
            ["https://x.com/a/status/1", "https://fb.watch/xyz"],
        )

    def test_strips_trailing_punctuation(self):
        self.assertEqual(extract_urls("look: https://x.com/a/status/9."),
                         ["https://x.com/a/status/9"])

    def test_ignores_non_http(self):
        self.assertEqual(extract_urls("ftp://x.com/a no http here"), [])


class TestEnricherMatching(unittest.TestCase):
    def setUp(self):
        self.tw = TwitterEnricher()
        self.fb = FacebookEnricher()

    def test_twitter_matches_x_and_twitter(self):
        self.assertTrue(self.tw.matches("https://x.com/jack/status/20"))
        self.assertTrue(self.tw.matches("https://twitter.com/jack/status/20"))
        self.assertTrue(self.tw.matches("https://mobile.twitter.com/jack/status/20"))

    def test_twitter_rejects_non_status(self):
        self.assertFalse(self.tw.matches("https://x.com/jack"))  # profile, not a post
        self.assertFalse(self.tw.matches("https://example.com/x.com/status/1"))

    def test_facebook_matches(self):
        self.assertTrue(self.fb.matches("https://facebook.com/watch/?v=123"))
        self.assertTrue(self.fb.matches("https://fb.watch/abcDEF/"))
        self.assertTrue(self.fb.matches("https://www.facebook.com/page/videos/123"))

    def test_facebook_rejects_others(self):
        self.assertFalse(self.fb.matches("https://x.com/a/status/1"))

    def test_instagram_matches(self):
        ig = InstagramEnricher()
        self.assertTrue(ig.matches("https://instagram.com/p/Abc123/"))
        self.assertTrue(ig.matches("https://www.instagram.com/reel/Xyz789/"))
        self.assertTrue(ig.matches("https://instagram.com/tv/Qwe456"))
        # username-prefixed forms (instagram.com/<user>/reel/<id>/)
        self.assertTrue(ig.matches("https://www.instagram.com/some.user_1/reel/DXY123/"))
        self.assertTrue(ig.matches("https://instagram.com/someuser/p/ABC/"))
        # short domain + share links
        self.assertTrue(ig.matches("https://instagr.am/p/ABC123/"))
        self.assertTrue(ig.matches("https://www.instagram.com/share/reel/ABC123/"))
        # not a post
        self.assertFalse(ig.matches("https://instagram.com/someuser"))
        self.assertFalse(ig.matches("https://x.com/a/status/1"))

    def test_tiktok_matches(self):
        tk = TikTokEnricher()
        self.assertTrue(tk.matches("https://www.tiktok.com/@user/video/123"))
        self.assertTrue(tk.matches("https://vm.tiktok.com/ZMabc/"))
        self.assertTrue(tk.matches("https://vt.tiktok.com/ZMxyz/"))
        self.assertFalse(tk.matches("https://x.com/a/status/1"))

    def test_registry_resolves_all_four(self):
        provs = ["twitter", "facebook", "instagram", "tiktok"]
        self.assertIsInstance(get_enricher("https://x.com/a/status/1", provs), TwitterEnricher)
        self.assertIsInstance(get_enricher("https://fb.watch/abc/", provs), FacebookEnricher)
        self.assertIsInstance(get_enricher("https://instagram.com/reel/x/", provs), InstagramEnricher)
        self.assertIsInstance(get_enricher("https://vm.tiktok.com/x/", provs), TikTokEnricher)

    def test_ssrf_guard_unknown_host_no_enricher(self):
        # The SSRF guard: a non-allowlisted host matches no enricher, so it
        # is never fetched.
        self.assertIsNone(get_enricher("http://169.254.169.254/latest/meta-data/", ["twitter", "facebook"]))
        self.assertIsNone(get_enricher("https://evil.example.com/x.com/status/1", ["twitter", "facebook"]))

    def test_get_enricher_respects_provider_list(self):
        # If only 'facebook' is enabled, an X link gets no enricher.
        self.assertIsNone(get_enricher("https://x.com/a/status/1", ["facebook"]))
        self.assertIsInstance(get_enricher("https://x.com/a/status/1", ["twitter"]), TwitterEnricher)


class TestTwitterMediaHostGuard(unittest.TestCase):
    def test_allows_twitter_cdn(self):
        self.assertTrue(TwitterEnricher._allowed_media_host("https://pbs.twimg.com/media/x.jpg"))
        self.assertTrue(TwitterEnricher._allowed_media_host("https://video.twimg.com/v/x.mp4"))

    def test_rejects_other_hosts(self):
        self.assertFalse(TwitterEnricher._allowed_media_host("https://evil.com/x.jpg"))
        # lookalike that merely contains the CDN host as a substring
        self.assertFalse(TwitterEnricher._allowed_media_host("https://pbs.twimg.com.evil.com/x.jpg"))


class TestEnrichConfig(unittest.TestCase):
    def test_disabled(self):
        self.assertIsNone(bridge_enrich_config({}))
        self.assertIsNone(bridge_enrich_config({"enrich": {"enabled": False}}))

    def test_defaults(self):
        cfg = bridge_enrich_config({"enrich": {"enabled": True}})
        self.assertEqual(cfg["providers"], ["twitter", "facebook", "instagram", "tiktok"])
        self.assertTrue(cfg["download_media"])
        self.assertEqual(cfg["max_media_mb"], 50.0)
        self.assertEqual(cfg["max_links"], 3)
        self.assertTrue(cfg["quote_original"])

    def test_quote_original_can_disable(self):
        cfg = bridge_enrich_config({"enrich": {"enabled": True, "quote_original": False}})
        self.assertFalse(cfg["quote_original"])

    def test_cookies_file_default_none(self):
        cfg = bridge_enrich_config({"enrich": {"enabled": True}})
        self.assertIsNone(cfg["cookies_file"])

    def test_cookies_file_parsed(self):
        cfg = bridge_enrich_config({"enrich": {"enabled": True, "cookies_file": "/app/x/cookies.txt"}})
        self.assertEqual(cfg["cookies_file"], "/app/x/cookies.txt")

    def test_provider_string_coerced_to_list(self):
        cfg = bridge_enrich_config({"enrich": {"enabled": True, "providers": "twitter"}})
        self.assertEqual(cfg["providers"], ["twitter"])

    def test_caps_clamped(self):
        cfg = bridge_enrich_config({"enrich": {"enabled": True, "max_media_mb": 0, "max_links": 0}})
        self.assertGreaterEqual(cfg["max_media_mb"], 1.0)
        self.assertGreaterEqual(cfg["max_links"], 1)


class TestFormatEnrichment(unittest.TestCase):
    def test_author_and_text(self):
        r = EnrichResult(author="Jack", text="hello world", icon="🐦")
        out = _format_enrichment(r, lambda s: s)
        self.assertEqual(out, "🐦 Jack:\nhello world")

    def test_escape_applied(self):
        # The escape fn must be applied to BOTH author and text.
        r = EnrichResult(author="<b>", text="<script>", icon="🐦")
        out = _format_enrichment(r, lambda s: s.replace("<", "&lt;").replace(">", "&gt;"))
        self.assertNotIn("<b>", out)
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;b&gt;", out)

    def test_text_only(self):
        # No author → icon prefixes the text directly, no stray colon.
        r = EnrichResult(author="", text="just text", icon="")
        out = _format_enrichment(r, lambda s: s)
        self.assertEqual(out, "🔗 just text")

    def test_author_only(self):
        r = EnrichResult(author="Jack", text="", icon="🐦")
        out = _format_enrichment(r, lambda s: s)
        self.assertEqual(out, "🐦 Jack")

    def test_empty(self):
        r = EnrichResult(author="", text="", icon="🐦")
        self.assertEqual(_format_enrichment(r, lambda s: s), "")


if __name__ == "__main__":
    unittest.main()
