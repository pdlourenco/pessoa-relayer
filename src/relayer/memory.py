"""Heteronym memory, versioned in a private git repository (docs/SPEC.md §7).

Each heteronym writes only its own live folder, ``<live>/<name>`` (mode 700,
its own user). The supervisor keeps a root-owned clone of the memory
repository and, every interval, copies each heteronym's folder into
``<repo>/memory/<name>/``, commits, and pushes.

The supervisor cannot read a heteronym's folder (no DAC_OVERRIDE, by
design), and must never extract an archive a heteronym controls as root.
So the copy runs in two halves joined by a pipe:

- the *dump* half runs as the heteronym (``python3 -m relayer.memory dump``)
  and writes a simple stream of (path, bytes) records for the regular files
  it can see, never following a symlink;
- the *load* half runs as root and writes those records into the
  root-owned clone, after checking every path.

Restoring a fresh live folder from the repository is the same pipe the
other way round, with the load half running as the heteronym.

Memory grants nothing: it is advice to a session, never a permission.
"""

from __future__ import annotations

import os
import shutil
import stat
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

MAGIC = b"RLMEM1\n"
MAX_FILE = 5 * 1024 * 1024          # one file; larger ones are skipped
MAX_TOTAL = 50 * 1024 * 1024        # one heteronym's memory
_HEADER = struct.Struct(">HQ")      # path length, data length


class MemoryError_(RuntimeError):
    pass


def _safe_rel(path: str) -> str:
    """A relative path with no '..', no absolute part, nothing hidden in .git."""
    p = PurePosixPath(path)
    if (p.is_absolute() or not p.parts or any(x in ("", ".", "..") for x in p.parts)
            or p.parts[0] == ".git" or "\0" in path or len(path.encode()) > 4096):
        raise MemoryError_(f"unsafe path in memory stream: {path!r}")
    return str(p)


# --- the stream -------------------------------------------------------------

def dump(root: Path, out: BinaryIO, max_file: int = MAX_FILE,
         max_total: int = MAX_TOTAL) -> list[str]:
    """Write every regular file under ``root``; return what was skipped."""
    skipped: list[str] = []
    total = 0
    out.write(MAGIC)
    for rel in sorted(_walk(root, root)):
        full = root / rel
        try:
            fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            skipped.append(f"{rel}: {exc.strerror}")
            continue
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                skipped.append(f"{rel}: not a regular file")
                continue
            if st.st_size > max_file or total + st.st_size > max_total:
                skipped.append(f"{rel}: too large ({st.st_size} bytes)")
                continue
            data = fh.read(max_file + 1)
        if len(data) > max_file:
            skipped.append(f"{rel}: grew past the limit")
            continue
        total += len(data)
        name = rel.encode()
        out.write(_HEADER.pack(len(name), len(data)) + name + data)
    out.flush()
    return skipped


