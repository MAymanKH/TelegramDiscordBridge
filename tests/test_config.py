"""
Unit tests for the pure helpers in :mod:`bridge.utils.config`.

These cover the logic that drives bridge dispatch decisions: mention
filter normalization, mention-only detection, user-ID parsing, and the
bidirectional-vs-directional bridge resolvers. Pure functions, no I/O.
"""

import unittest

from bridge.utils.config import (
    normalize_mention_filters,
    normalize_user_ids,
    is_mention_only,
    bridge_sources,
    bridge_targets,
    bridge_all_platforms,
    is_directional_bridge,
    get_enabled_platforms,
    bridge_digest_config,
    parse_upload_limit_mb,
    effective_mention_filters,
    effective_always_forward_user_ids,
    effective_destination_size_limit_bytes,
)


class TestNormalizeMentionFilters(unittest.TestCase):
    def test_none_returns_empty_list(self):
        self.assertEqual(normalize_mention_filters(None), [])

    def test_single_string(self):
        self.assertEqual(normalize_mention_filters("@bot"), ["@bot"])

    def test_lowercases(self):
        self.assertEqual(normalize_mention_filters("@MyBot"), ["@mybot"])

    def test_strips_whitespace(self):
        self.assertEqual(normalize_mention_filters("  @bot  "), ["@bot"])

    def test_list_input(self):
        self.assertEqual(
            normalize_mention_filters(["@A", "#TAG", "!cmd"]),
            ["@a", "#tag", "!cmd"],
        )

    def test_drops_none_and_empty(self):
        self.assertEqual(
            normalize_mention_filters(["@bot", "", None, "  ", "#tag"]),
            ["@bot", "#tag"],
        )

    def test_coerces_non_strings(self):
        # If a YAML user accidentally writes a number, str() it.
        self.assertEqual(normalize_mention_filters(123), ["123"])


class TestNormalizeUserIds(unittest.TestCase):
    def test_none_returns_empty_set(self):
        self.assertEqual(normalize_user_ids(None), set())

    def test_single_int(self):
        self.assertEqual(normalize_user_ids(42), {42})

    def test_list(self):
        self.assertEqual(normalize_user_ids([1, 2, 3]), {1, 2, 3})

    def test_string_coerced_to_int(self):
        self.assertEqual(normalize_user_ids("42"), {42})

    def test_list_with_string_ints(self):
        self.assertEqual(normalize_user_ids(["1", "2"]), {1, 2})

    def test_skips_non_numeric(self):
        self.assertEqual(
            normalize_user_ids([1, "two", None, "3"]),
            {1, 3},
        )

    def test_dedupes(self):
        self.assertEqual(normalize_user_ids([1, 1, 2, 2]), {1, 2})


class TestIsMentionOnly(unittest.TestCase):
    def test_empty_filters_returns_false(self):
        self.assertFalse(is_mention_only("@bot", []))

    def test_empty_text_returns_false(self):
        self.assertFalse(is_mention_only("", ["@bot"]))

    def test_exact_match(self):
        self.assertTrue(is_mention_only("@bot", ["@bot"]))

    def test_with_trailing_whitespace(self):
        self.assertTrue(is_mention_only("@bot   ", ["@bot"]))

    def test_with_leading_whitespace(self):
        self.assertTrue(is_mention_only("   @bot", ["@bot"]))

    def test_with_real_content(self):
        self.assertFalse(is_mention_only("@bot hello", ["@bot"]))

    def test_multiple_filters_all_match(self):
        self.assertTrue(is_mention_only("@a @b", ["@a", "@b"]))

    def test_multiple_filters_partial_match(self):
        # "@a" filter matches but "@b" remains as content
        self.assertFalse(is_mention_only("@a @b", ["@a"]))

    def test_discord_role_mention_brackets_stripped(self):
        # Filter "@&111222333444555666" should treat "<@&111222333444555666>"
        # as mention-only even though `replace` leaves the brackets "<>".
        self.assertTrue(is_mention_only("<@&111222333444555666>", ["@&111222333444555666"]))

    def test_discord_user_mention_brackets_stripped(self):
        self.assertTrue(is_mention_only("<@123>", ["@123"]))

    def test_brackets_with_real_text(self):
        self.assertFalse(is_mention_only("<@123> hi", ["@123"]))

    def test_mention_with_punctuation_only(self):
        # "<@bot>!!!" — exclamations count as mention residue, drop the message.
        self.assertTrue(is_mention_only("<@bot>!!!", ["@bot"]))

    def test_real_text_not_dropped_by_residue_strip(self):
        # Make sure "rock & roll" doesn't accidentally count as residue.
        self.assertFalse(is_mention_only("rock & roll @bot", ["@bot"]))


