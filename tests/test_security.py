"""
Security regression tests.

Path-traversal and filename-injection cases that we explicitly defend
against. If any of these fail, the bridge has regressed to a state where
a hostile user could write outside the media directory.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from bridge.utils.media import safe_basename, get_unique_filepath


class TestSafeBasename(unittest.TestCase):
    def test_strips_parent_traversal(self):
        # The classic one. Must NOT return anything containing "../".
        out = safe_basename("../../etc/passwd")
        self.assertNotIn("..", out)
        self.assertNotIn("/", out)

    def test_strips_absolute_path(self):
        out = safe_basename("/etc/shadow")
        self.assertNotIn("/", out)
        self.assertEqual(out, "shadow")

    def test_strips_windows_path(self):
        out = safe_basename(r"C:\Windows\System32\evil.dll")
        self.assertNotIn("\\", out)
        self.assertNotIn(":", out)

    def test_strips_null_byte(self):
        out = safe_basename("ok.txt\x00.exe")
        self.assertNotIn("\x00", out)

    def test_leading_dot_replaced(self):
        # ".bashrc" would be a hidden file overwrite vector
        out = safe_basename(".bashrc")
        self.assertFalse(out.startswith("."))

    def test_leading_dash_replaced(self):
        # "-rf" or "-i" could be misparsed as flags by ffmpeg / shell
        out = safe_basename("-rf")
        self.assertFalse(out.startswith("-"))

    def test_double_dot_only(self):
        out = safe_basename("..")
        self.assertNotIn("..", out)
        self.assertNotEqual(out, "")

    def test_empty_returns_fallback(self):
        self.assertEqual(safe_basename(""), "file")
        self.assertEqual(safe_basename("", fallback="custom"), "custom")

    def test_normal_filename_preserved(self):
        # A perfectly normal filename should round-trip unchanged
        self.assertEqual(safe_basename("vacation_photo.jpg"), "vacation_photo.jpg")

    def test_unicode_preserved(self):
        # Non-ASCII names are valid filenames, should be kept
        self.assertEqual(safe_basename("ёжик.png"), "ёжик.png")


class TestGetUniqueFilepathSecurity(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_traversal_input_stays_inside_directory(self):
        path = get_unique_filepath(self.dir, "../../etc/passwd", ".txt")
        # Must resolve to a child of the temp dir
        self.assertTrue(
            os.path.abspath(path).startswith(os.path.abspath(self.dir)),
            f"escape detected: {path}",
        )

    def test_absolute_input_stays_inside_directory(self):
        path = get_unique_filepath(self.dir, "/tmp/evil", ".sh")
        self.assertTrue(
            os.path.abspath(path).startswith(os.path.abspath(self.dir)),
            f"escape detected: {path}",
        )

    def test_normal_input_works(self):
        path = get_unique_filepath(self.dir, "image", ".png")
        self.assertEqual(os.path.basename(path), "image.png")
        self.assertTrue(os.path.abspath(path).startswith(os.path.abspath(self.dir)))

    def test_collision_appends_counter(self):
        # Create the first file so the second call has to disambiguate
        first = get_unique_filepath(self.dir, "image", ".png")
        with open(first, "w") as fh: fh.write("x")
        second = get_unique_filepath(self.dir, "image", ".png")
        self.assertNotEqual(first, second)
        self.assertTrue("(2)" in os.path.basename(second))

    def test_dangerous_extension_neutralized(self):
        # An extension like "../evil" must not escape either
        path = get_unique_filepath(self.dir, "image", "../evil")
        self.assertTrue(
            os.path.abspath(path).startswith(os.path.abspath(self.dir)),
            f"extension escape: {path}",
        )


class TestTelegramFormatDigestEscapes(unittest.TestCase):
    """Telegram outbound runs in HTML parse mode. User-supplied sender
    names and message content must be html-escaped before interpolation
    or a hostile user could inject Markdown link syntax / HTML tags into
    the destination chat (phishing vector)."""

    def setUp(self):
        # Stub aiosqlite so we can import the platform module without a
        # working DB / aiosqlite install in test env.
        import sys
        from unittest.mock import MagicMock
        sys.modules.setdefault("aiosqlite", MagicMock())
        sys.modules.setdefault("pyrogram", MagicMock())
        sys.modules.setdefault("pyrogram.enums", MagicMock())
        sys.modules.setdefault("pyrogram.errors", MagicMock())

    def test_html_tags_in_user_text_are_escaped(self):
        from bridge.platforms.telegram import TelegramPlatform
        p = TelegramPlatform.__new__(TelegramPlatform)
        body = p.format_digest_body([
            (1700000000.0, "<a href='evil'>Support</a>", "click <script>x</script>"),
        ])
        # Hostile HTML must be escaped — no live <a> or <script> tags
        # interpolated raw into the body.
        self.assertNotIn("<a href='evil'>", body)
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;a href=", body)
        self.assertIn("&lt;script&gt;", body)
        # Our own structural tags should still be present and well-formed
        self.assertIn("<b>", body)
        self.assertIn("</b>", body)
        self.assertIn("<blockquote>", body)
        self.assertIn("</blockquote>", body)

    def test_markdown_link_syntax_is_inert_in_html_mode(self):
        """Pyrogram in HTML parse mode does NOT interpret ``[text](url)``
        as a link — it's just plain text. So even though html.escape
        doesn't touch the brackets, the link injection vector is closed."""
        from bridge.platforms.telegram import TelegramPlatform
        p = TelegramPlatform.__new__(TelegramPlatform)
        body = p.format_digest_body([
            (1700000000.0, "[Support](https://phish)", "ok"),
        ])
        # Literal brackets remain (and that's fine — HTML doesn't parse them).
        self.assertIn("[Support](https://phish)", body)
        # No <a> tag was generated for the link injection attempt.
        self.assertNotIn("<a", body)
        self.assertNotIn("href=", body)

    def test_escape_user_text_html(self):
        from bridge.platforms.telegram import TelegramPlatform
        p = TelegramPlatform.__new__(TelegramPlatform)
        self.assertEqual(p.escape_user_text("<a>"), "&lt;a&gt;")
        self.assertEqual(p.escape_user_text("&"), "&amp;")
        # Markdown link brackets stay literal — but they're text, not parsed,
        # because the platform now sends with parse_mode=HTML.
        self.assertEqual(p.escape_user_text("[x](y)"), "[x](y)")


