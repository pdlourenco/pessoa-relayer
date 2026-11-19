"""TmuxBackend against a real tmux server on a throwaway socket.

Skipped where tmux is not installed (CI installs it). Sessions run a short
shell command, not an agent, so the test needs no sign-in and no network.
"""

import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from relayer.sessions import SessionError, TmuxBackend


def wait_for(path: Path, timeout=5.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and path.read_text().endswith("END\n"):
            return path.read_text()
        time.sleep(0.05)
    raise AssertionError(f"{path} not written")


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class Tmux(unittest.TestCase):
    def setUp(self):
        self.socket = f"relayer-test-{os.getpid()}"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.addCleanup(subprocess.run, ["tmux", "-L", self.socket, "kill-server"],
                        capture_output=True)
        self.backend = TmuxBackend(self.socket)

    def sleeper(self):
        return shlex.join(["env", "-i", "sleep", "300"])

    def test_no_server_means_no_sessions(self):
        self.assertEqual(self.backend.list(), {})

    def test_start_list_stop(self):
        self.backend.start("pa", self.sleeper(), self.tmp.name, "abc123")
        listed = self.backend.list()
        self.assertEqual(set(listed), {"pa"})
        self.assertEqual(listed["pa"].config_hash, "abc123")
        self.assertGreater(listed["pa"].last_activity, 0)
        self.backend.stop("pa")
        self.assertEqual(self.backend.list(), {})

    def test_stop_matches_exact_name(self):
        self.backend.start("pa", self.sleeper(), self.tmp.name, "aa")
        self.backend.start("pab", self.sleeper(), self.tmp.name, "aa")
        self.backend.stop("pa")
        self.assertEqual(set(self.backend.list()), {"pab"})

    def test_duplicate_start_raises(self):
        self.backend.start("pa", self.sleeper(), self.tmp.name, "aa")
        with self.assertRaises(SessionError):
            self.backend.start("pa", self.sleeper(), self.tmp.name, "aa")

    def test_session_inherits_nothing(self):
        """Secrets in the starter's environment and in ~/.tmux.conf stay out."""
        home = self.dir / "home"
        home.mkdir()
        (home / ".tmux.conf").write_text("set-environment -g FROM_CONF leaked\n")
        out = self.dir / "env.txt"
        script = f"env > {shlex.quote(str(out))}; echo END >> {shlex.quote(str(out))}; sleep 300"
        command = shlex.join(["env", "-i", "PATH=/usr/bin:/bin", "KEEP=yes",
                              "sh", "-c", script])
        with mock.patch.dict(os.environ, {"GH_TOKEN": "secret", "HOME": str(home),
                                          "ANTHROPIC_API_KEY": "sk-x"}):
            backend = TmuxBackend(self.socket)   # picks up the patched env
            backend.start("pa", command, self.tmp.name, "aa")
        env = wait_for(out)
        self.assertIn("KEEP=yes", env)
        for leaked in ("GH_TOKEN", "ANTHROPIC_API_KEY", "FROM_CONF"):
            self.assertNotIn(leaked, env)

    def test_hash_in_workdir_is_literal(self):
        odd = self.dir / "a#(touch pwned)b"
        odd.mkdir()
        out = self.dir / "pwd.txt"
        script = f"pwd > {shlex.quote(str(out))}; echo END >> {shlex.quote(str(out))}; sleep 300"
        self.backend.start("pa", shlex.join(["sh", "-c", script]), str(odd), "aa")
        self.assertEqual(wait_for(out).splitlines()[0], str(odd))
        self.assertFalse((odd / "pwned").exists())
        self.assertFalse(Path("pwned").exists())

    def test_trailing_semicolon_argument_cannot_chain(self):
        # As one shlex-quoted string, "x;" is inside quotes, so tmux never
        # sees a bare trailing ";" and runs no second command.
        argv = ["sh", "-c", "sleep 300", "x;", "new-session", "-d", "-s", "evil"]
        self.backend.start("pa", shlex.join(argv), self.tmp.name, "aa")
        self.assertNotIn("evil", self.backend.list())

    def test_refuses_command_ending_in_separator(self):
        with self.assertRaises(SessionError):
            self.backend.start("pa", "sleep 300 ;", self.tmp.name, "aa")

    def test_refuses_unsafe_name(self):
        with self.assertRaises(SessionError):
            self.backend.start("pa;x", self.sleeper(), self.tmp.name, "aa")


if __name__ == "__main__":
    unittest.main()
