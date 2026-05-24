"""
Unit tests for :class:`bridge.router.Router`.

The router has only two database touchpoints (``resolve_native_id`` and
``save_message_mapping``) — these are stubbed with ``AsyncMock`` so the
tests run with no real DB and no need for ``aiosqlite`` at test time.

Verifies the rules that matter:

* Bidirectional bridges skip same-platform echo.
* Directional bridges allow same-platform fan-out (user is explicit).
* Multi-target list expands to one send per chat ID.
* ``forward_reactions=False`` suppresses reaction notifications entirely.
"""

import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

# Stub ``aiosqlite`` before ``bridge.database`` gets imported transitively —
# the real database module isn't exercised in these tests, but importing it
# would otherwise pull in aiosqlite which isn't a dev dependency.
sys.modules.setdefault("aiosqlite", MagicMock())

from bridge.router import Router  # noqa: E402  (import after sys.modules patch)


class FakePlatform:
    """In-memory stand-in for a real platform — records every call.

    Implements the methods the router actually invokes (send_text,
    send_file, escape_user_text, format_digest_body) without inheriting
    from BasePlatform — keeps the test self-contained and decoupled."""

    def __init__(self, name: str):
        self.name = name
        self.sent_text: list[tuple] = []   # (chat_id, content, sender, reply_to)
        self.sent_files: list[tuple] = []  # (chat_id, file_path, sender, reply_to)
        self._next_id = 1

    async def send_text(self, chat_id, content, sender, reply_to_native_id=None):
        self.sent_text.append((chat_id, content, sender, reply_to_native_id))
        msg_id = self._next_id
        self._next_id += 1
        return msg_id

    async def send_file(self, chat_id, file_path, file_ext, sender, reply_to_native_id=None):
        self.sent_files.append((chat_id, file_path, sender, reply_to_native_id))
        msg_id = self._next_id
        self._next_id += 1
        return msg_id

    def escape_user_text(self, s: str) -> str:
        # Identity for tests; matches BasePlatform default.
        return s

    def format_digest_body(self, entries) -> str:
        # Mirror BasePlatform's default plain-text format with consecutive
        # same-sender grouping so tests assert on a stable shape.
        import time
        groups: list[list] = []
        for ts, sender, content in entries:
            if not content or not content.strip(): continue
            lines = content.splitlines() or [content]
            if groups and groups[-1][0] == sender:
                groups[-1][2].extend(lines)
            else:
                groups.append([sender, ts, list(lines)])
        out: list[str] = []
        for sender, ts, lines in groups:
            ts_str = time.strftime("%H:%M", time.localtime(ts))
            out.append(f"{sender} · {ts_str}")
            for line in lines:
                out.append(f"  {line}")
            out.append("")
        return "\n".join(out).rstrip()