class TestDiscordHumanizeMentions(unittest.TestCase):
    """Discord stores mentions as raw markup like ``<@1234>`` — when
    forwarded to other platforms as plain text, the recipient sees the
    raw token. ``_humanize_mentions`` rewrites these to readable form.
    Not strictly a security issue but worth a regression test."""

    def setUp(self):
        import sys
        from unittest.mock import MagicMock
        sys.modules.setdefault("aiosqlite", MagicMock())
        sys.modules.setdefault("discord", MagicMock())
        sys.modules.setdefault("discord.ext", MagicMock())
        sys.modules.setdefault("discord.ext.commands", MagicMock())

    def _make_platform(self, users=None, channels=None):
        """Build a TelegramPlatform-free Discord platform stub with a
        controllable bot.get_user / bot.get_channel."""
        from bridge.platforms.discord import DiscordPlatform
        from unittest.mock import MagicMock
        p = DiscordPlatform.__new__(DiscordPlatform)
        bot = MagicMock()
        bot.get_user.side_effect = lambda uid: (users or {}).get(uid)
        bot.get_channel.side_effect = lambda cid: (channels or {}).get(cid)
        p._bot = bot
        return p

    def test_resolved_user_mention(self):
        from unittest.mock import MagicMock
        u = MagicMock()
        u.display_name = "alice"
        u.name = "alice_legacy"
        p = self._make_platform(users={123: u})
        self.assertEqual(p._humanize_mentions("hi <@123>"), "hi @alice")

    def test_unresolved_user_mention_falls_back_to_id(self):
        p = self._make_platform()
        self.assertEqual(p._humanize_mentions("ping <@99999>"), "ping @99999")

    def test_nickname_form(self):
        from unittest.mock import MagicMock
        u = MagicMock()
        u.display_name = "bob"
        u.name = "bob"
        p = self._make_platform(users={42: u})
        # <@!id> is the legacy nickname-mention form; should resolve same way.
        self.assertEqual(p._humanize_mentions("<@!42>!"), "@bob!")

    def test_channel_mention(self):
        from unittest.mock import MagicMock
        ch = MagicMock()
        ch.name = "general"
        p = self._make_platform(channels={555: ch})
        self.assertEqual(p._humanize_mentions("see <#555>"), "see #general")

    def test_role_mention(self):
        p = self._make_platform()
        self.assertEqual(p._humanize_mentions("<@&777>"), "@role:777")

    def test_custom_emoji(self):
        p = self._make_platform()
        self.assertEqual(p._humanize_mentions("hello <:wave:12345>"), "hello :wave:")
        self.assertEqual(p._humanize_mentions("<a:dance:67890>"), ":dance:")

    def test_mixed(self):
        from unittest.mock import MagicMock
        u = MagicMock()
        u.display_name = "alice"
        u.name = "alice"
        p = self._make_platform(users={1: u})
        out = p._humanize_mentions("hey <@1> see <#0> :tag <:hi:9>")
        self.assertEqual(out, "hey @alice see #0 :tag :hi:")

    def test_empty_input(self):
        p = self._make_platform()
        self.assertEqual(p._humanize_mentions(""), "")
        self.assertEqual(p._humanize_mentions(None), None)