class TestBridgeDirectionResolvers(unittest.TestCase):
    def test_bidirectional_sources_equals_targets(self):
        bridge = {"name": "b", "platforms": {"telegram": -100, "discord": 200}}
        self.assertEqual(bridge_sources(bridge), {"telegram": [-100], "discord": [200]})
        self.assertEqual(bridge_targets(bridge), {"telegram": [-100], "discord": [200]})

    def test_directional_sources_and_targets_differ(self):
        bridge = {
            "name": "b",
            "from": {"discord": 100},
            "to": {"telegram": -200},
        }
        self.assertEqual(bridge_sources(bridge), {"discord": [100]})
        self.assertEqual(bridge_targets(bridge), {"telegram": [-200]})

    def test_directional_only_to_no_from(self):
        # Edge case: only "to" key. Sources empty, targets present.
        bridge = {"name": "b", "to": {"telegram": -200}}
        self.assertEqual(bridge_sources(bridge), {})
        self.assertEqual(bridge_targets(bridge), {"telegram": [-200]})

    def test_multi_target_list(self):
        bridge = {
            "name": "fan",
            "from": {"discord": 100},
            "to": {"telegram": [-200, -300], "discord": [400]},
        }
        self.assertEqual(
            bridge_targets(bridge),
            {"telegram": [-200, -300], "discord": [400]},
        )

    def test_multi_source_list(self):
        bridge = {
            "name": "agg",
            "from": {"discord": [100, 200]},
            "to": {"telegram": -300},
        }
        self.assertEqual(bridge_sources(bridge), {"discord": [100, 200]})

    def test_scalar_is_normalized_to_singleton_list(self):
        bridge = {"name": "b", "platforms": {"telegram": -100}}
        self.assertEqual(bridge_sources(bridge), {"telegram": [-100]})

    def test_all_platforms_bidirectional(self):
        bridge = {"name": "b", "platforms": {"telegram": -100, "discord": 200}}
        self.assertEqual(
            bridge_all_platforms(bridge),
            {"telegram": [-100], "discord": [200]},
        )

    def test_all_platforms_directional_unions(self):
        bridge = {
            "name": "b",
            "from": {"discord": 100},
            "to": {"telegram": -200, "discord": 300},
        }
        # 'discord' appears in both from and to with different IDs.
        # bridge_all_platforms should union them.
        result = bridge_all_platforms(bridge)
        self.assertEqual(set(result.keys()), {"discord", "telegram"})
        self.assertEqual(set(result["discord"]), {100, 300})
        self.assertEqual(result["telegram"], [-200])

    def test_all_platforms_dedupes_overlap(self):
        # Same chat id in from and to — should appear once.
        bridge = {
            "name": "b",
            "from": {"discord": 100},
            "to": {"discord": 100},
        }
        self.assertEqual(bridge_all_platforms(bridge), {"discord": [100]})

    def test_is_directional_bridge_true_for_from(self):
        self.assertTrue(is_directional_bridge({"from": {"discord": 1}}))

    def test_is_directional_bridge_true_for_to(self):
        self.assertTrue(is_directional_bridge({"to": {"discord": 1}}))

    def test_is_directional_bridge_false_for_platforms(self):
        self.assertFalse(is_directional_bridge({"platforms": {"discord": 1}}))