def _walk(root: Path, top: Path) -> Iterator[str]:
    with os.scandir(top) as it:
        for entry in it:
            rel = os.path.relpath(entry.path, root)
            if entry.name == ".git":
                continue
            if entry.is_dir(follow_symlinks=False):
                yield from _walk(root, Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                yield rel


def load(stream: BinaryIO, dest: Path, max_total: int = MAX_TOTAL) -> int:
    """Replace ``dest``'s contents with the files in ``stream``; return the count.

    ``dest`` and everything created under it are fresh directories made
    here, so no path a stream names can lead outside ``dest``.
    """
    if stream.read(len(MAGIC)) != MAGIC:
        raise MemoryError_("not a memory stream")
    staging = dest.with_name(dest.name + ".incoming")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(mode=0o700)
    count = total = 0
    try:
        while True:
            head = stream.read(_HEADER.size)
            if not head:
                break
            if len(head) != _HEADER.size:
                raise MemoryError_("truncated memory stream")
            name_len, data_len = _HEADER.unpack(head)
            total += data_len
            if data_len > MAX_FILE or total > max_total:
                raise MemoryError_("memory stream exceeds the size limits")
            rel = _safe_rel(stream.read(name_len).decode("utf-8", "strict"))
            data = stream.read(data_len)
            if len(data) != data_len:
                raise MemoryError_("truncated memory stream")
            target = staging / rel
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            count += 1
        old = dest.with_name(dest.name + ".old")
        shutil.rmtree(old, ignore_errors=True)
        if dest.exists():
            os.rename(dest, old)
        os.rename(staging, dest)
        shutil.rmtree(old, ignore_errors=True)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return count


# --- the supervisor's side ----------------------------------------------------

@dataclass
class PushState:
    blocked: str | None = None      # why pushing stopped, until it works again


class MemoryRepo:
    """The root-owned clone and the per-heteronym copies into it."""

    def __init__(self, repo: Path, live: Path, subdir: str = "memory"):
        self.repo = repo
        self.live = live
        self.subdir = subdir
        self.push_state = PushState()

    def live_dir(self, name: str) -> str:
        return str(self.live / name)

    def _helper(self, ident, verb: str, folder: str) -> list[str]:
        return [*ident.as_user(), sys.executable, "-m", "relayer.memory", verb, folder]

    @staticmethod
    def _helper_env() -> dict[str, str]:
        # The helper runs as the heteronym, whose session could attach to a
        # same-uid process and read its environment: give it nothing of the
        # supervisor's (git credentials, for one).
        return {k: os.environ[k] for k in ("PATH", "PYTHONPATH", "LANG") if k in os.environ}

    def collect(self, ident) -> list[str]:
        """Copy one heteronym's live folder into the clone; return notes."""
        dest = self.repo / self.subdir / ident.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(self._helper(ident, "dump", self.live_dir(ident.name)),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=self._helper_env())
        try:
            load(proc.stdout, dest)
        finally:
            proc.stdout.close()
            err = proc.stderr.read().decode(errors="replace").strip()
            proc.stderr.close()
            code = proc.wait()
        if code != 0:
            raise MemoryError_(f"dump for {ident.name} failed: {err}")
        return [f"memory {ident.name}: skipped {line}" for line in err.splitlines() if line]

    def restore(self, ident) -> bool:
        """Fill an empty live folder from the clone; False if nothing to do."""
        src = self.repo / self.subdir / ident.name
        if not src.is_dir():
            return False
        proc = subprocess.Popen(self._helper(ident, "load-if-empty", self.live_dir(ident.name)),
                                stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=self._helper_env())
        try:
            dump(src, proc.stdin)
        finally:
            proc.stdin.close()
            err = proc.stderr.read().decode(errors="replace").strip()
            code = proc.wait()
        if code != 0:
            raise MemoryError_(f"restore for {ident.name} failed: {err}")
        return True

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(["git", "-C", str(self.repo), *args],
                              capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise MemoryError_(f"git {args[0]}: {proc.stderr.strip()}")
        return proc

    def commit(self, names: list[str], when: str) -> bool:
        """Commit memory changes; False if there were none."""
        if not (self.repo / self.subdir).is_dir():
            return False
        self._git("add", "-A", "--", self.subdir)
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        self._git("-c", "user.name=pessoa-relayer", "-c", "user.email=relayer@localhost",
                  "commit", "-q", "-m", f"memory: {', '.join(names)} ({when})")
        return True

    def push(self) -> str | None:
        """Push; on rejection, rebase onto the remote and try once more.

        A failure leaves the commits in the local clone (nothing is lost)
        and is returned for the log; the next interval tries again. A
        rebase conflict is aborted and reported, never forced through.
        """
        if self._git("push", "-q", "origin", "HEAD", check=False).returncode == 0:
            self.push_state.blocked = None
            return None
        fetch = self._git("pull", "-q", "--rebase", "origin", check=False)
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
    if any(folder.iterdir()):
        sys.stdin.buffer.read()        # drain: the live folder already has content
        return 0
    staging = folder / ".restore"
    load(sys.stdin.buffer, staging)
    for entry in staging.iterdir():
        os.rename(entry, folder / entry.name)
    staging.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