class TestLogInjectionSanitizer(unittest.TestCase):
    """Hostile sender display names can contain newlines and ANSI escapes;
    if logged verbatim, they forge log lines or hide tracks under
    ``docker logs``. ``safe_for_log`` strips control bytes."""

    def test_newline_replaced(self):
        from bridge.utils.logger import safe_for_log
        s = "Alice\n[2026-01-01] [INFO] FAKE"
        out = safe_for_log(s)
        self.assertNotIn("\n", out)
        self.assertIn("\\x0a", out)

    def test_carriage_return_replaced(self):
        from bridge.utils.logger import safe_for_log
        out = safe_for_log("Bob\rfake")
        self.assertNotIn("\r", out)
        self.assertIn("\\x0d", out)

    def test_ansi_escape_replaced(self):
        from bridge.utils.logger import safe_for_log
        # \x1b[2J\x1b[H = clear screen + cursor home — would hide all
        # preceding output if printed to a terminal.
        out = safe_for_log("\x1b[2J\x1b[H")
        self.assertNotIn("\x1b", out)
        self.assertIn("\\x1b", out)

    def test_tab_preserved(self):
        # TAB is harmless and useful; no need to escape.
        from bridge.utils.logger import safe_for_log
        self.assertEqual(safe_for_log("a\tb"), "a\tb")

    def test_unicode_preserved(self):
        from bridge.utils.logger import safe_for_log
        self.assertEqual(safe_for_log("日本語ёжик"), "日本語ёжик")

    def test_truncation(self):
        from bridge.utils.logger import safe_for_log
        out = safe_for_log("x" * 500, max_len=10)
        self.assertEqual(out, "xxxxxxxxxx…")

    def test_none_returns_empty(self):
        from bridge.utils.logger import safe_for_log
        self.assertEqual(safe_for_log(None), "")

    def test_non_string_coerced(self):
        from bridge.utils.logger import safe_for_log
        self.assertEqual(safe_for_log(42), "42")