class TestGetEnabledPlatforms(unittest.TestCase):
    def test_collects_from_platforms_key(self):
        settings = {"bridges": [{"platforms": {"telegram": 1, "discord": 2}}]}
        self.assertEqual(get_enabled_platforms(settings), ["discord", "telegram"])

    def test_collects_from_directional_keys(self):
        settings = {"bridges": [
            {"from": {"discord": 1}, "to": {"telegram": 2}},
        ]}
        self.assertEqual(get_enabled_platforms(settings), ["discord", "telegram"])

    def test_unions_across_bridges(self):
        settings = {"bridges": [
            {"platforms": {"telegram": 1}},
            {"from": {"discord": 2}, "to": {"whatsapp": "3@g.us"}},
        ]}
        self.assertEqual(
            get_enabled_platforms(settings),
            ["discord", "telegram", "whatsapp"],
        )

    def test_no_bridges_returns_empty(self):
        self.assertEqual(get_enabled_platforms({}), [])


class TestBridgeDigestConfig(unittest.TestCase):
    def test_no_digest_key_returns_none(self):
        self.assertIsNone(bridge_digest_config({"name": "b"}))

    def test_disabled_returns_none(self):
        self.assertIsNone(bridge_digest_config({"digest": {"enabled": False}}))

    def test_enabled_default_wait(self):
        self.assertEqual(
            bridge_digest_config({"digest": {"enabled": True}}),
            {"wait_seconds": 300.0, "max_wait_seconds": 0.0, "buffer_media": False},
        )

    def test_explicit_wait(self):
        cfg = bridge_digest_config({"digest": {"enabled": True, "wait_seconds": 60}})
        self.assertEqual(cfg["wait_seconds"], 60.0)

    def test_string_wait_coerced(self):
        cfg = bridge_digest_config({"digest": {"enabled": True, "wait_seconds": "120"}})
        self.assertEqual(cfg["wait_seconds"], 120.0)

    def test_invalid_wait_falls_back_to_default(self):
        cfg = bridge_digest_config({"digest": {"enabled": True, "wait_seconds": "abc"}})
        self.assertEqual(cfg["wait_seconds"], 300.0)

    def test_zero_wait_clamped(self):
        cfg = bridge_digest_config({"digest": {"enabled": True, "wait_seconds": 0}})
        self.assertGreaterEqual(cfg["wait_seconds"], 0.1)

    def test_buffer_media_default_false(self):
        cfg = bridge_digest_config({"digest": {"enabled": True}})
        self.assertFalse(cfg["buffer_media"])

    def test_buffer_media_explicit_true(self):
        cfg = bridge_digest_config({"digest": {"enabled": True, "buffer_media": True}})
        self.assertTrue(cfg["buffer_media"])

    def test_max_wait_default_disabled(self):
        cfg = bridge_digest_config({"digest": {"enabled": True}})
        self.assertEqual(cfg["max_wait_seconds"], 0.0)

    def test_max_wait_explicit(self):
        cfg = bridge_digest_config({"digest": {"enabled": True, "max_wait_seconds": 600}})
        self.assertEqual(cfg["max_wait_seconds"], 600.0)

    def test_max_wait_string_coerced(self):
        cfg = bridge_digest_config({"digest": {"enabled": True, "max_wait_seconds": "120"}})
        self.assertEqual(cfg["max_wait_seconds"], 120.0)

    def test_max_wait_invalid_falls_back_to_zero(self):
        cfg = bridge_digest_config({"digest": {"enabled": True, "max_wait_seconds": "abc"}})
        self.assertEqual(cfg["max_wait_seconds"], 0.0)

    def test_max_wait_negative_clamped(self):
        cfg = bridge_digest_config({"digest": {"enabled": True, "max_wait_seconds": -10}})
        self.assertEqual(cfg["max_wait_seconds"], 0.0)


