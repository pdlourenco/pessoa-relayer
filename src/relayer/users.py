"""One Unix user per heteronym (docs/SPEC.md §6).

The supervisor runs as root inside its container with only CAP_CHOWN,
CAP_SETUID and CAP_SETGID. With those it creates each heteronym's user,
gives it its folders (mode 700), and starts its session as that user. It
holds no DAC_OVERRIDE, so it cannot read inside a heteronym's folders
itself; anything it must do inside one, it does as that user.

UIDs come from a map kept in the state directory, so a recreated container
(whose /etc/passwd starts empty) gives every heteronym its old uid back and
its folders stay its own. A uid is never reused for another name.
"""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import re
import select
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

USER_PREFIX = "h-"          # keeps heteronym users clear of system names
DEFAULT_UID_BASE = 20000
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,29}$")   # + prefix stays within 32
MAX_OWNER_SCAN = 100_000    # entries walked when rebuilding; beyond it, refuse
AS_USER_TIMEOUT = 30.0      # seconds for a command run as a heteronym


class AccountError(RuntimeError):
    pass


@dataclass(frozen=True)
class Identity:
    name: str               # heteronym name
    user: str               # Unix user name
    uid: int
    gid: int
    home: str

    def env(self) -> dict[str, str]:
        return {"HOME": self.home, "USER": self.user, "LOGNAME": self.user}

    def as_user(self) -> list[str]:
        """Command prefix that runs the rest as this user, with no capabilities."""
        return ["setpriv", f"--reuid={self.uid}", f"--regid={self.gid}",
                "--clear-groups", "--"]

    def kill(self, pids) -> None:
        """SIGKILL ``pids`` of this user; see :func:`kill_as`."""
        kill_as(self.uid, pids)


class UidMap:
    """Persistent heteronym → uid assignments under the state directory."""

    def __init__(self, path: Path, base: int = DEFAULT_UID_BASE):
        self.path = path
        self.base = base

    def load(self) -> dict[str, int]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise AccountError(f"{self.path}: unreadable uid map: {exc}") from None
        if not isinstance(data, dict) or not all(
                isinstance(k, str) and type(v) is int for k, v in data.items()):
            raise AccountError(f"{self.path}: uid map must map names to integers")
        return data

    def uid(self, name: str, owner_hint: int | None = None,
            taken: frozenset[int] | Callable[[], set[int]] = frozenset()) -> int:
        """The uid for ``name``, assigning one if new.

        ``owner_hint`` is the uid that already owns the heteronym's home,
        if any: a lost or reset map is rebuilt from the files rather than
        handing those files to a stranger. ``taken`` holds uids that own
        anything under the folder roots, or is a function returning them,
        called only when a uid must be assigned; none of them is ever handed
        to a new name (docs/SPEC.md §6.1).
        """
        if not _NAME_RE.match(name):
            raise AccountError(f"not a heteronym name usable as a user: {name!r}")
        if not self.path.parent.is_dir():       # the root-only state directory
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)      # login and supervise share the map
            assigned = self.load()
            if name in assigned:
                return assigned[name]
            used = set(assigned.values()) | set(taken() if callable(taken) else taken)
            if owner_hint is not None and owner_hint >= self.base and \
                    owner_hint not in set(assigned.values()):
                uid = owner_hint
            else:
                uid = max([self.base - 1, *used]) + 1
            assigned[name] = uid
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(assigned, indent=2, sort_keys=True) + "\n")
            os.replace(tmp, self.path)   # atomic: a crash never loses the map
            return uid

    def names(self) -> list[str]:
        return sorted(self.load())


