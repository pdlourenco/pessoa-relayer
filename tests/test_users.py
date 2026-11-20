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
from relayer.users import AccountError, Accounts, UidMap, processes_of, run_as

# Run as a heteronym: SIGSTOP every other process of its uid, as fast as it
# can, so any helper running as that uid is frozen before it acts.
STOPPER = """
import os, signal, time
me, uid = os.getpid(), os.getuid()
while True:
    for d in os.listdir('/proc'):
        if d.isdigit() and int(d) != me:
            try:
                if os.stat('/proc/' + d).st_uid == uid:
                    os.kill(int(d), signal.SIGSTOP)
            except OSError:
                pass
    time.sleep(0.002)
"""


def finishes(fn, limit):
    """Run fn in a thread; (finished, result or exception)."""
    import threading
    box = {}

    def go():
        try:
            box["ok"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["err"] = exc
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(limit)
    return not t.is_alive(), box

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


class Processes(unittest.TestCase):
    def test_zombies_are_not_live_processes(self):
        # A killed process stays a zombie until its parent reaps it; CI's
        # runner adopts orphans and reaps late, which once made a clean stop
        # look like a survivor.
        from relayer.users import processes_of
        child = subprocess.Popen(["true"])
        deadline = time.time() + 5
        while time.time() < deadline:
            with open(f"/proc/{child.pid}/status") as fh:
                if "State:\tZ" in fh.read():
                    break
            time.sleep(0.02)
        self.assertNotIn(child.pid, processes_of(os.getuid()))
        live = subprocess.Popen(["sleep", "5"])
        self.addCleanup(live.kill)
        self.assertIn(live.pid, processes_of(os.getuid()))
        child.wait()


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


def remove_user(ident):
    """Kill a test user's processes, wait until none is live, then delete it.
    userdel refuses a user with a running process, and a stale user breaks
    the next run's tests."""
    from relayer.users import processes_of
    deadline = time.time() + 5
    while processes_of(ident.uid) and time.time() < deadline:
        subprocess.run(["kill", "-9", *map(str, processes_of(ident.uid))], capture_output=True)
        time.sleep(0.05)
    for _ in range(20):
        if subprocess.run(["userdel", ident.user], capture_output=True).returncode in (0, 6):
            break                                  # 6: no such user
        time.sleep(0.1)
    subprocess.run(["groupdel", ident.user], capture_output=True)
    shutil.rmtree(f"/tmp/tmux-{ident.uid}", ignore_errors=True)


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
                                 str(self.root / "homes"), str(self.root / "work"))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for ident in self.accounts.known():
            remove_user(ident)
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
        # Root with these capabilities cannot read a checkout under a home
        # it does not own (CI's /home/runner): give it a world-readable copy.
        lib = self.root / "pylib"
        shutil.copytree(Path(__file__).resolve().parent.parent / "src" / "relayer",
                        lib / "relayer")
        subprocess.run(["chmod", "-R", "a+rX", str(lib)], check=True)
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from relayer.users import Accounts, UidMap\n"
            "from pathlib import Path\n"
            "a = Accounts(UidMap(Path(%r), %d), %r)\n"
            "a.prepare('pa', (%r, %r))\n"
        ) % (str(lib),
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

    def test_symlink_inside_own_folder_cannot_redirect_root(self):
        """A heteronym owns its folder and may plant anything in it. A nested
        folder from its definition must never make root act through that."""
        target = self.root / "target"
        (target / "bin").mkdir(parents=True)
        os.chmod(target, 0o755)
        os.chmod(target / "bin", 0o755)
        g = str(self.root / "work" / "g")
        ident = self.accounts.prepare("g", (g,))
        subprocess.run([*ident.as_user(), "ln", "-s", str(target), f"{g}/sub"], check=True)
        with self.assertRaises(AccountError):
            self.accounts.prepare("g", (g, f"{g}/sub/bin"))
        self.assertEqual(os.stat(target / "bin").st_uid, 0)
        self.assertEqual(os.stat(target / "bin").st_mode & 0o777, 0o755)

    def test_root_never_acts_on_a_path_only_on_descriptors(self):
        """Pins the race fix: a path can be swapped between check and use, a
        held descriptor cannot. Root-side mkdir must be relative to a held
        directory, and chmod/chown must go through descriptors."""
        real_mkdir = os.mkdir

        def mkdir(path, mode=0o777, *, dir_fd=None):
            if dir_fd is None:
                raise AssertionError(f"root mkdir by path: {path}")
            return real_mkdir(path, mode, dir_fd=dir_fd)

        def by_path(*args, **kwargs):
            raise AssertionError(f"root chmod/chown by path: {args[0]}")

        work = str(self.root / "work" / "pa")
        with mock.patch("relayer.users.os.mkdir", mkdir), \
                mock.patch("relayer.users.os.chmod", by_path), \
                mock.patch("relayer.users.os.chown", by_path):
            self.accounts.prepare("pa", (work, f"{work}/notes"))
        self.assertEqual(os.stat(f"{work}/notes").st_uid, self.accounts.identity("pa").uid)

    def test_a_stray_session_does_not_break_listing(self):
        backend = UserTmuxBackend(f"relayer-it-{os.getpid()}", self.accounts)
        pa = self.h("pa")
        backend.prepare(pa)
        backend.start("pa", "sleep 300", pa.folders[0], "ab12")
        ident = self.accounts.identity("pa")
        # The heteronym makes sessions of its own on its own server.
        subprocess.run([*ident.as_user(), "env", f"HOME={ident.home}",
                        f"TMUX_TMPDIR={ident.home}", "tmux", "-L", backend.socket,
                        "new-session", "-d", "-s", "EVIL|x", "sleep 300"], check=True)
        self.assertEqual(set(backend.list()), {"pa"})
        self.assertEqual(backend.errors, [])

    def test_another_heteronym_cannot_squat_the_socket_dir(self):
        backend = UserTmuxBackend(f"relayer-it-{os.getpid()}", self.accounts)
        pa, pb = self.h("pa"), self.h("pb")
        backend.prepare(pa)
        a = self.accounts.identity("pa")
        b_uid = self.accounts.uids.base + 1          # predictable: pb is next
        subprocess.run([*a.as_user(), "mkdir", "-m", "700", f"/tmp/tmux-{b_uid}"])
        self.addCleanup(shutil.rmtree, f"/tmp/tmux-{b_uid}", True)
        backend.prepare(pb)
        self.assertEqual(self.accounts.identity("pb").uid, b_uid)
        backend.start("pb", "sleep 300", pb.folders[0], "cd34")
        self.assertIn("pb", backend.list())

    def test_a_lost_uid_map_never_hands_files_to_a_stranger(self):
        pz = str(self.root / "work" / "pz")
        old = self.accounts.prepare("pz", (pz,))
        self.accounts.uids.path.unlink()                  # state lost
        stranger = self.accounts.identity("aa")           # sorts first, arrives first
        self.assertNotEqual(stranger.uid, old.uid)
        self.assertEqual(self.accounts.identity("pz").uid, old.uid)   # rebuilt from its home

    def _orphan_highest(self, folders_for_old):
        """``top`` then ``old`` (the highest uid); then the map, old's home
        and its user are lost. Returns old's identity."""
        top = self.accounts.prepare("top", (str(self.root / "work" / "top"),))
        self.addCleanup(remove_user, top)        # the map that names it is lost
        old = self.accounts.prepare("old", folders_for_old)
        self.accounts.uids.path.unlink()
        shutil.rmtree(old.home)
        remove_user(old)
        return old

    def test_a_lost_map_counts_owners_of_nested_folders(self):
        """Probe review of #6: /work/old is root's, old owns /work/old/notes."""
        old = self._orphan_highest((str(self.root / "work" / "old" / "notes"),))
        self.assertEqual(os.stat(self.root / "work" / "old").st_uid, 0)
        self.assertNotEqual(self.accounts.identity("newcomer").uid, old.uid)

    def test_a_lost_map_counts_owners_under_other_roots(self):
        live = self.root / "live"
        live.mkdir(mode=0o755)
        self.accounts.other_roots = (str(live),)
        old = self._orphan_highest((str(live / "old"),))
        self.assertNotEqual(self.accounts.identity("newcomer").uid, old.uid)

    def test_parents_made_on_the_way_stay_roots(self):
        d = str(self.root / "work" / "projects" / "delta")
        e = str(self.root / "work" / "projects" / "eps")
        self.accounts.prepare("delta", (d,))
        parent = os.stat(self.root / "work" / "projects")
        self.assertEqual((parent.st_uid, parent.st_mode & 0o777), (0, 0o755))
        self.accounts.prepare("eps", (e,))           # not refused: the parent is root's
        self.assertEqual(os.stat(e).st_uid, self.accounts.identity("eps").uid)

    def test_a_hidden_session_and_its_daemons_are_still_stopped(self):
        backend = UserTmuxBackend(f"relayer-it-{os.getpid()}", self.accounts)
        pa = self.h("pa")
        backend.prepare(pa)
        backend.start("pa", "sleep 300", pa.folders[0], "ab12")
        ident = self.accounts.identity("pa")
        env = ["env", f"HOME={ident.home}", f"TMUX_TMPDIR={ident.home}"]
        # Hide the session from listing, and leave a detached daemon.
        subprocess.run([*ident.as_user(), *env, "tmux", "-L", backend.socket,
                        "set-option", "-t", "pa", "@relayer_hash", "x|y"], check=True)
        subprocess.run([*ident.as_user(), "setsid", "-f", "sleep", "301"], check=True)
        listed = backend.list()
        self.assertEqual(listed["pa"].config_hash, "")      # seen, untagged
        backend.stop("pa")
        self.assertEqual(__import__("relayer.users", fromlist=["x"]).processes_of(ident.uid), [])
        self.assertEqual(backend.list(), {})

    def _assert_listing_survives(self, attack):
        """Start pa, let ``attack(ident, backend)`` make its tmux server
        unresponsive, then list: it must come back, report pa, and leave pa
        nothing running (probe review of #6). Popen.kill fails as it does
        for root without CAP_KILL, so nothing may rely on it."""
        backend = UserTmuxBackend(f"relayer-it-{os.getpid()}", self.accounts, timeout=1)
        pa = self.h("pa")
        backend.prepare(pa)
        backend.start("pa", "sleep 300", pa.folders[0], "ab12")
        ident = self.accounts.identity("pa")
        attack(ident, backend)
        with mock.patch("subprocess.Popen.kill", side_effect=PermissionError(1, "EPERM")):
            done, result = finishes(backend.list, 30)
        self.assertTrue(done, "list() stalled on an unresponsive tmux server")
        self.assertNotIn("err", result, result.get("err"))
        self.assertNotIn("pa", result["ok"])
        self.assertTrue(any("pa" in e for e in backend.errors), backend.errors)
        self.assertEqual(processes_of(ident.uid), [])

    def test_a_stopped_tmux_server_does_not_stall_listing(self):
        def stop_server(ident, backend):
            subprocess.run([*ident.as_user(), "pkill", "-STOP", "-u", str(ident.uid), "-x",
                            "tmux: server"], check=False)
            subprocess.run([*ident.as_user(), "pkill", "-STOP", "-u", str(ident.uid), "tmux"],
                           check=True)
        self._assert_listing_survives(stop_server)

    def test_a_heteronym_stopping_its_own_processes_does_not_stall_listing(self):
        """Second review of #6: a kill run as the heteronym can be stopped by
        it before it signals; the kill must come from outside its reach."""
        def stop_everything(ident, backend):
            subprocess.run([*ident.as_user(), "setsid", "-f", "python3", "-c", STOPPER],
                           check=True)
            time.sleep(0.5)                       # the stopper has frozen the server
        self._assert_listing_survives(stop_everything)

    def test_kill_as_reaches_only_the_heteronyms_processes(self):
        """Re-probe of #6: the killing child keeps real uid 0, which the kernel
        lets signal any root process; kill_as must still hit only uid's."""
        from relayer.users import kill_as
        pa = self.accounts.prepare("pa", ())
        pb = self.accounts.prepare("pb", ())
        root_sleep, b_sleep, a_sleep = self._sleepers(pa, pb)
        kill_as(pa.uid, [root_sleep.pid, b_sleep.pid, a_sleep.pid])
        self.assertEqual(a_sleep.wait(timeout=5), -9)      # the control: pa's own
        self.assertIsNone(root_sleep.poll(), "kill_as killed a root process")
        self.assertIsNone(b_sleep.poll(), "kill_as killed another heteronym's process")

    def _sleepers(self, pa, pb):
        """A sleep each as root, pb and pa, once setpriv has switched uid."""
        from relayer.users import _real_uid
        procs = (subprocess.Popen(["sleep", "60"]),
                 subprocess.Popen([*pb.as_user(), "sleep", "60"]),
                 subprocess.Popen([*pa.as_user(), "sleep", "60"]))

        def reap():
            for p in procs:
                p.kill()
                p.wait()
        self.addCleanup(reap)
        deadline = time.time() + 5
        while time.time() < deadline and (_real_uid(procs[1].pid), _real_uid(procs[2].pid)) \
                != (pb.uid, pa.uid):
            time.sleep(0.02)
        return procs

    def test_kill_as_without_pidfds_still_checks_the_uid(self):
        """Second review: where pidfd_open fails (old kernel, seccomp), kill_as
        must neither do nothing nor kill another uid's process."""
        from relayer.users import kill_as
        pa = self.accounts.prepare("pa", ())
        pb = self.accounts.prepare("pb", ())
        root_sleep, b_sleep, a_sleep = self._sleepers(pa, pb)
        with mock.patch("relayer.users.os.pidfd_open", side_effect=OSError(38, "ENOSYS")):
            kill_as(pa.uid, [root_sleep.pid, b_sleep.pid, a_sleep.pid])
        self.assertEqual(a_sleep.wait(timeout=5), -9)
        self.assertIsNone(root_sleep.poll(), "killed a root process")
        self.assertIsNone(b_sleep.poll(), "killed another heteronym's process")

    def test_a_process_whose_main_thread_exited_is_still_live(self):
        """Second review: such a leader shows state Z while its threads run;
        it must not hide from processes_of, and so from a stop."""
        from relayer.users import kill_as
        pa = self.accounts.prepare("pa", ())
        script = ("import ctypes, threading, time\n"
                  "threading.Thread(target=time.sleep, args=(300,)).start()\n"
                  "ctypes.CDLL(None).pthread_exit(None)\n")
        proc = subprocess.Popen([*pa.as_user(), "python3", "-c", script])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        deadline, state = time.time() + 5, ""
        while time.time() < deadline:
            with open(f"/proc/{proc.pid}/status") as fh:
                status = fh.read()
            if "State:\tZ" in status and "Threads:\t2" in status:
                state = "threaded zombie leader"
                break
            time.sleep(0.02)
        self.assertEqual(state, "threaded zombie leader")
        self.assertIn(proc.pid, processes_of(pa.uid))
        kill_as(pa.uid, processes_of(pa.uid))
        self.assertEqual(proc.wait(timeout=5), -9)

    def test_a_command_run_as_the_heteronym_is_bounded(self):
        ident = self.accounts.prepare("pa", ())
        subprocess.run([*ident.as_user(), "setsid", "-f", "python3", "-c", STOPPER],
                       check=True)
        time.sleep(0.3)
        with mock.patch("subprocess.Popen.kill", side_effect=PermissionError(1, "EPERM")):
            done, result = finishes(lambda: run_as(ident, ["sleep", "60"], timeout=1), 20)
        self.assertTrue(done, "run_as stalled on a stopped process")
        self.assertIsInstance(result.get("err"), AccountError)
        def argv0(pid):
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    return fh.read().split(b"\0")[0]
            except OSError:
                return b""
        self.assertNotIn(b"sleep", [argv0(p) for p in processes_of(ident.uid)])

    def test_a_silent_listener_on_the_socket_does_not_stall_listing(self):
        def squat(ident, backend):
            sock = f"{ident.home}/tmux-{ident.uid}/{backend.socket}"
            env = ["env", f"HOME={ident.home}", f"TMUX_TMPDIR={ident.home}"]
            subprocess.run([*ident.as_user(), *env, "tmux", "-L", backend.socket,
                            "kill-server"], check=True)
            listener = ("import os, socket, time\n"
                        f"p = {sock!r}\n"
                        "while os.path.exists(p):\n"
                        "    try: os.unlink(p)\n"
                        "    except OSError: time.sleep(0.05)\n"
                        "s = socket.socket(socket.AF_UNIX); s.bind(p); s.listen(8)\n"
                        "open(p + '.ready', 'w').close()\n"
                        "held = []\n"
                        "while True: held.append(s.accept())\n")
            subprocess.run([*ident.as_user(), "setsid", "-f", "python3", "-c", listener],
                           check=True)
            deadline = time.time() + 5
            while not os.path.exists(sock + ".ready") and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.exists(sock + ".ready"), "listener did not start")
        self._assert_listing_survives(squat)

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



