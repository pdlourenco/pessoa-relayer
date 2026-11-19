"""Heteronym memory (docs/SPEC.md §7)."""

import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from relayer.config import ConfigError
from relayer.memory import MAGIC, MemoryError_, MemoryRepo, dump, load, receive

END = struct.pack(">HQ", 0, 0)
from relayer.runtime import load_runtime
from relayer.users import Accounts, UidMap


def record(path: str, data: bytes) -> bytes:
    name = path.encode()
    return struct.pack(">HQ", len(name), len(data)) + name + data


class Stream(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def roundtrip(self, src: Path, dest: Path, **kw) -> list[str]:
        buf = io.BytesIO()
        skipped = dump(src, buf, **kw)
        buf.seek(0)
        load(buf, dest)
        return skipped

    def test_roundtrip_and_deletions(self):
        src, dest = self.tmp / "src", self.tmp / "dest"
        (src / "notes").mkdir(parents=True)
        (src / "a.md").write_text("alpha")
        (src / "notes" / "b.md").write_text("beta")
        dest.mkdir()
        (dest / "stale.md").write_text("old")
        self.roundtrip(src, dest)
        self.assertEqual((dest / "a.md").read_text(), "alpha")
        self.assertEqual((dest / "notes" / "b.md").read_text(), "beta")
        self.assertFalse((dest / "stale.md").exists())

    def test_symlinks_are_never_followed(self):
        src, dest = self.tmp / "src", self.tmp / "dest"
        src.mkdir()
        secret = self.tmp / "secret"
        secret.write_text("TOKEN")
        (src / "link").symlink_to(secret)
        (self.tmp / "dir").mkdir()
        (self.tmp / "dir" / "inner.md").write_text("outside the folder")
        (src / "dirlink").symlink_to(self.tmp / "dir")
        self.roundtrip(src, dest)
        self.assertEqual(list(dest.iterdir()), [])

    def test_size_limits_skip_not_fail(self):
        src, dest = self.tmp / "src", self.tmp / "dest"
        src.mkdir()
        (src / "big").write_bytes(b"x" * 2000)
        (src / "small").write_bytes(b"y")
        skipped = self.roundtrip(src, dest, max_file=1000)
        self.assertEqual([p.name for p in dest.iterdir()], ["small"])
        self.assertIn("big: over the memory limits", skipped[0])

    def test_git_dir_is_not_copied(self):
        src, dest = self.tmp / "src", self.tmp / "dest"
        (src / ".git").mkdir(parents=True)
        (src / ".git" / "config").write_text("[core]")
        self.roundtrip(src, dest)
        self.assertFalse((dest / ".git").exists())

    def test_unsafe_paths_are_rejected(self):
        for bad in ("../escape", "/etc/passwd", "a/../../b", ".git/hooks/post-commit", "",
                    "sub/.git/config", "a/.GIT/x", ".git./x", "GIT~1/x", "a/.gitattributes",
                    "b/.gitmodules", ".git /x", "a//b", "a\\b", "git~1:a/x",
                    "GIT~1 :a/x", "a:b", "/".join(["d"] * 33)):
            with self.subTest(bad=bad):
                dest = self.tmp / f"dest{abs(hash(bad))}"
                with self.assertRaises(MemoryError_):
                    load(io.BytesIO(MAGIC + record(bad, b"x") + END), dest)
                self.assertFalse((self.tmp / "escape").exists())
                self.assertFalse(dest.exists())

    def test_a_failed_load_leaves_the_old_copy(self):
        dest = self.tmp / "dest"
        dest.mkdir()
        (dest / "keep.md").write_text("keep")
        with self.assertRaises(MemoryError_):
            load(io.BytesIO(MAGIC + record("ok.md", b"1") + b"\x00\x05"), dest)
        self.assertEqual((dest / "keep.md").read_text(), "keep")

    def test_no_end_marker_no_change(self):
        dest = self.tmp / "dest"
        dest.mkdir()
        (dest / "keep.md").write_text("keep")
        with self.assertRaises(MemoryError_):
            load(io.BytesIO(MAGIC + record("new.md", b"1")), dest)   # cut short, cleanly
        self.assertEqual([p.name for p in dest.iterdir()], ["keep.md"])
        with self.assertRaises(MemoryError_):
            load(io.BytesIO(MAGIC + record("new.md", b"1") + END + b"x"), dest)

    def test_git_like_names_are_skipped_not_fatal(self):
        src, dest = self.tmp / "src", self.tmp / "dest"
        (src / "a" / ".Git").mkdir(parents=True)
        (src / "a" / ".Git" / "config").write_text("[core]")
        (src / ".gitattributes").write_text("* filter=x")
        (src / "notes.md").write_text("kept")
        skipped = self.roundtrip(src, dest)
        self.assertEqual(sorted(p.name for p in dest.rglob("*")), ["notes.md"])
        self.assertEqual(len(skipped), 2)

    def test_directory_count_limit(self):
        from relayer import memory
        stream = MAGIC + b"".join(record(f"d{i}/f", b"") for i in range(5)) + END
        old = memory.MAX_DIRS
        memory.MAX_DIRS = 3
        self.addCleanup(setattr, memory, "MAX_DIRS", old)
        with self.assertRaises(MemoryError_):
            receive(io.BytesIO(stream), self.tmp / "dirs")
        self.assertFalse((self.tmp / "dirs").exists())

    def test_file_count_limit(self):
        src, dest = self.tmp / "src", self.tmp / "dest"
        src.mkdir()
        for i in range(5):
            (src / f"f{i}").write_text("")
        buf = io.BytesIO()
        skipped = dump(src, buf, max_files=3)
        buf.seek(0)
        self.assertEqual(receive(buf, dest), 3)
        self.assertEqual(len(skipped), 2)
        with self.assertRaises(MemoryError_):
            receive(io.BytesIO(MAGIC + record("a", b"") + record("b", b"") + END),
                    self.tmp / "d2", max_files=1)

    def test_helper_gets_none_of_the_supervisors_secrets(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"GIT_ASKPASS": "/x", "GH_TOKEN": "t",
                                          "PATH": "/bin", "LANG": "C.UTF-8"}):
            env = MemoryRepo._helper_env()
        self.assertEqual(set(env) - {"PYTHONPATH"}, {"PATH", "LANG"})

    def test_not_a_stream(self):
        with self.assertRaises(MemoryError_):
            load(io.BytesIO(b"tar file"), self.tmp / "dest")