class TestParseUploadLimitMb(unittest.TestCase):
    MB = 1024 * 1024

    def test_none_uses_default(self):
        self.assertEqual(parse_upload_limit_mb(None, 10), 10 * self.MB)

    def test_int_value(self):
        self.assertEqual(parse_upload_limit_mb(25, 10), 25 * self.MB)

    def test_float_value(self):
        self.assertEqual(parse_upload_limit_mb(7.5, 10), int(7.5 * self.MB))

    def test_string_coerced(self):
        self.assertEqual(parse_upload_limit_mb("50", 10), 50 * self.MB)

    def test_invalid_uses_default(self):
        self.assertEqual(parse_upload_limit_mb("abc", 10), 10 * self.MB)

    def test_floor_of_one_mb(self):
        self.assertEqual(parse_upload_limit_mb(0, 10), self.MB)
        self.assertEqual(parse_upload_limit_mb(0.5, 10), self.MB)


class TestEffectiveMentionFilters(unittest.TestCase):
    def test_no_bridge_override_uses_platform_default(self):
        self.assertEqual(
            effective_mention_filters({"name": "b"}, ["@plat"]),
            ["@plat"],
        )

    def test_bridge_string_overrides_platform(self):
        self.assertEqual(
            effective_mention_filters({"mention_filter": "@bridge"}, ["@plat"]),
            ["@bridge"],
        )

    def test_bridge_list_overrides_platform(self):
        self.assertEqual(
            effective_mention_filters({"mention_filter": ["@a", "@b"]}, ["@plat"]),
            ["@a", "@b"],
        )

    def test_bridge_empty_list_clears_platform(self):
        # Explicitly setting an empty list on the bridge clears the default
        self.assertEqual(
            effective_mention_filters({"mention_filter": []}, ["@plat"]),
            [],
        )

    def test_bridge_value_lowercased(self):
        self.assertEqual(
            effective_mention_filters({"mention_filter": "@MyBot"}, []),
            ["@mybot"],
        )


class TestEffectiveAlwaysForwardUserIds(unittest.TestCase):
    def test_no_bridge_override_uses_platform_default(self):
        self.assertEqual(
            effective_always_forward_user_ids({"name": "b"}, {1, 2}),
            {1, 2},
        )

    def test_bridge_int_overrides_platform(self):
        self.assertEqual(
            effective_always_forward_user_ids({"always_forward_user_ids": 99}, {1, 2}),
            {99},
        )

    def test_bridge_list_overrides_platform(self):
        self.assertEqual(
            effective_always_forward_user_ids({"always_forward_user_ids": [10, 20]}, {1, 2}),
            {10, 20},
        )

    def test_bridge_empty_list_clears_platform(self):
        self.assertEqual(
            effective_always_forward_user_ids({"always_forward_user_ids": []}, {1, 2}),
            set(),
        )


class TestEffectiveDestinationSizeLimit(unittest.TestCase):
    MB = 1024 * 1024

    def test_no_bridge_override_uses_platform_default(self):
        self.assertEqual(
            effective_destination_size_limit_bytes({"name": "b"}, 10 * self.MB),
            10 * self.MB,
        )

    def test_bridge_int_override(self):
        self.assertEqual(
            effective_destination_size_limit_bytes({"destination_size_limit_mb": 50}, 10 * self.MB),
            50 * self.MB,
        )

    def test_bridge_float_override(self):
        self.assertEqual(
            effective_destination_size_limit_bytes({"destination_size_limit_mb": 7.5}, 10 * self.MB),
            int(7.5 * self.MB),
        )

    def test_bridge_string_value_coerced(self):
        self.assertEqual(
            effective_destination_size_limit_bytes({"destination_size_limit_mb": "100"}, 10 * self.MB),
            100 * self.MB,
        )

    def test_bridge_invalid_falls_back_to_platform_default(self):
        # Bad value falls back to the platform default (in MB) parsed back to bytes.
        result = effective_destination_size_limit_bytes(
            {"destination_size_limit_mb": "abc"}, 25 * self.MB,
        )
        self.assertEqual(result, 25 * self.MB)


if __name__ == "__main__":
    unittest.main()