class TestPostDownloadSizeCap(unittest.IsolatedAsyncioTestCase):
    """Verify the bridge re-checks file size after download — the
    pre-download cap relies on metadata that a malicious or buggy client
    could in principle understate."""

    async def test_database_init_chmods_to_owner_only(self):
        # POSIX: bridge.db should be mode 0o600 after init.
        if os.name != "posix":
            self.skipTest("POSIX-only check")
        import sys, tempfile
        from unittest.mock import MagicMock, AsyncMock

        # Stub aiosqlite so init_db doesn't need a real driver.
        fake_aio = MagicMock()
        fake_db = MagicMock()
        fake_db.execute = AsyncMock()
        fake_db.commit = AsyncMock()
        fake_aio.connect.return_value.__aenter__ = AsyncMock(return_value=fake_db)
        fake_aio.connect.return_value.__aexit__ = AsyncMock(return_value=False)
        sys.modules["aiosqlite"] = fake_aio

        from bridge import database

        # Need a real file on disk for chmod to apply to.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bridge.db")
            with open(path, "wb") as fh: fh.write(b"")
            # Loosen mode first to verify init_db tightens it.
            os.chmod(path, 0o644)
            await database.init_db(path)
            mode = os.stat(path).st_mode & 0o777
            self.assertEqual(mode, 0o600,
                             f"bridge.db should be 0o600 after init_db, got 0o{mode:o}")


class TestStartupErrorHandling(unittest.IsolatedAsyncioTestCase):
    """Bridge should fail gracefully (clear log + non-zero exit) on the
    common operator misconfigurations rather than dumping a traceback."""

    async def test_missing_settings_yaml_returns_nonzero(self):
        import sys, tempfile
        from unittest.mock import MagicMock
        # Stub the parts of main.py that pull in heavy deps.
        sys.modules.setdefault("aiosqlite", MagicMock())
        sys.modules.setdefault("pyrogram", MagicMock())
        sys.modules.setdefault("pyrogram.enums", MagicMock())
        sys.modules.setdefault("discord", MagicMock())
        sys.modules.setdefault("discord.ext", MagicMock())
        sys.modules.setdefault("discord.ext.commands", MagicMock())
        sys.modules.setdefault("neonize", MagicMock())
        sys.modules.setdefault("neonize.aioze", MagicMock())
        sys.modules.setdefault("neonize.aioze.client", MagicMock())
        sys.modules.setdefault("neonize.aioze.events", MagicMock())
        sys.modules.setdefault("neonize.utils", MagicMock())
        sys.modules.setdefault("neonize.proto", MagicMock())
        sys.modules.setdefault("neonize.proto.waE2E", MagicMock())
        sys.modules.setdefault("neonize.proto.waE2E.WAWebProtobufsE2E_pb2", MagicMock())

        # Point SETTINGS_PATH at a dir that exists but no settings.yaml.
        with tempfile.TemporaryDirectory() as d:
            from unittest.mock import patch
            import main as bridger_main
            missing = os.path.join(d, "no-such-settings.yaml")
            with patch.object(bridger_main, "SETTINGS_PATH", missing):
                rc = await bridger_main.main()
            # Non-zero — operator needs to fix something.
            self.assertEqual(rc, 2)


