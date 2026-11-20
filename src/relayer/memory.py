"""Heteronym memory, versioned in a private git repository (docs/SPEC.md §7).

Each heteronym writes only its own live folder, ``<live>/<name>`` (mode 700,
its own user). The supervisor keeps a root-owned clone of the memory
repository and, every interval, copies each heteronym's folder into
``<repo>/memory/<name>/``, commits, and pushes.

The supervisor cannot read a heteronym's folder (no DAC_OVERRIDE, by
design), and must never extract an archive a heteronym controls as root.
So the copy runs in two halves joined by a pipe:

- the *dump* half runs as the heteronym (``python3 -m relayer.memory dump``)
  and writes a stream of (path, bytes) records for the regular files it can
  see, never following a symlink, then an end marker;
- the *receive* half runs as root and writes those records into a staging
  directory in the root-owned clone, checking every path. The staging
  directory replaces the saved copy only after the end marker arrived and
  the helper exited cleanly.

The stream is treated as fully hostile: the helper runs as the heteronym,
whose session can tamper with a same-uid process, so nothing in it is
trusted because "our code wrote it".

Restoring a fresh live folder from the repository is the same pipe the
other way round, with the receive half running as the heteronym.

Memory grants nothing: it is advice to a session, never a permission.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import select
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

from .users import kill_child

MAGIC = b"RLMEM2\n"
MAX_FILE = 5 * 1024 * 1024          # one file; larger ones are skipped
MAX_TOTAL = 50 * 1024 * 1024        # one heteronym's memory, names included
MAX_FILES = 10_000                  # one heteronym's file count
HELPER_TIMEOUT = 120                # seconds a dump or restore may take
_HEADER = struct.Struct(">HQ")      # path length, data length
_END = _HEADER.pack(0, 0)           # end marker: a record with an empty path
# Any component git could treat as its own: .git and every .git* name
# (.gitattributes, .gitmodules, ...), in any case, with the trailing dots or
# spaces some filesystems drop, and anything with an NTFS 8.3 short-name
# tail "~<digit>": git reads "git~1" as .git and "gitmod~1" or "gi7eba~1" as
# .gitmodules, and its fsck rejects bad ones. Checked after removing Unicode
# format characters (category Cf), which HFS+ ignores: ".g\u200cit" is
# ".git" there, and git's fsck rejects it (hasDotgit).
_GITLIKE = re.compile(r"^(\.git.*|.*~\d.*)$", re.IGNORECASE)
MAX_DEPTH = 32                      # path components in one memory path
MAX_DIRS = 10_000                   # directories in one heteronym's memory
_GIT_SAFE = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
             "-c", "submodule.recurse=false",
             # A second line behind _gitlike: git itself refuses paths that
             # are .git on HFS+ or NTFS checkouts.
             "-c", "core.protectHFS=true", "-c", "core.protectNTFS=true",
             # Every command, not just commit: `pull --rebase` rewrites commits
             # and needs a committer, and a fresh container has no git identity.
             "-c", "user.name=pessoa-relayer", "-c", "user.email=relayer@localhost")


class MemoryError_(RuntimeError):
    pass


def _gitlike(component: str) -> bool:
    visible = "".join(c for c in component if unicodedata.category(c) != "Cf")
    return bool(_GITLIKE.match(visible.rstrip(". ")))


def _safe_rel(path: str) -> str:
    """A relative, normalised path with no git-like component anywhere."""
    p = PurePosixPath(path)
    if (p.is_absolute() or not p.parts or any(x in ("", ".", "..") for x in p.parts)
            or any(_gitlike(x) for x in p.parts) or "\0" in path or "\\" in path
            or ":" in path or len(p.parts) > MAX_DEPTH
            or len(path.encode()) > 4096 or str(p) != path):
        raise MemoryError_(f"unsafe path in memory stream: {path!r}")
    return path


# --- the stream -------------------------------------------------------------

def dump(root: Path, out: BinaryIO, max_file: int = MAX_FILE,
         max_total: int = MAX_TOTAL, max_files: int = MAX_FILES) -> list[str]:
    """Write every regular file under ``root``, then the end marker; return
    what was skipped and why."""
    skipped: list[str] = []
    total = count = 0
    out.write(MAGIC)
    for rel in sorted(_walk(root, root, skipped)):
        try:
            rel.encode("utf-8")
            _safe_rel(rel)
        except (UnicodeEncodeError, MemoryError_):
            skipped.append(f"{rel!r}: name not allowed in memory")
            continue
        try:
            # O_NONBLOCK: a FIFO swapped in must not block the open.
            fd = os.open(root / rel, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as exc:
            skipped.append(f"{rel}: {exc.strerror}")
            continue
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                skipped.append(f"{rel}: not a regular file")
                continue
            name = rel.encode()
            if (st.st_size > max_file or total + st.st_size + len(name) > max_total
                    or count >= max_files):
                skipped.append(f"{rel}: over the memory limits")
                continue
            data = fh.read(max_file + 1)
        if len(data) > max_file:
            skipped.append(f"{rel}: grew past the limit")
            continue
        total += len(data) + len(name)
        count += 1
        out.write(_HEADER.pack(len(name), len(data)) + name + data)
    out.write(_END)
    out.flush()
    return skipped


def _walk(root: Path, top: Path, skipped: list[str]) -> Iterator[str]:
    try:
        entries = list(os.scandir(top))
    except OSError as exc:
        skipped.append(f"{os.path.relpath(top, root)}: {exc.strerror}")
        return
    for entry in entries:
        rel = os.path.relpath(entry.path, root)
        if _gitlike(entry.name):
            skipped.append(f"{rel}: git-like names are not kept in memory")
            continue
        if entry.is_dir(follow_symlinks=False):
            yield from _walk(root, Path(entry.path), skipped)
        elif entry.is_file(follow_symlinks=False):
            yield rel


def receive(stream: BinaryIO, staging: Path, max_total: int = MAX_TOTAL,
            max_files: int = MAX_FILES) -> int:
    """Write the files in ``stream`` into a new directory ``staging``.

    Returns the file count. Leaves nothing behind unless the whole stream,
    end marker included, was valid.
    """
    if stream.read(len(MAGIC)) != MAGIC:
        raise MemoryError_("not a memory stream")
    staging.mkdir(mode=0o700)
    count = total = 0
    dirs: set[str] = set()
    try:
        while True:
            head = stream.read(_HEADER.size)
            if len(head) != _HEADER.size:
                raise MemoryError_("memory stream ended without its end marker")
            name_len, data_len = _HEADER.unpack(head)
            if name_len == 0:
                if data_len != 0 or stream.read(1):
                    raise MemoryError_("data after the memory stream's end marker")
                return count
            count += 1
            total += data_len + name_len
            if data_len > MAX_FILE or total > max_total or count > max_files:
                raise MemoryError_("memory stream exceeds the limits")
            try:
                rel = _safe_rel(stream.read(name_len).decode("utf-8", "strict"))
            except UnicodeDecodeError:
                raise MemoryError_("memory stream path is not UTF-8") from None
            data = stream.read(data_len)
            if len(data) != data_len:
                raise MemoryError_("truncated memory stream")
            target = staging / rel
            parents = PurePosixPath(rel).parents
            dirs.update(str(d) for d in parents if str(d) != ".")
            if len(dirs) > MAX_DIRS:
                raise MemoryError_("memory stream has too many directories")
            try:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600)
            except OSError as exc:
                # A repeated name, a file and a directory of one name, or a
                # name too long for the filesystem.
                raise MemoryError_(f"memory stream cannot write {rel!r}: "
                                   f"{exc.strerror}") from None
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def replace(staging: Path, dest: Path) -> None:
    """Swap ``staging`` in for ``dest`` (neither may be a symlink)."""
    for p in (dest, staging):
        if p.is_symlink():
            raise MemoryError_(f"{p} is a symlink; refusing to touch it")
    old = dest.with_name(dest.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        os.rename(dest, old)
    os.rename(staging, dest)
    shutil.rmtree(old, ignore_errors=True)


def load(stream: BinaryIO, dest: Path, max_total: int = MAX_TOTAL) -> int:
    """receive() then replace(): for callers with nothing else to check."""
    staging = dest.with_name(dest.name + ".incoming")
    shutil.rmtree(staging, ignore_errors=True)
    count = receive(stream, staging, max_total)
    replace(staging, dest)
    return count


# --- deadline-bounded pipe ends -----------------------------------------------

class _DeadlineReader:
    """``read(n)`` on a pipe that gives up at a deadline.

    Killing a helper does not close its stdout if the heteronym's session
    holds a copy of the descriptor (pidfd_getfd), so EOF may never come;
    every read waits only until the one deadline for the whole transfer.
    """

    def __init__(self, fd: int, deadline: float):
        self.fd, self.deadline = fd, deadline
        os.set_blocking(fd, False)

    def read(self, n: int) -> bytes:
        chunks, want = [], n
        while want > 0:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise MemoryError_("memory helper timed out")
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(self.fd, min(want, 1 << 16))
            if not chunk:
                break                       # EOF
            chunks.append(chunk)
            want -= len(chunk)
        return b"".join(chunks)


class _DeadlineWriter:
    """``write``/``flush`` on a pipe that gives up at a deadline."""

    def __init__(self, fd: int, deadline: float):
        self.fd, self.deadline = fd, deadline
        os.set_blocking(fd, False)

    def write(self, data: bytes) -> int:
        view = memoryview(data)
        while view:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise MemoryError_("memory helper timed out")
            _, ready, _ = select.select([], [self.fd], [], remaining)
            if ready:
                view = view[os.write(self.fd, view[: 1 << 16]):]
        return len(data)

    def flush(self) -> None:
        pass


def _drain_capped(fd: int, keep: list[bytes], cap: int = 64 * 1024) -> None:
    """Read a pipe to EOF, keeping at most ``cap`` bytes: a helper (or a
    session holding its stderr) can write without limit and fill nothing."""
    kept = 0
    while True:
        try:
            chunk = os.read(fd, 1 << 16)
        except OSError:
            return
        if not chunk:
            return
        if kept < cap:
            keep.append(chunk[: cap - kept])
            kept += len(keep[-1])


# --- the supervisor's side ----------------------------------------------------

@dataclass
class PushState:
    blocked: str | None = None      # why pushing stopped, until it works again


class MemoryRepo:
    """The root-owned clone and the per-heteronym copies into it."""

    def __init__(self, repo: Path, live: Path, subdir: str = "memory",
                 timeout: float = HELPER_TIMEOUT):
        self.repo = repo
        self.live = live
        self.subdir = subdir
        self.timeout = timeout
        self.push_state = PushState()

    def live_dir(self, name: str) -> str:
        return str(self.live / name)

    def secure(self) -> None:
        """Make sure the clone is the supervisor's own, mode 700.

        Its ``.git`` holds every heteronym's memory, and a plain ``git
        clone`` on the host makes it 755 with 644 objects (the image's own
        chmod is hidden by the bind mount), which would let one heteronym
        read every other's. Owned by anyone else, or a symlink: refused.
        """
        try:
            fd = os.open(self.repo, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise MemoryError_(f"memory.repo {self.repo}: not a plain directory "
                               f"({exc.strerror})") from None
        try:
            st = os.fstat(fd)
            if st.st_uid != os.geteuid():
                raise MemoryError_(f"memory.repo {self.repo} belongs to uid {st.st_uid}; "
                                   "it must be the supervisor's own (root)")
            if st.st_mode & 0o077:
                os.fchmod(fd, 0o700)
        finally:
            os.close(fd)

    def _memory_root(self) -> Path:
        """``<repo>/memory``, a real directory, never a symlink."""
        self.secure()
        root = self.repo / self.subdir
        if root.is_symlink():
            raise MemoryError_(f"{root} is a symlink; refusing to use it")
        root.mkdir(exist_ok=True)
        return root

    def _helper(self, ident, verb: str, folder: str) -> list[str]:
        # -P: never put the working directory on sys.path, so a module in
        # whatever directory the helper starts in cannot replace ours.
        return [*ident.as_user(), sys.executable, "-P", "-m", "relayer.memory", verb, folder]

    @staticmethod
    def _helper_env() -> dict[str, str]:
        # The helper runs as the heteronym, whose session could attach to a
        # same-uid process and read its environment: give it nothing of the
        # supervisor's (git credentials, for one).
        return {k: os.environ[k] for k in ("PATH", "PYTHONPATH", "LANG") if k in os.environ}

    def _run_helper(self, argv: list[str], kill, feed=None,
                    drain=None) -> tuple[int, str]:
        """Run a helper with one deadline for everything.

        ``drain(reader)`` or ``feed(writer)`` runs in this thread on pipe ends
        that give up at the deadline (a held descriptor copy can withhold
        EOF forever); stderr is drained by a thread that keeps at most 64 KB.
        The helper is killed when the deadline passes with ``kill(pids)``
        (``Identity.kill``): root here holds no CAP_KILL, so ``proc.kill()``
        would fail, and a kill run as the heteronym could be stopped by it.
        """
        deadline = time.monotonic() + self.timeout
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE if feed else subprocess.DEVNULL,
                                stdout=subprocess.PIPE if drain else subprocess.DEVNULL,
                                stderr=subprocess.PIPE, env=self._helper_env(), cwd="/")
        kept: list[bytes] = []
        threading.Thread(target=_drain_capped, args=(proc.stderr.fileno(), kept),
                         daemon=True).start()
        try:
            if drain:
                drain(_DeadlineReader(proc.stdout.fileno(), deadline))
            else:
                try:
                    feed(_DeadlineWriter(proc.stdin.fileno(), deadline))
                except BrokenPipeError:
                    pass
                proc.stdin.close()              # EOF: the helper checks nothing follows
            code = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _kill(proc, kill)
            raise MemoryError_("memory helper timed out") from None
        except BaseException:
            _kill(proc, kill)
            raise
        finally:
            for pipe in (proc.stdin, proc.stdout):
                if pipe:
                    try:
                        pipe.close()
                    except OSError:
                        pass
        time.sleep(0.05)                        # let the stderr thread catch up
        return code, b"".join(kept).decode(errors="replace").strip()

    def collect(self, ident) -> list[str]:
        """Copy one heteronym's live folder into the clone; return notes."""
        root = self._memory_root()
        dest = root / ident.name
        staging = root / (ident.name + ".incoming")
        shutil.rmtree(staging, ignore_errors=True)
        result: dict[str, int] = {}

        def drain(stdout):
            result["count"] = receive(stdout, staging)

        try:
            code, err = self._run_helper(
                self._helper(ident, "dump", self.live_dir(ident.name)), ident.kill,
                drain=drain)
        except MemoryError_ as exc:
            raise MemoryError_(f"memory of {ident.name} not copied; the saved copy is "
                               f"unchanged: {exc}") from None
        if code != 0 or "count" not in result:
            shutil.rmtree(staging, ignore_errors=True)
            raise MemoryError_(f"dump for {ident.name} failed (exit {code}); the saved "
                               f"copy is unchanged: {err[-500:]}")
        replace(staging, dest)
        return [f"memory {ident.name}: skipped {line}" for line in err.splitlines()[:50] if line]

    def restore(self, ident) -> bool:
        """Fill an empty live folder from the clone; False if nothing to do."""
        self.secure()                    # prepare runs this before any collect
        root = self.repo / self.subdir
        src = root / ident.name
        if root.is_symlink() or src.is_symlink() or not src.is_dir():
            return False                 # nothing saved, or not a plain directory

        def feed(stdin):
            dump(src, stdin)

        code, err = self._run_helper(
            self._helper(ident, "load-if-empty", self.live_dir(ident.name)), ident.kill,
            feed=feed)
        if code != 0:
            raise MemoryError_(f"restore for {ident.name} failed (exit {code}): {err[-500:]}")
        return True

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(["git", *_GIT_SAFE, "-C", str(self.repo), *args],
                              capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise MemoryError_(f"git {args[0]}: {proc.stderr.strip()}")
        return proc

    def commit(self, names: list[str], when: str) -> tuple[list[str], list[str]]:
        """Stage each heteronym's folder on its own and commit.

        Returns (committed names, problems). One heteronym's folder that
        git refuses never stops the others'.
        """
        if not (self.repo / self.subdir).is_dir():
            return [], []
        staged, problems = [], []
        for name in names:
            path = f"{self.subdir}/{name}"
            add = self._git("add", "-A", "--", path, check=False)
            if add.returncode != 0:
                self._discard(path)
                problems.append(f"memory {name}: not committed: {add.stderr.strip()}")
                continue
            raw = self._git("diff", "--cached", "--raw", "--", path).stdout
            if any(line.split()[1:2] == ["160000"] for line in raw.splitlines()):
                self._discard(path)
                problems.append(f"memory {name}: not committed: it contains a git link")
                continue
            staged.append(name)
        if not staged or self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return [], problems
        self._git("commit", "-q", "-m", f"memory: {', '.join(staged)} ({when})")
        return staged, problems

    def _discard(self, path: str) -> None:
        """Return one heteronym's folder in the clone to its last commit.

        Unstaging alone would leave the refused files in the working tree,
        and `git pull --rebase` refuses a dirty tree: one heteronym would
        block every push of its class. The live folder is untouched, so the
        next collect copies it again (and is refused again, for it alone).
        """
        self._git("reset", "-q", "--", path, check=False)
        self._git("checkout", "-q", "HEAD", "--", path, check=False)   # nothing there: fine
        self._git("clean", "-ffdxq", "--", path, check=False)

    def push(self) -> str | None:
        """Push; on rejection, rebase onto the remote and try once more.

        A failure leaves the commits in the local clone (nothing is lost)
        and is returned for the log; the next interval tries again. A
        rebase conflict is aborted and reported, never forced through.
        """
        if self._git("push", "-q", "origin", "HEAD", check=False).returncode == 0:
            self.push_state.blocked = None
            return None
        fetch = self._git("pull", "-q", "--rebase", "--no-recurse-submodules", "origin",
                          check=False)
        if fetch.returncode != 0:
            self._git("rebase", "--abort", check=False)
            self.push_state.blocked = f"rebase onto the remote failed: {fetch.stderr.strip()}"
            return self.push_state.blocked
        retry = self._git("push", "-q", "origin", "HEAD", check=False)
        if retry.returncode != 0:
            self.push_state.blocked = f"push failed: {retry.stderr.strip()}"
            return self.push_state.blocked
        self.push_state.blocked = None
        return None


def _kill(proc: subprocess.Popen, kill) -> None:
    """Kill a helper (relayer.users.kill_child with ``kill``), then reap it."""
    if proc.poll() is None:
        # Until it is reaped, the pid cannot be reused: this signals the
        # helper or nothing.
        kill_child(proc, kill)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass                      # left to the heteronym's stop


# --- the heteronym's side: `python3 -m relayer.memory <verb> <folder>` -------

def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("dump", "load-if-empty"):
        print("usage: python3 -m relayer.memory dump|load-if-empty <folder>", file=sys.stderr)
        return 2
    verb, folder = argv[0], Path(argv[1])
    if verb == "dump":
        for line in dump(folder, sys.stdout.buffer):
            print(line, file=sys.stderr)
        return 0
    if any(p.name != ".restore" for p in folder.iterdir()):
        sys.stdin.buffer.read()        # drain: the live folder already has content
        return 0
    staging = folder / ".restore"
    shutil.rmtree(staging, ignore_errors=True)
    receive(sys.stdin.buffer, staging)
    for entry in staging.iterdir():
        os.rename(entry, folder / entry.name)
    staging.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