class TestRouterDispatch(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tg = FakePlatform("telegram")
        self.dc = FakePlatform("discord")
        self.platforms = {"telegram": self.tg, "discord": self.dc}

        # Stub out the two database functions the router actually calls.
        from bridge import router as router_mod
        self._real_db = router_mod.database
        router_mod.database = MagicMock()
        router_mod.database.resolve_native_id = AsyncMock(return_value=None)
        router_mod.database.save_message_mapping = AsyncMock(return_value=None)

    async def asyncTearDown(self):
        from bridge import router as router_mod
        router_mod.database = self._real_db

    # --- bidirectional ----------------------------------------------------

    async def test_bidirectional_skips_source_platform(self):
        """A Telegram message must not be echoed back to a Telegram chat in
        the same bidirectional bridge — only routed to the other platform."""
        bridges = [{"name": "b", "platforms": {"telegram": -100, "discord": 200}}]
        router = Router(self.platforms, bridges, db_path=":memory:")
        await router.on_message("telegram", "b", 1, "hi", "Alice")
        self.assertEqual(len(self.dc.sent_text), 1)
        self.assertEqual(self.dc.sent_text[0][0], 200)
        self.assertEqual(self.tg.sent_text, [])

    # --- directional ------------------------------------------------------

    async def test_directional_only_targets_in_to(self):
        """A Discord-source bridge with Telegram in `to` only — Discord
        message goes to Telegram; nothing echoes to Discord."""
        bridges = [{
            "name": "b",
            "from": {"discord": 200},
            "to": {"telegram": -100},
        }]
        router = Router(self.platforms, bridges, db_path=":memory:")
        await router.on_message("discord", "b", 1, "hi", "Alice")
        self.assertEqual(len(self.tg.sent_text), 1)
        self.assertEqual(self.tg.sent_text[0][0], -100)
        self.assertEqual(self.dc.sent_text, [])

    async def test_directional_allows_same_platform_fanout(self):
        """`from: discord:100, to: discord:[200, 300]` — user is explicit
        about same-platform fan-out. Both Discord targets receive."""
        bridges = [{
            "name": "b",
            "from": {"discord": 100},
            "to": {"discord": [200, 300]},
        }]
        router = Router(self.platforms, bridges, db_path=":memory:")
        await router.on_message("discord", "b", 1, "hi", "Alice")
        self.assertEqual(len(self.dc.sent_text), 2)
        self.assertEqual({c[0] for c in self.dc.sent_text}, {200, 300})

    async def test_multi_target_list_expands(self):
        """A `to:` value as a list of chat IDs results in one send per ID."""
        bridges = [{
            "name": "b",
            "from": {"discord": 100},
            "to": {"telegram": [-200, -300, -400]},
        }]
        router = Router(self.platforms, bridges, db_path=":memory:")
        await router.on_message("discord", "b", 1, "hi", "Alice")
        self.assertEqual(len(self.tg.sent_text), 3)
        self.assertEqual({c[0] for c in self.tg.sent_text}, {-200, -300, -400})

    async def test_directional_does_not_route_target_side_inputs(self):
        """If a non-source platform somehow invokes routing, the only
        targets that get hit are those listed in ``to:``. Discord (not in
        ``to:``) receives nothing."""
        bridges = [{
            "name": "b",
            "from": {"discord": 200},
            "to": {"telegram": -100},
        }]
        router = Router(self.platforms, bridges, db_path=":memory:")
        await router.on_message("telegram", "b", 1, "hi", "Alice")
        self.assertEqual(self.dc.sent_text, [])

    # --- reactions --------------------------------------------------------

    async def test_forward_reactions_off_suppresses(self):
        bridges = [{"name": "b", "platforms": {"telegram": -100, "discord": 200}}]
        router = Router(self.platforms, bridges, db_path=":memory:", forward_reactions=False)
        await router.on_reaction("telegram", "b", 1, "👍", "Alice")
        self.assertEqual(self.dc.sent_text, [])
        self.assertEqual(self.tg.sent_text, [])

    async def test_forward_reactions_on_dispatches(self):
        bridges = [{"name": "b", "platforms": {"telegram": -100, "discord": 200}}]
        router = Router(self.platforms, bridges, db_path=":memory:", forward_reactions=True)
        await router.on_reaction("telegram", "b", 1, "👍", "Alice")
        self.assertEqual(len(self.dc.sent_text), 1)
        self.assertIn("Alice reacted with 👍", self.dc.sent_text[0][1])

    # --- digest mode ------------------------------------------------------

    async def test_digest_aggregates_messages_into_one_thread(self):
        """Three messages within the wait window flush as a single thread."""
        bridges = [{
            "name": "b",
            "platforms": {"telegram": -100, "discord": 200},
            "digest": {"enabled": True, "wait_seconds": 0.2},
        }]
        router = Router(self.platforms, bridges, db_path=":memory:")

        await router.on_message("telegram", "b", 1, "hello", "Alice")
        await router.on_message("telegram", "b", 2, "how are you", "Bob")
        await router.on_message("telegram", "b", 3, "I'm good thanks", "Alice")

        # Nothing dispatched yet (still buffering)
        self.assertEqual(self.dc.sent_text, [])

        # Wait past the window
        await asyncio.sleep(0.4)

        # One aggregated thread, not three
        self.assertEqual(len(self.dc.sent_text), 1)
        body = self.dc.sent_text[0][1]
        self.assertIn("Alice", body)
        self.assertIn("Bob", body)
        self.assertIn("hello", body)
        self.assertIn("how are you", body)
        self.assertIn("I'm good thanks", body)
        # FakePlatform uses the plain-text default. Real platforms format
        # natively (HTML for Telegram, Markdown for Discord) — that
        # behavior is covered indirectly by the platform-specific tests.
        self.assertTrue(body.startswith("Alice "), f"unexpected body: {body!r}")
        self.assertIn(" · ", body)
        self.assertEqual(body.count("Alice ·"), 2)
        self.assertEqual(body.count("Bob ·"), 1)

    async def test_digest_skips_empty_content(self):
        """Whitespace-only or empty messages don't get a block."""
        bridges = [{
            "name": "b",
            "platforms": {"telegram": -100, "discord": 200},
            "digest": {"enabled": True, "wait_seconds": 0.2},
        }]
        router = Router(self.platforms, bridges, db_path=":memory:")
        await router.on_message("telegram", "b", 1, "real msg", "Alice")
        await router.on_message("telegram", "b", 2, "   ", "Bob")
        await router.on_message("telegram", "b", 3, "", "Carol")
        await asyncio.sleep(0.4)
        self.assertEqual(len(self.dc.sent_text), 1)
        body = self.dc.sent_text[0][1]
        self.assertIn("Alice", body)
        self.assertNotIn("Bob", body)
        self.assertNotIn("Carol", body)

    async def test_digest_includes_media_mention_for_files(self):
        """A file dispatched to a digest bridge ships immediately AND adds
        a placeholder line to the digest thread."""
        bridges = [{
            "name": "b",
            "platforms": {"telegram": -100, "discord": 200},
            "digest": {"enabled": True, "wait_seconds": 0.2},
        }]
        router = Router(self.platforms, bridges, db_path=":memory:")

        # Create an actual temp file so on_file's os.remove() doesn't error
        import tempfile
        tf = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tf.write(b"fake-bytes")
        tf.close()
        try:
            await router.on_file("telegram", "b", 1, tf.name, ".jpg", "Alice")
            await router.on_message("telegram", "b", 2, "look at this", "Alice")
            await asyncio.sleep(0.4)
        finally:
            try: __import__("os").remove(tf.name)
            except OSError: pass

        # File was sent immediately
        self.assertEqual(len(self.dc.sent_files), 1)
        # Digest flushed once with both the media mention and the text
        self.assertEqual(len(self.dc.sent_text), 1)
        body = self.dc.sent_text[0][1]
        self.assertIn("look at this", body)
        self.assertIn("🖼", body)
        self.assertIn(".jpg]", body)

    async def test_digest_groups_consecutive_same_sender(self):
        """Three messages by Alice in a row should share ONE header,
        not produce three separate ``Alice · HH:MM`` lines."""
        bridges = [{
            "name": "b",
            "platforms": {"telegram": -100, "discord": 200},
            "digest": {"enabled": True, "wait_seconds": 0.2},
        }]
        router = Router(self.platforms, bridges, db_path=":memory:")

        await router.on_message("telegram", "b", 1, "first", "Alice")
        await router.on_message("telegram", "b", 2, "second", "Alice")
        await router.on_message("telegram", "b", 3, "third", "Alice")
        await asyncio.sleep(0.4)

        self.assertEqual(len(self.dc.sent_text), 1)
        body = self.dc.sent_text[0][1]
        # Only ONE Alice header even though Alice sent three messages.
        self.assertEqual(body.count("Alice ·"), 1, f"unexpected body:\n{body}")
        # All three message contents are still present.
        self.assertIn("first", body)
        self.assertIn("second", body)
        self.assertIn("third", body)

    async def test_digest_orders_entries_by_source_timestamp(self):
        """Source timestamps must determine digest order even if async
        handler concurrency causes the bridge to receive a later-typed
        message BEFORE an earlier-typed one (e.g. a slow media download
        finishes after a quick text reply)."""
        bridges = [{
            "name": "b",
            "platforms": {"telegram": -100, "discord": 200},
            "digest": {"enabled": True, "wait_seconds": 0.2},
        }]
        router = Router(self.platforms, bridges, db_path=":memory:")

        # Add in REVERSE order of source ts (simulates an out-of-order race).
        # Message A was sent at T=100, message B at T=200, but B arrives
        # at the buffer first.
        await router.on_message("telegram", "b", 1, "B-newer", "Alice", source_ts=200.0)
        await router.on_message("telegram", "b", 2, "A-older", "Alice", source_ts=100.0)
        await asyncio.sleep(0.4)

        body = self.dc.sent_text[0][1]
        # A-older must appear BEFORE B-newer in the rendered body.
        a_idx = body.index("A-older")
        b_idx = body.index("B-newer")
        self.assertLess(a_idx, b_idx,
                        f"out-of-order: A-older at {a_idx}, B-newer at {b_idx}\n{body}")

    async def test_digest_debounces_consecutive_messages(self):
        """Messages arriving faster than wait_seconds keep resetting the
        timer; the digest only flushes after the channel goes quiet."""
        bridges = [{
            "name": "b",
            "platforms": {"telegram": -100, "discord": 200},
            "digest": {"enabled": True, "wait_seconds": 0.3},
        }]
        router = Router(self.platforms, bridges, db_path=":memory:")

        # Send messages every 0.15s — well under the 0.3s debounce window.
        await router.on_message("telegram", "b", 1, "first", "Alice")
        await asyncio.sleep(0.15)
        await router.on_message("telegram", "b", 2, "second", "Alice")
        await asyncio.sleep(0.15)
        await router.on_message("telegram", "b", 3, "third", "Alice")

        # Cumulative elapsed > wait_seconds, but no 0.3s gap — nothing
        # should have flushed yet.
        self.assertEqual(self.dc.sent_text, [],
                         "debounce reset failed — flushed mid-burst")

        # Now go silent past wait_seconds → flush.
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.dc.sent_text), 1)
        body = self.dc.sent_text[0][1]
        self.assertIn("first", body)
        self.assertIn("second", body)
        self.assertIn("third", body)

    async def test_digest_max_wait_caps_endless_chatter(self):
        """A channel that never goes silent still gets a flush at
        max_wait_seconds from the FIRST message in the cycle."""
        bridges = [{
            "name": "b",
            "platforms": {"telegram": -100, "discord": 200},
            "digest": {"enabled": True, "wait_seconds": 0.4, "max_wait_seconds": 0.6},
        }]
        router = Router(self.platforms, bridges, db_path=":memory:")

        # Hammer messages at 0.1s intervals for 0.8s total. wait_seconds
        # debounce alone (0.4s) would never fire. max_wait_seconds (0.6s)
        # should force a flush by then.
        start = asyncio.get_event_loop().time()
        n = 0
        while asyncio.get_event_loop().time() - start < 0.7:
            n += 1
            await router.on_message("telegram", "b", n, f"msg {n}", "Alice")
            await asyncio.sleep(0.1)

        # By now we should have at least one digest flush from the cap.
        self.assertGreaterEqual(len(self.dc.sent_text), 1,
                                "max_wait_seconds did not fire")

    async def test_digest_does_not_group_when_sender_alternates(self):
        """A → B → A produces TWO ``Alice ·`` headers (not one); the
        groups are by adjacency, not by absolute count."""
        bridges = [{
            "name": "b",
            "platforms": {"telegram": -100, "discord": 200},
            "digest": {"enabled": True, "wait_seconds": 0.2},
        }]
        router = Router(self.platforms, bridges, db_path=":memory:")

        await router.on_message("telegram", "b", 1, "hi", "Alice")
        await router.on_message("telegram", "b", 2, "hi back", "Bob")
        await router.on_message("telegram", "b", 3, "ok bye", "Alice")
        await asyncio.sleep(0.4)

        body = self.dc.sent_text[0][1]
        self.assertEqual(body.count("Alice ·"), 2)
        self.assertEqual(body.count("Bob ·"), 1)

    async def test_digest_buffer_media_holds_file_until_flush(self):
        """With buffer_media=true, files are NOT shipped immediately —
        they go out together with the digest text at flush time."""
        bridges = [{
            "name": "b",
            "platforms": {"telegram": -100, "discord": 200},
            "digest": {"enabled": True, "wait_seconds": 0.2, "buffer_media": True},
        }]
        router = Router(self.platforms, bridges, db_path=":memory:")

        import tempfile
        tf = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tf.write(b"fake-bytes")
        tf.close()
        try:
            await router.on_file("telegram", "b", 1, tf.name, ".png", "Alice")
            await router.on_message("telegram", "b", 2, "see this", "Alice")

            # Pre-flush: nothing has been dispatched yet — file is held
            self.assertEqual(self.dc.sent_files, [])
            self.assertEqual(self.dc.sent_text, [])
            self.assertTrue(__import__("os").path.exists(tf.name),
                            "file must still exist before flush")

            await asyncio.sleep(0.4)

            # Post-flush: file shipped + digest text shipped
            self.assertEqual(len(self.dc.sent_files), 1)
            self.assertEqual(len(self.dc.sent_text), 1)
            body = self.dc.sent_text[0][1]
            self.assertIn("see this", body)
            self.assertIn("🖼", body)
            # File on disk has been cleaned up
            self.assertFalse(__import__("os").path.exists(tf.name))
        finally:
            try: __import__("os").remove(tf.name)
            except OSError: pass

    async def test_digest_multiline_content_preserves_lines(self):
        """Each line of a multiline message stays in the body."""
        bridges = [{
            "name": "b",
            "platforms": {"telegram": -100, "discord": 200},
            "digest": {"enabled": True, "wait_seconds": 0.2},
        }]
        router = Router(self.platforms, bridges, db_path=":memory:")
        await router.on_message("telegram", "b", 1, "line one\nline two\nline three", "Alice")
        await asyncio.sleep(0.4)
        body = self.dc.sent_text[0][1]
        self.assertIn("line one", body)
        self.assertIn("line two", body)
        self.assertIn("line three", body)

    async def test_digest_respects_directional_routing(self):
        """Digest flush honors the bridge's `from`/`to` direction."""
        bridges = [{
            "name": "b",
            "from": {"telegram": -100},
            "to": {"discord": 200},
            "digest": {"enabled": True, "wait_seconds": 0.2},
        }]
        router = Router(self.platforms, bridges, db_path=":memory:")
        await router.on_message("telegram", "b", 1, "hi", "Alice")
        await asyncio.sleep(0.4)
        self.assertEqual(len(self.dc.sent_text), 1)
        self.assertEqual(self.tg.sent_text, [])

    async def test_digest_disabled_falls_through_to_immediate_dispatch(self):
        """With no digest block, on_message dispatches right away."""
        bridges = [{"name": "b", "platforms": {"telegram": -100, "discord": 200}}]
        router = Router(self.platforms, bridges, db_path=":memory:")
        await router.on_message("telegram", "b", 1, "hi", "Alice")
        # Immediate — no need to sleep
        self.assertEqual(len(self.dc.sent_text), 1)
        self.assertEqual(self.dc.sent_text[0][1], "hi")


if __name__ == "__main__":
    unittest.main()