class TestSettingsPathResolution(unittest.TestCase):
    """Settings-path resolution must self-heal env/mount drift: if
    BRIDGER_SETTINGS_PATH points at a non-existent file but settings.yaml
    exists at a standard location, use the existing one."""

    def setUp(self):
        import sys
        from unittest.mock import MagicMock
        for m in ["aiosqlite", "pyrogram", "pyrogram.enums",
                  "discord", "discord.ext", "discord.ext.commands",
                  "neonize", "neonize.aioze", "neonize.aioze.client",
                  "neonize.aioze.events", "neonize.utils",
                  "neonize.proto", "neonize.proto.waE2E",
                  "neonize.proto.waE2E.WAWebProtobufsE2E_pb2"]:
            sys.modules.setdefault(m, MagicMock())

    def test_env_path_used_when_it_exists(self):
        import tempfile, os as _os
        import main as bridger_main
        with tempfile.TemporaryDirectory() as d:
            p = _os.path.join(d, "settings.yaml")
            open(p, "w").close()
            with patch.dict(_os.environ, {"BRIDGER_SETTINGS_PATH": p}):
                self.assertEqual(bridger_main._resolve_settings_path(), p)

    def test_falls_back_when_env_path_missing(self):
        import tempfile, os as _os
        import main as bridger_main
        with tempfile.TemporaryDirectory() as d:
            # env points at a path that does NOT exist
            missing = _os.path.join(d, "nope", "settings.yaml")
            fallback = _os.path.join(d, "settings.yaml")
            open(fallback, "w").close()
            # Run from inside d so the bare "settings.yaml" candidate resolves.
            cwd = _os.getcwd()
            try:
                _os.chdir(d)
                with patch.dict(_os.environ, {"BRIDGER_SETTINGS_PATH": missing}):
                    self.assertEqual(bridger_main._resolve_settings_path(), "settings.yaml")
            finally:
                _os.chdir(cwd)

    def test_returns_preferred_when_nothing_exists(self):
        import os as _os
        import main as bridger_main
        with patch.dict(_os.environ, {"BRIDGER_SETTINGS_PATH": "/definitely/not/here.yaml"}):
            # Nothing exists → returns the env path so the error names it.
            self.assertEqual(bridger_main._resolve_settings_path(), "/definitely/not/here.yaml")


class TestLoggingResilience(unittest.TestCase):
    """A non-writable log directory (e.g. a Docker bind mount the
    container user can't write) must NOT crash startup — file logging
    degrades to console-only."""

    def test_file_handler_failure_does_not_raise(self):
        import importlib
        from unittest.mock import patch
        import bridge.utils.logger as logmod
        # Force a fresh, unconfigured logger module state.
        importlib.reload(logmod)
        with patch.object(logmod, "RotatingFileHandler",
                          side_effect=OSError("[Errno 13] Permission denied")):
            # Must not raise despite the file handler blowing up.
            logmod.setup_logging(log_dir="/nonexistent/should/not/matter")
        root = __import__("logging").getLogger("bridge")
        # Console handler still present → logging works.
        self.assertTrue(any(h.__class__.__name__ == "StreamHandler" for h in root.handlers))
        # Reset so other tests / real runs reconfigure cleanly.
        importlib.reload(logmod)


class TestTimezoneApply(unittest.TestCase):
    """`_apply_timezone` should update os.environ['TZ'] when the
    settings key is present, and be a no-op when missing."""

    def setUp(self):
        import sys
        from unittest.mock import MagicMock
        for m in ["aiosqlite", "pyrogram", "pyrogram.enums",
                  "discord", "discord.ext", "discord.ext.commands",
                  "neonize", "neonize.aioze", "neonize.aioze.client",
                  "neonize.aioze.events", "neonize.utils",
                  "neonize.proto", "neonize.proto.waE2E",
                  "neonize.proto.waE2E.WAWebProtobufsE2E_pb2"]:
            sys.modules.setdefault(m, MagicMock())
        # Snapshot TZ so we can restore.
        self._orig_tz = os.environ.get("TZ")

    def tearDown(self):
        if self._orig_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._orig_tz

    def test_no_timezone_key_leaves_env_alone(self):
        import main as bridger_main
        before = os.environ.get("TZ")
        bridger_main._apply_timezone({})
        self.assertEqual(os.environ.get("TZ"), before)

    def test_empty_timezone_value_leaves_env_alone(self):
        import main as bridger_main
        before = os.environ.get("TZ")
        bridger_main._apply_timezone({"timezone": "   "})
        self.assertEqual(os.environ.get("TZ"), before)

    def test_timezone_value_sets_env(self):
        import main as bridger_main
        bridger_main._apply_timezone({"timezone": "Europe/Berlin"})
        self.assertEqual(os.environ.get("TZ"), "Europe/Berlin")