class Accounts:
    """Creates users and their folders; needs root with CHOWN/SETUID/SETGID."""

    def __init__(self, uids: UidMap, homes_root: str, work_root: str = "/work",
                 other_roots: tuple[str, ...] = ()):
        self.uids = uids
        self.homes_root = homes_root
        self.work_root = work_root
        # Further roots under which heteronyms own folders (the memory live
        # root): their owners count as taken too.
        self.other_roots = tuple(other_roots)

    def _owners(self) -> set[int]:
        """Every non-root uid owning an entry reachable from a folder root
        through root-owned directories.

        A defined folder can sit below parents root made on the way
        (``/work/x/notes``), so the top level is not enough. Only root-owned
        directories are entered: a heteronym cannot add entries to those, so
        the walk is root's own tree and nothing a heteronym plants can
        lengthen or redirect it. Symlinks are not followed. A walk that
        cannot be completed refuses, rather than risk a uid that owns files.
        """
        owners: set[int] = set()
        seen = 0
        stack = [self.homes_root, self.work_root, *self.other_roots]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for entry in it:
                        seen += 1
                        if seen > MAX_OWNER_SCAN:
                            raise AccountError(
                                f"more than {MAX_OWNER_SCAN} entries under the folder "
                                "roots; refusing to assign a new uid without a full scan")
                        st = entry.stat(follow_symlinks=False)
                        if st.st_uid:
                            owners.add(st.st_uid)
                        elif stat.S_ISDIR(st.st_mode):
                            stack.append(entry.path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise AccountError(f"{d}: cannot scan for file owners ({exc.strerror}); "
                                   "refusing to assign a new uid") from None
        return owners

    def identity(self, name: str) -> Identity:
        home = os.path.join(self.homes_root, name)
        try:
            st = os.lstat(home)
            hint = st.st_uid if stat.S_ISDIR(st.st_mode) and st.st_uid else None
        except FileNotFoundError:
            hint = None
        uid = self.uids.uid(name, owner_hint=hint, taken=self._owners)
        return Identity(name, USER_PREFIX + name, uid, uid, home)

    def known(self) -> list[Identity]:
        return [self.identity(n) for n in self.uids.names()]

    def ensure_user(self, ident: Identity) -> None:
        try:
            existing = pwd.getpwnam(ident.user)
        except KeyError:
            try:
                taken = pwd.getpwuid(ident.uid).pw_name
            except KeyError:
                taken = None
            if taken is not None:
                raise AccountError(f"uid {ident.uid} already belongs to {taken}")
            _run(["groupadd", "--gid", str(ident.gid), ident.user])
            _run(["useradd", "--uid", str(ident.uid), "--gid", str(ident.gid),
                  "--no-create-home", "--home-dir", ident.home,
                  "--shell", "/bin/bash", "-l", ident.user])
            return
        if existing.pw_uid != ident.uid:
            raise AccountError(f"user {ident.user} exists with uid {existing.pw_uid}, "
                               f"expected {ident.uid}")

    def ensure_dir(self, ident: Identity, path: str) -> None:
        """Make ``path`` exist, owned by the heteronym, mode 700.

        Root walks down from "/" by file descriptor, never following a
        symlink (O_NOFOLLOW), so nothing a heteronym does to paths can
        redirect a root-side mkdir, chmod or chown. Root acts only on
        root-owned components; from the first component the heteronym owns,
        the rest is done as the heteronym, with nothing it could not do
        itself. A component owned by anyone else is refused.
        """
        parts = Path(path).parts
        if not parts or parts[0] != "/" or ".." in parts:
            raise AccountError(f"{path!r} is not an absolute, normalised path")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open("/", flags)
        try:
            for i, part in enumerate(parts[1:], start=1):
                last = i == len(parts) - 1
                created = False
                try:
                    child = os.open(part, flags, dir_fd=fd)
                except FileNotFoundError:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    created = True
                    child = os.open(part, flags, dir_fd=fd)
                except PermissionError:
                    # A folder root may not read: someone else's 700 folder.
                    # O_PATH needs no read permission; it is enough to check
                    # whose it is before acting as that user.
                    probe = os.open(part, os.O_PATH | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        pst = os.fstat(probe)
                    finally:
                        os.close(probe)
                    here = os.path.join(*parts[:i + 1])
                    if not stat.S_ISDIR(pst.st_mode):
                        raise AccountError(f"{here}: not a plain directory") from None
                    if pst.st_uid != ident.uid:
                        raise AccountError(f"{here} belongs to uid {pst.st_uid}, "
                                           f"not {ident.user}") from None
                    self._as_user_rest(ident, path)
                    return
                except OSError as exc:
                    raise AccountError(f"{os.path.join(*parts[:i + 1])}: not a plain "
                                       f"directory (symlink or file): {exc.strerror}") from None
                os.close(fd)
                fd = child
                st = os.fstat(fd)
                here = os.path.join(*parts[:i + 1])
                if st.st_uid == 0:
                    if last:
                        os.fchmod(fd, 0o700)          # by descriptor: no path to swap
                        os.fchown(fd, ident.uid, ident.gid)
                        return
                    if created:
                        # A parent made on the way down stays root's and
                        # traversable: handing it over would give this
                        # heteronym its siblings' parent.
                        os.fchmod(fd, 0o755)
                    continue                          # a root-owned ancestor: go on
                if st.st_uid == ident.uid:
                    self._as_user_rest(ident, path)
                    return
                raise AccountError(f"{here} belongs to uid {st.st_uid}, not {ident.user}")
        finally:
            os.close(fd)

    def _as_user_rest(self, ident: Identity, path: str) -> None:
        # Below the heteronym's own folder, act as the heteronym. chmod fails
        # unless it owns the final folder: that is the ownership check.
        run_as(ident, ["mkdir", "-p", "-m", "700", "--", path])
        run_as(ident, ["chmod", "700", "--", path])

    def prepare(self, name: str, folders: tuple[str, ...]) -> Identity:
        ident = self.identity(name)
        self.ensure_user(ident)
        self.ensure_dir(ident, ident.home)
        for folder in folders:
            self.ensure_dir(ident, folder)
        return ident


def kill_as(uid: int, pids) -> None:
    """Send SIGKILL to those of ``pids`` that are processes of ``uid``, from a
    process the heteronym cannot touch.

    Root here holds no CAP_KILL. A `kill` command run as the heteronym would
    have the right uid, but the heteronym could stop it (SIGSTOP) before it
    signals, and the caller would wait forever. So a forked child sets only
    its *effective* uid to ``uid``, keeping real and saved uid 0: the
    heteronym, matching neither, can neither signal nor trace it.

    Its real uid 0 also lets it signal every root process, so each target
    is pinned first: a pidfd refers to one process and cannot be redirected
    to a reused pid, and the signal is sent only if that process's real uid
    is ``uid``. If the pinned process has gone by the time its uid is read,
    the pidfd signal fails: it never reaches whatever reuses the pid.
    """
    pids = [int(p) for p in pids]
    if not pids:
        return
    if os.geteuid() != 0:
        _signal_pinned(uid, pids)       # we are the user: nothing else is reachable
        return
    child = os.fork()
    if child == 0:                        # pragma: no cover - runs in the child
        try:
            os.setresuid(0, uid, 0)
            _signal_pinned(uid, pids)
        finally:
            os._exit(0)
    os.waitpid(child, 0)


def _real_uid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("Uid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _signal_pinned(uid: int, pids: list[int]) -> None:
    for pid in pids:
        try:
            fd = os.pidfd_open(pid)
        except OSError:
            continue                      # already gone
        try:
            if _real_uid(pid) == uid:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
        except OSError:
            pass
        finally:
            os.close(fd)


def run_as(ident: Identity, argv: list[str], timeout: float = AS_USER_TIMEOUT) -> None:
    """Run ``argv`` as the heteronym, bounded: a heteronym can stop or hold
    any process of its uid. Waits for the process, not for end-of-file (it
    can hold a copy of the pipe); on the deadline the process is killed with
    :func:`kill_as`. Raises AccountError on failure or timeout."""
    proc = subprocess.Popen([*ident.as_user(), *argv], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_as(ident.uid, [proc.pid])
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise AccountError(f"{argv[0]} as {ident.user}: no answer within "
                               f"{timeout:g}s") from None
        err = b""
        if select.select([proc.stderr], [], [], 0)[0]:
            os.set_blocking(proc.stderr.fileno(), False)
            try:
                err = os.read(proc.stderr.fileno(), 64 * 1024)
            except BlockingIOError:
                pass
    finally:
        proc.stderr.close()
    if code != 0:
        raise AccountError(f"{argv[0]} as {ident.user} failed: "
                           f"{err.decode(errors='replace').strip()}")


def processes_of(uid: int) -> list[int]:
    """Live PIDs whose real uid is ``uid`` (readable from /proc by anyone).

    Zombies are left out: killed but not yet reaped by their parent, they
    run nothing and hold nothing but a process-table entry.
    """
    pids = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            with open(f"/proc/{entry.name}/status") as fh:
                fields = dict(line.split(":", 1) for line in fh if ":" in line)
            if fields["State"].split()[0] in ("Z", "X"):
                continue
            if int(fields["Uid"].split()[0]) == uid:
                pids.append(int(entry.name))
        except (OSError, ValueError, KeyError, IndexError):
            continue                     # gone, or not ours to read
    return pids


def _run(argv: list[str]) -> None:
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AccountError(f"{argv[0]} failed: {proc.stderr.strip()}")