GOOD = """\
version: 1
class: assistants
models: {fast: h, standard: s, strong: o, frontier: f}
session:
  command: ["claude"]
"""


class Config(unittest.TestCase):
    def load(self, body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "relayer.yaml"
        p.write_text(textwrap.dedent(body))
        return load_runtime(p)

    def test_memory_section(self):
        rt = self.load(GOOD + "memory: {repo: /memory-repo, live: /memory, interval: 5m}\n")
        self.assertEqual((rt.memory_repo, rt.memory_live, rt.memory_interval),
                         ("/memory-repo", "/memory", 300))

    def test_memory_needs_user_isolation(self):
        with self.assertRaises(ConfigError) as exc:
            self.load(GOOD + "isolation: none\nmemory: {repo: /memory-repo}\n")
        self.assertIn("needs isolation: users", "\n".join(exc.exception.errors))

    def test_interval_must_be_finite(self):
        with self.assertRaises(ConfigError):
            self.load(GOOD + "memory: {repo: /r, interval: never}\n")


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


class Git(unittest.TestCase):
    """Commit and push against a local bare origin, with no git identity
    configured anywhere, as in a fresh container."""

    def setUp(self):
        for k, v in (("GIT_CONFIG_GLOBAL", "/dev/null"), ("GIT_CONFIG_NOSYSTEM", "1")):
            old = os.environ.get(k)
            os.environ[k] = v
            self.addCleanup(lambda k=k, old=old: os.environ.__setitem__(k, old)
                            if old is not None else os.environ.pop(k, None))
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.clone = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", str(origin), str(self.clone)], check=True,
                       capture_output=True)
        git(self.clone, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
            "--allow-empty", "-m", "init")
        git(self.clone, "push", "-q", "origin", "HEAD:main")
        self.other = self.tmp / "other"                  # another container's clone
        subprocess.run(["git", "clone", "-q", str(origin), str(self.other)], check=True,
                       capture_output=True)
        self.repo = MemoryRepo(self.clone, self.tmp / "live")

    def write(self, name, text):
        d = self.clone / "memory" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "notes.md").write_text(text)

    def other_commits(self, name, text):
        d = self.other / "memory" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "notes.md").write_text(text)
        git(self.other, "add", "-A")
        git(self.other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
        git(self.other, "push", "-q", "origin", "HEAD:main")

    def test_nothing_to_commit(self):
        self.assertEqual(self.repo.commit(["pa"], "now"), ([], []))

    def test_commit_and_push(self):
        self.write("pa", "one")
        self.assertEqual(self.repo.commit(["pa"], "now"), (["pa"], []))
        self.assertIsNone(self.repo.push())

    def test_a_git_link_in_one_folder_does_not_block_the_others(self):
        self.write("pa", "pa's notes")
        nested = self.clone / "memory" / "pb" / "inner"
        nested.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(nested)], check=True)
        git(nested, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
            "--allow-empty", "-m", "x")
        committed, problems = self.repo.commit(["pa", "pb"], "now")
        self.assertEqual(committed, ["pa"])
        self.assertIn("git link", problems[0])

    def test_rejected_push_rebases_and_retries(self):
        self.other_commits("op", "from the ops class")   # a different heteronym
        self.write("pa", "one")
        self.repo.commit(["pa"], "now")
        self.assertIsNone(self.repo.push())
        self.assertIsNone(self.repo.push_state.blocked)

    def test_conflict_keeps_commits_and_blocks_until_fixed(self):
        self.other_commits("pa", "someone else wrote pa's memory")
        self.write("pa", "pa's own")
        self.repo.commit(["pa"], "now")
        head = subprocess.run(["git", "-C", str(self.clone), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout
        problem = self.repo.push()
        self.assertIn("rebase onto the remote failed", problem)
        self.assertEqual(self.repo.push_state.blocked, problem)
        after = subprocess.run(["git", "-C", str(self.clone), "rev-parse", "HEAD"],
                               capture_output=True, text=True).stdout
        self.assertEqual(head, after)                    # nothing lost, nothing forced
        status = subprocess.run(["git", "-C", str(self.clone), "status", "--porcelain"],
                                capture_output=True, text=True).stdout
        self.assertEqual(status, "")                     # rebase aborted cleanly


class FakeIdent:
    """Runs the helper as the current user: no root needed."""

    def __init__(self, name):
        self.name = name

    def as_user(self):
        return []


class Helper(unittest.TestCase):
    """collect/restore against a stand-in helper, to drive failures."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "repo" / "memory" / "pa").mkdir(parents=True)
        (self.tmp / "repo" / "memory" / "pa" / "saved.md").write_text("saved")
        (self.tmp / "live" / "pa").mkdir(parents=True)
        self.repo = MemoryRepo(self.tmp / "repo", self.tmp / "live", timeout=3)

    def with_helper(self, script):
        p = self.tmp / "helper.py"
        p.write_text(textwrap.dedent(script))
        self.repo._helper = lambda ident, verb, folder: [sys.executable, str(p), verb, folder]

    def saved(self):
        return sorted(x.name for x in (self.tmp / "repo" / "memory" / "pa").iterdir())

    def test_failed_dump_keeps_the_saved_copy(self):
        self.with_helper(f"""
            import sys, struct
            out = sys.stdout.buffer
            out.write({MAGIC!r})
            name = b"partial.md"
            out.write(struct.pack(">HQ", len(name), 1) + name + b"x")
            out.write(struct.pack(">HQ", 0, 0))
            out.flush()
            sys.exit(1)
            """)
        with self.assertRaises(MemoryError_):
            self.repo.collect(FakeIdent("pa"))
        self.assertEqual(self.saved(), ["saved.md"])

    def test_a_hung_helper_is_killed(self):
        self.with_helper("""
            import sys, time
            sys.stdout.buffer.write(b"RLMEM2\\n"); sys.stdout.flush()
            time.sleep(60)
            """)
        start = time.time()
        with self.assertRaises(MemoryError_):
            self.repo.collect(FakeIdent("pa"))
        self.assertLess(time.time() - start, 20)
        self.assertEqual(self.saved(), ["saved.md"])

    def test_stderr_flood_does_not_wedge(self):
        self.with_helper(f"""
            import sys
            sys.stderr.write("x" * 2_000_000)
            sys.stdout.buffer.write({MAGIC!r} + b"\\x00" * 10)
            """)
        self.repo.collect(FakeIdent("pa"))
        self.assertEqual(self.saved(), [])

    def test_a_held_pipe_cannot_outlast_the_deadline(self):
        """The heteronym's session can hold a copy of the helper's stdout, so
        killing the helper never brings EOF. Here a grandchild holds it."""
        self.with_helper("""
            import subprocess, sys, time
            subprocess.Popen(["sleep", "30"])          # inherits stdout and keeps it open
            sys.stdout.buffer.write(b"RLMEM2\\n"); sys.stdout.flush()
            time.sleep(30)
            """)
        start = time.time()
        with self.assertRaises(MemoryError_):
            self.repo.collect(FakeIdent("pa"))
        self.assertLess(time.time() - start, 10)
        self.assertEqual(self.saved(), ["saved.md"])
        subprocess.run(["pkill", "-f", "^sleep 30$"], capture_output=True)

    def test_symlinked_folders_in_the_clone_are_refused(self):
        secret = self.tmp / "secret"
        secret.mkdir()
        (secret / "credentials").write_text("TOKEN")
        mem = self.tmp / "repo" / "memory"
        shutil.rmtree(mem / "pa")
        (mem / "pa").symlink_to(secret)
        self.assertFalse(self.repo.restore(FakeIdent("pa")))
        shutil.rmtree(mem)
        mem.symlink_to(secret)
        with self.assertRaises(MemoryError_):
            self.repo.collect(FakeIdent("pa"))
        self.assertTrue((secret / "credentials").exists())


ROOT = os.geteuid() == 0 and shutil.which("setpriv") and shutil.which("useradd")


@unittest.skipUnless(ROOT, "needs root, setpriv and useradd")
class RealUsers(unittest.TestCase):
    def setUp(self):
        self.base = 62000 + (os.getpid() % 200) * 4
        self.root = Path(tempfile.mkdtemp(prefix="relayer-mem-", dir="/tmp"))
        os.chmod(self.root, 0o755)
        for d in ("work", "homes", "state", "live", "repo"):
            (self.root / d).mkdir(mode=0o755)
        subprocess.run(["git", "init", "-q", str(self.root / "repo")], check=True)
        # The helper runs as the heteronym, which cannot read this checkout;
        # in the image the package sits in a world-readable venv. Mirror that.
        lib = self.root / "pylib"
        shutil.copytree(Path(__file__).resolve().parent.parent / "src" / "relayer",
                        lib / "relayer")
        subprocess.run(["chmod", "-R", "a+rX", str(lib)], check=True)
        self.lib = str(lib)
        old = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = self.lib
        self.addCleanup(lambda: os.environ.__setitem__("PYTHONPATH", old) if old is not None
                        else os.environ.pop("PYTHONPATH", None))
        self.accounts = Accounts(UidMap(self.root / "state" / "users.json", self.base),
                                 str(self.root / "homes"), str(self.root / "work"))
        self.repo = MemoryRepo(self.root / "repo", self.root / "live")
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for ident in self.accounts.known():
            subprocess.run(["pkill", "-KILL", "-u", str(ident.uid)], capture_output=True)
            time.sleep(0.1)
            subprocess.run(["userdel", ident.user], capture_output=True)
            subprocess.run(["groupdel", ident.user], capture_output=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def live(self, name):
        ident = self.accounts.prepare(name, ())
        self.accounts.ensure_dir(ident, self.repo.live_dir(name))
        return ident

    def as_user(self, ident, *argv):
        subprocess.run([*ident.as_user(), *argv], check=True)

    def test_collect_copies_only_regular_files_as_the_heteronym(self):
        ident = self.live("pa")
        live = self.repo.live_dir("pa")
        self.as_user(ident, "sh", "-c", f"echo hello > {live}/notes.md")
        secret = self.root / "secret"
        secret.write_text("ROOT ONLY")
        os.chmod(secret, 0o600)
        self.as_user(ident, "ln", "-s", str(secret), f"{live}/steal")
        notes = self.repo.collect(ident)
        copied = self.root / "repo" / "memory" / "pa"
        self.assertEqual((copied / "notes.md").read_text(), "hello\n")
        self.assertFalse((copied / "steal").exists())
        self.assertEqual(notes, [])

    def test_collect_works_with_only_three_capabilities(self):
        ident = self.live("pa")
        live = self.repo.live_dir("pa")
        self.as_user(ident, "sh", "-c", f"echo bounded > {live}/n.md")
        src = self.lib
        script = (f"import sys; sys.path.insert(0, {src!r})\n"
                  "from pathlib import Path\n"
                  "from relayer.users import Accounts, UidMap\n"
                  "from relayer.memory import MemoryRepo\n"
                  f"a = Accounts(UidMap(Path({str(self.root / 'state' / 'users.json')!r}), "
                  f"{self.base}), {str(self.root / 'homes')!r}, {str(self.root / 'work')!r})\n"
                  f"r = MemoryRepo(Path({str(self.root / 'repo')!r}), Path({str(self.root / 'live')!r}))\n"
                  "r.collect(a.identity('pa'))\n")
        proc = subprocess.run(["setpriv", "--bounding-set=-all,+chown,+setuid,+setgid",
                               "--inh-caps=-all", "--", "python3", "-c", script],
                              capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], "PYTHONPATH": src})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.root / "repo" / "memory" / "pa" / "n.md").read_text(), "bounded\n")

    def test_restore_fills_only_an_empty_live_folder(self):
        saved = self.root / "repo" / "memory" / "pa"
        saved.mkdir(parents=True)
        (saved / "notes.md").write_text("remembered")
        ident = self.live("pa")
        self.assertTrue(self.repo.restore(ident))
        restored = Path(self.repo.live_dir("pa")) / "notes.md"
        st = os.stat(restored)
        self.assertEqual((st.st_uid, restored.read_text()), (ident.uid, "remembered"))
        (saved / "notes.md").write_text("older copy")
        self.repo.restore(ident)                      # live folder not empty: untouched
        self.assertEqual(restored.read_text(), "remembered")


if __name__ == "__main__":
    unittest.main()