class TestForwardSenderAttribution(unittest.TestCase):
    """When a message is forwarded, the bridge should attribute it to
    the ORIGINAL author, not the forwarder."""

    def setUp(self):
        import sys
        from unittest.mock import MagicMock
        sys.modules.setdefault("aiosqlite", MagicMock())
        sys.modules.setdefault("pyrogram", MagicMock())
        sys.modules.setdefault("pyrogram.enums", MagicMock())
        sys.modules.setdefault("discord", MagicMock())
        sys.modules.setdefault("discord.ext", MagicMock())
        sys.modules.setdefault("discord.ext.commands", MagicMock())

    # --- Telegram ---

    def test_telegram_forward_from_user_wins(self):
        from unittest.mock import MagicMock
        from bridge.platforms.telegram import _get_sender_name
        forwarder = MagicMock()
        forwarder.first_name = "Bob"
        forwarder.last_name = ""
        forwarder.username = "bob"
        original = MagicMock()
        original.first_name = "Alice"
        original.last_name = ""
        original.username = "alice"
        msg = MagicMock()
        msg.from_user = forwarder
        msg.forward_from = original
        msg.forward_from_chat = None
        msg.forward_sender_name = None
        self.assertEqual(_get_sender_name(msg), "Alice")

    def test_telegram_forward_from_chat_uses_title(self):
        from unittest.mock import MagicMock
        from bridge.platforms.telegram import _get_sender_name
        chat = MagicMock()
        chat.title = "Acme News"
        msg = MagicMock()
        msg.from_user = MagicMock(first_name="Bob", last_name="", username="bob")
        msg.forward_from = None
        msg.forward_from_chat = chat
        msg.forward_sender_name = None
        self.assertEqual(_get_sender_name(msg), "Acme News")

    def test_telegram_forward_sender_name_when_hidden(self):
        from unittest.mock import MagicMock
        from bridge.platforms.telegram import _get_sender_name
        msg = MagicMock()
        msg.from_user = MagicMock(first_name="Bob", last_name="", username="bob")
        msg.forward_from = None
        msg.forward_from_chat = None
        msg.forward_sender_name = "Anonymous Tipster"
        self.assertEqual(_get_sender_name(msg), "Anonymous Tipster")

    def test_telegram_normal_message_uses_from_user(self):
        from unittest.mock import MagicMock
        from bridge.platforms.telegram import _get_sender_name
        msg = MagicMock()
        msg.from_user = MagicMock(first_name="Carol", last_name="Doe", username="carol")
        msg.forward_from = None
        msg.forward_from_chat = None
        msg.forward_sender_name = None
        self.assertEqual(_get_sender_name(msg), "Carol Doe")

    # --- Discord ---

    def test_discord_snapshot_author_wins(self):
        from unittest.mock import MagicMock
        from bridge.platforms.discord import DiscordPlatform
        snap_author = MagicMock()
        snap_author.display_name = "Alice"
        snap_author.global_name = "alice_global"
        snap_author.name = "alice_legacy"
        snap = MagicMock()
        snap.author = snap_author
        msg = MagicMock()
        msg.message_snapshots = [snap]
        msg.author = MagicMock(display_name="Bob")  # forwarder
        self.assertEqual(DiscordPlatform._resolve_sender_name(msg), "Alice")

    def test_discord_snapshot_no_author_falls_back_to_forwarder(self):
        from unittest.mock import MagicMock
        from bridge.platforms.discord import DiscordPlatform
        snap = MagicMock(spec=[])  # no `author` attribute at all
        msg = MagicMock()
        msg.message_snapshots = [snap]
        msg.author = MagicMock(display_name="Bob")
        self.assertEqual(DiscordPlatform._resolve_sender_name(msg), "Bob")

    def test_discord_regular_message_uses_author(self):
        from unittest.mock import MagicMock
        from bridge.platforms.discord import DiscordPlatform
        msg = MagicMock()
        msg.message_snapshots = []
        msg.author = MagicMock(display_name="Carol")
        self.assertEqual(DiscordPlatform._resolve_sender_name(msg), "Carol")


if __name__ == "__main__":
    unittest.main()
