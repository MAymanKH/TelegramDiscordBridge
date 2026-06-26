"""
Unit tests for :class:`bridge.utils.janitor.MediaJanitor`.

The janitor sweeps orphaned media files. We exercise the one-shot
``sweep_once`` against a temp directory layout that mirrors how the
bridge stores files in production: ``messages/<platform>/`` plus
``/tmp/bridge_xcode_*`` transcoder leftovers.
"""

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from bridge.utils.janitor import MediaJanitor


class TestMediaJanitor(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # Build a fake `messages/` layout in a temp directory and patch
        # `platform_dir` to point inside it.
        self.tmp = tempfile.TemporaryDirectory()
        self.messages_root = self.tmp.name
        self.platform_names = ["telegram", "discord"]
        for name in self.platform_names:
            os.makedirs(os.path.join(self.messages_root, name))

        self._patcher = patch(
            "bridge.utils.janitor.platform_dir",
            side_effect=lambda name: os.path.join(self.messages_root, name),
        )
        self._patcher.start()

        # Use a separate temp dir for "tmp" so we don't sweep real /tmp
        self.fake_tmp = tempfile.TemporaryDirectory()
        self._tempdir_patcher = patch(
            "bridge.utils.janitor.tempfile.gettempdir",
            return_value=self.fake_tmp.name,
        )
        self._tempdir_patcher.start()

        # Patch the transcode directory the janitor reads from — same idea:
        # don't let the test sweep the real messages/transcoded/ dir.
        self.fake_transcode_dir = tempfile.TemporaryDirectory()
        self._transcode_patcher = patch(
            "bridge.utils.transcode.TRANSCODE_DIR",
            self.fake_transcode_dir.name,
        )
        self._transcode_patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()
        self._tempdir_patcher.stop()
        self._transcode_patcher.stop()
        self.tmp.cleanup()
        self.fake_tmp.cleanup()
        self.fake_transcode_dir.cleanup()

    # Helpers
    def _put(self, dir_path: str, name: str, age_seconds: float, content: bytes = b"x") -> str:
        path = os.path.join(dir_path, name)
        with open(path, "wb") as fh: fh.write(content)
        ts = time.time() - age_seconds
        os.utime(path, (ts, ts))
        return path

    # Tests
    async def test_old_media_file_deleted(self):
        old = self._put(os.path.join(self.messages_root, "telegram"), "video.mp4", 7200)
        janitor = MediaJanitor(self.platform_names, max_age_seconds=3600)
        deleted = await janitor.sweep_once()
        self.assertEqual(deleted, 1)
        self.assertFalse(os.path.exists(old))

    async def test_recent_media_file_kept(self):
        recent = self._put(os.path.join(self.messages_root, "telegram"), "fresh.mp4", 60)
        janitor = MediaJanitor(self.platform_names, max_age_seconds=3600)
        await janitor.sweep_once()
        self.assertTrue(os.path.exists(recent))

    async def test_protected_session_file_never_deleted(self):
        """A WhatsApp session.db inside the platform dir must survive,
        even when ancient."""
        session = self._put(os.path.join(self.messages_root, "discord"), "session.db", 30 * 86400)
        janitor = MediaJanitor(self.platform_names, max_age_seconds=3600)
        await janitor.sweep_once()
        self.assertTrue(os.path.exists(session), "session.db should be protected")

    async def test_protected_session_journal_kept(self):
        journal = self._put(os.path.join(self.messages_root, "telegram"), "my_bot.session-journal", 30 * 86400)
        janitor = MediaJanitor(self.platform_names, max_age_seconds=3600)
        await janitor.sweep_once()
        self.assertTrue(os.path.exists(journal))

    async def test_orphan_transcode_temp_deleted(self):
        # Both legacy /tmp and current messages/transcoded/ paths are swept.
        old_tmp = self._put(self.fake_tmp.name, "bridge_xcode_abc123.mp4", 7200)
        old_xcode = self._put(self.fake_transcode_dir.name, "bridge_xcode_def456.mp4", 7200)
        janitor = MediaJanitor(self.platform_names, max_age_seconds=3600)
        deleted = await janitor.sweep_once()
        self.assertEqual(deleted, 2)
        self.assertFalse(os.path.exists(old_tmp))
        self.assertFalse(os.path.exists(old_xcode))

    async def test_unrelated_tmp_files_left_alone(self):
        """Files in /tmp that don't match our prefix are NEVER touched."""
        other = self._put(self.fake_tmp.name, "someone_elses_file.txt", 7200)
        janitor = MediaJanitor(self.platform_names, max_age_seconds=3600)
        await janitor.sweep_once()
        self.assertTrue(os.path.exists(other), "non-bridge tmp file must not be deleted")

    async def test_multiple_platforms_swept(self):
        a = self._put(os.path.join(self.messages_root, "telegram"), "a.mp4", 7200)
        b = self._put(os.path.join(self.messages_root, "discord"), "b.png", 7200)
        janitor = MediaJanitor(self.platform_names, max_age_seconds=3600)
        deleted = await janitor.sweep_once()
        self.assertEqual(deleted, 2)
        self.assertFalse(os.path.exists(a))
        self.assertFalse(os.path.exists(b))

    async def test_floor_on_max_age(self):
        """Cannot configure a sub-minute max_age (would be too aggressive)."""
        j = MediaJanitor(self.platform_names, max_age_seconds=0.1)
        self.assertGreaterEqual(j.max_age_seconds, 60.0)

    async def test_floor_on_interval(self):
        j = MediaJanitor(self.platform_names, interval_seconds=0.5)
        self.assertGreaterEqual(j.interval_seconds, 60.0)


if __name__ == "__main__":
    unittest.main()
