"""TmuxBackend against a real tmux server on a throwaway socket.

Skipped where tmux is not installed. The session runs ``sleep``, not an
agent, so the test needs no sign-in and no network.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from relayer.sessions import SessionError, TmuxBackend


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class Tmux(unittest.TestCase):
    def setUp(self):
        self.socket = f"relayer-test-{os.getpid()}"
        self.backend = TmuxBackend(self.socket)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(subprocess.run, ["tmux", "-L", self.socket, "kill-server"],
                        capture_output=True)

    def test_no_server_means_no_sessions(self):
        self.assertEqual(self.backend.list(), {})

    def test_start_list_stop(self):
        self.backend.start("pa", ["sleep", "300"], self.tmp.name, {"X": "1"}, "abc123")
        listed = self.backend.list()
        self.assertEqual(set(listed), {"pa"})
        self.assertEqual(listed["pa"].config_hash, "abc123")
        self.assertGreater(listed["pa"].last_activity, 0)
        self.backend.stop("pa")
        self.assertEqual(self.backend.list(), {})

    def test_stop_matches_exact_name(self):
        self.backend.start("pa", ["sleep", "300"], self.tmp.name, {}, "h")
        self.backend.start("pab", ["sleep", "300"], self.tmp.name, {}, "h")
        self.backend.stop("pa")
        self.assertEqual(set(self.backend.list()), {"pab"})

    def test_duplicate_start_raises(self):
        self.backend.start("pa", ["sleep", "300"], self.tmp.name, {}, "h")
        with self.assertRaises(SessionError):
            self.backend.start("pa", ["sleep", "300"], self.tmp.name, {}, "h")


if __name__ == "__main__":
    unittest.main()
