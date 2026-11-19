"""One Unix user per heteronym (docs/SPEC.md §6)."""

import json
import os
import pwd
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from relayer.cli import _backend
from relayer.runtime import Runtime
from relayer.sessions import UserTmuxBackend
from relayer.users import AccountError, Accounts, UidMap

from helpers import heteronym


class Uids(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.map = UidMap(Path(self.tmp.name) / "state" / "users.json", base=30000)

    def test_assigns_in_order_and_remembers(self):
        self.assertEqual(self.map.uid("pa"), 30000)
        self.assertEqual(self.map.uid("pb"), 30001)
        self.assertEqual(self.map.uid("pa"), 30000)
        again = UidMap(self.map.path, base=30000)        # a restarted supervisor
        self.assertEqual(again.uid("pb"), 30001)

    def test_never_reuses_a_uid(self):
        self.map.uid("pa")
        self.map.uid("pb")
        data = json.loads(self.map.path.read_text())
        del data["pa"]                                   # pa removed by hand
        self.map.path.write_text(json.dumps(data))
        self.assertEqual(self.map.uid("pc"), 30002)

    def test_rejects_unsafe_names(self):
        for bad in ("Root", "a" * 31, "../x", ""):
            with self.subTest(bad=bad), self.assertRaises(AccountError):
                self.map.uid(bad)

    def test_corrupt_map_is_an_error_not_a_reset(self):
        self.map.path.parent.mkdir(parents=True)
        self.map.path.write_text("{not json")
        with self.assertRaises(AccountError):
            self.map.uid("pa")

    def test_identity(self):
        ident = Accounts(self.map, "/homes").identity("pa")
        self.assertEqual((ident.user, ident.uid, ident.home), ("h-pa", 30000, "/homes/pa"))
        self.assertEqual(ident.env(), {"HOME": "/homes/pa", "USER": "h-pa", "LOGNAME": "h-pa"})
        self.assertEqual(ident.as_user()[:3], ["setpriv", "--reuid=30000", "--regid=30000"])


class SessionEnvironment(unittest.TestCase):
    def test_identity_replaces_the_supervisors(self):
        rt = Runtime(env={"CLAUDE_CONFIG_DIR": "{home}/.claude"})
        env = rt.session_env({"HOME": "/root", "USER": "root", "PATH": "/bin"},
                             {"HOME": "/homes/pa", "USER": "h-pa", "LOGNAME": "h-pa"})
        self.assertEqual(env["HOME"], "/homes/pa")
        self.assertEqual(env["USER"], "h-pa")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/homes/pa/.claude")


class BackendChoice(unittest.TestCase):
    def test_users_isolation_needs_root(self):
        with mock.patch("relayer.cli.os.geteuid", return_value=1000):
            backend, problem = _backend(Runtime(isolation="users"), Path("/tmp"))
        self.assertIsNone(backend)
        self.assertIn("needs the supervisor to run as root", problem)

    def test_none_isolation_runs_as_current_user(self):
        backend, problem = _backend(Runtime(isolation="none"), Path("/tmp"))
        self.assertIsNone(problem)
        self.assertNotIsInstance(backend, UserTmuxBackend)


ROOT_TOOLS = os.geteuid() == 0 and all(shutil.which(t) for t in
                                        ("useradd", "groupadd", "setpriv", "tmux"))


@unittest.skipUnless(ROOT_TOOLS, "needs root, useradd, setpriv and tmux")
class RealUsers(unittest.TestCase):
    """Creates real users. The work tree lives under /tmp with world-traversable
    parents, as /work and /homes are in the image."""

    def setUp(self):
        self.base = 61000 + (os.getpid() % 500) * 4
        self.root = Path(tempfile.mkdtemp(prefix="relayer-users-", dir="/tmp"))
        os.chmod(self.root, 0o755)
        for d in ("work", "homes", "state"):
            (self.root / d).mkdir(mode=0o755)
        self.accounts = Accounts(UidMap(self.root / "state" / "users.json", self.base),
                                 str(self.root / "homes"))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for ident in self.accounts.known():
            subprocess.run(["pkill", "-KILL", "-u", str(ident.uid)], capture_output=True)
            time.sleep(0.1)
            subprocess.run(["userdel", ident.user], capture_output=True)
            subprocess.run(["groupdel", ident.user], capture_output=True)
            shutil.rmtree(f"/tmp/tmux-{ident.uid}", ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def h(self, name, *folders):
        return heteronym(name, folder=str(self.root / "work" / name)) if not folders else \
            __import__("dataclasses").replace(heteronym(name), folders=tuple(folders))

    def test_prepare_creates_user_home_and_private_folders(self):
        ident = self.accounts.prepare("pa", (str(self.root / "work" / "pa"),))
        self.assertEqual(pwd.getpwnam("h-pa").pw_uid, ident.uid)
        for path in (ident.home, str(self.root / "work" / "pa")):
            st = os.stat(path)
            self.assertEqual((st.st_uid, st.st_mode & 0o777), (ident.uid, 0o700), path)

    def test_prepare_is_idempotent_and_fixes_mode(self):
        work = str(self.root / "work" / "pa")
        ident = self.accounts.prepare("pa", (work,))
        subprocess.run([*ident.as_user(), "chmod", "755", work], check=True)
        self.accounts.prepare("pa", (work,))
        self.assertEqual(os.stat(work).st_mode & 0o777, 0o700)

    def test_refuses_a_folder_owned_by_another_heteronym(self):
        a = str(self.root / "work" / "pa")
        self.accounts.prepare("pa", (a,))
        with self.assertRaises(AccountError):
            self.accounts.prepare("pb", (a,))

    def test_refuses_a_symlinked_folder(self):
        target = self.root / "elsewhere"
        target.mkdir()
        (self.root / "work" / "pa").symlink_to(target)
        with self.assertRaisesRegex(AccountError, "symlink"):
            self.accounts.prepare("pa", (str(self.root / "work" / "pa"),))

    def test_hands_over_a_root_owned_folder_with_mode_700(self):
        work = self.root / "work" / "pa"
        work.mkdir(mode=0o755)
        os.chmod(work, 0o755)
        ident = self.accounts.prepare("pa", (str(work),))
        st = os.stat(work)
        self.assertEqual((st.st_uid, st.st_mode & 0o777), (ident.uid, 0o700))

    def test_with_only_three_capabilities(self):
        """The container's real constraint: CHOWN, SETUID, SETGID and nothing
        else, so root cannot enter a heteronym's folder and nested folders
        must be made as the heteronym."""
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from relayer.users import Accounts, UidMap\n"
            "from pathlib import Path\n"
            "a = Accounts(UidMap(Path(%r), %d), %r)\n"
            "a.prepare('pa', (%r, %r))\n"
        ) % (str(Path(__file__).resolve().parent.parent / "src"),
             str(self.root / "state" / "users.json"), self.base, str(self.root / "homes"),
             str(self.root / "work" / "pa"), str(self.root / "work" / "pa" / "notes"))
        proc = subprocess.run(["setpriv", "--bounding-set=-all,+chown,+setuid,+setgid",
                               "--inh-caps=-all", "--", "python3", "-c", script],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ident = self.accounts.identity("pa")
        notes = self.root / "work" / "pa" / "notes"
        self.assertEqual((os.stat(notes).st_uid, os.stat(notes).st_mode & 0o777),
                         (ident.uid, 0o700))
        # And root with those three capabilities cannot read inside it.
        peek = subprocess.run(["setpriv", "--bounding-set=-all,+chown,+setuid,+setgid",
                               "--inh-caps=-all", "--", "ls", str(notes)],
                              capture_output=True, text=True)
        self.assertNotEqual(peek.returncode, 0)

    def test_session_runs_as_its_user_and_others_cannot_look_in(self):
        backend = UserTmuxBackend(f"relayer-it-{os.getpid()}", self.accounts)
        pa = self.h("pa")
        pb = self.h("pb")
        for h in (pa, pb):
            backend.prepare(h)
        out = Path(pa.folders[0]) / "who.txt"
        script = f"id -un > {out}; echo $HOME >> {out}; echo END >> {out}; sleep 300"
        backend.start("pa", f"env -i HOME={self.accounts.identity('pa').home} "
                            f"PATH=/usr/bin:/bin sh -c '{script}'", pa.folders[0], "ab12")
        ident = self.accounts.identity("pa")
        deadline = time.time() + 5
        text = ""
        while time.time() < deadline and not text.endswith("END\n"):
            r = subprocess.run([*ident.as_user(), "cat", str(out)], capture_output=True, text=True)
            text = r.stdout
            time.sleep(0.05)
        self.assertEqual(text.splitlines()[:2], ["h-pa", ident.home])
        self.assertEqual(set(backend.list()), {"pa"})
        self.assertEqual(backend.list()["pa"].config_hash, "ab12")
        other = self.accounts.identity("pb")
        peek = subprocess.run([*other.as_user(), "ls", pa.folders[0]],
                              capture_output=True, text=True)
        self.assertNotEqual(peek.returncode, 0)
        backend.stop("pa")
        self.assertEqual(backend.list(), {})


if __name__ == "__main__":
    unittest.main()