class Login(unittest.TestCase):
    """relayer login (docs/SPEC.md §6.3, §6.6), without real users."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        repo = Path(__file__).resolve().parent.parent
        (root / "relayer.yaml").write_text(
            "version: 1\nclass: assistants\nisolation: none\n"
            "models: {fast: f, standard: s, strong: o, frontier: x}\n"
            "session:\n  command: [claude, --model, '{model}', --remote-control]\n")
        (root / "heteronyms.yaml").write_text(
            "version: 1\nheteronyms:\n  pa: {class: assistants, template: assistant}\n")
        self.argv = ["login", "--heteronyms", str(root / "heteronyms.yaml"),
                     "--templates", str(repo / "templates"),
                     "--runtime", str(root / "relayer.yaml"),
                     "--work-root", str(root / "work"), "--state", str(root / "state"), "pa"]

    def run_login(self, running):
        from relayer import cli
        from relayer.sessions import Running
        fake = mock.Mock()
        fake.list.return_value = {"pa": Running("pa", "h", 0)} if running else {}
        with mock.patch.object(cli, "_backend", return_value=(fake, None)), \
                mock.patch.object(cli.os, "execvp") as execvp, mock.patch("sys.stderr"):
            code = cli.main(self.argv)
        return code, execvp

    def test_runs_the_full_session_command(self):
        code, execvp = self.run_login(running=False)
        argv = execvp.call_args.args[1]
        self.assertEqual(argv[-4:], ["claude", "--model", "s", "--remote-control"])

    def test_refuses_while_the_session_runs(self):
        code, execvp = self.run_login(running=True)
        self.assertEqual(code, 1)
        execvp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
