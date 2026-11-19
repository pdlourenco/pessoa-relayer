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

import json
import os
import pwd
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

USER_PREFIX = "h-"          # keeps heteronym users clear of system names
DEFAULT_UID_BASE = 20000
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,29}$")   # + prefix stays within 32


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

    def uid(self, name: str) -> int:
        """The uid for ``name``, assigning the next free one if new."""
        if not _NAME_RE.match(name):
            raise AccountError(f"not a heteronym name usable as a user: {name!r}")
        assigned = self.load()
        if name in assigned:
            return assigned[name]
        uid = max([self.base - 1, *assigned.values()]) + 1
        assigned[name] = uid
        tmp = self.path.with_suffix(".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(assigned, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.path)       # atomic: a crash never loses the map
        return uid

    def names(self) -> list[str]:
        return sorted(self.load())


class Accounts:
    """Creates users and their folders; needs root with CHOWN/SETUID/SETGID."""

    def __init__(self, uids: UidMap, homes_root: str):
        self.uids = uids
        self.homes_root = homes_root

    def identity(self, name: str) -> Identity:
        uid = self.uids.uid(name)
        return Identity(name, USER_PREFIX + name, uid, uid,
                        os.path.join(self.homes_root, name))

    def known(self) -> list[Identity]:
        return [self.identity(n) for n in self.uids.names()]

    def ensure_user(self, ident: Identity) -> None:
        try:
            existing = pwd.getpwnam(ident.user)
        except KeyError:
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

        Components under a root-owned parent are created by root and
        handed over; components under the heteronym's own folders are
        created as the heteronym, since root here cannot write there.
        A component owned by anyone else is refused.
        """
        parts = Path(path).parts
        current = Path(parts[0])
        for part in parts[1:]:
            parent, current = current, current / part
            try:
                st = os.lstat(current)
            except FileNotFoundError:
                st = None
            except PermissionError:
                # Inside a folder only its owner can enter: carry on as them.
                st = "inaccessible"
            if st is None:
                owner = os.lstat(parent).st_uid
                if owner == 0:
                    os.mkdir(current, 0o700)
                    os.chmod(current, 0o700)        # umask-proof
                    os.chown(current, ident.uid, ident.gid)
                elif owner == ident.uid:
                    _run([*ident.as_user(), "mkdir", "-m", "700", "--", str(current)])
                else:
                    raise AccountError(f"{parent} belongs to uid {owner}, not "
                                       f"{ident.user}; refusing to create {current}")
                continue
            if st == "inaccessible":
                _run([*ident.as_user(), "mkdir", "-p", "-m", "700", "--", str(current)])
                if current == Path(path):
                    # chmod fails unless the heteronym owns it: that is the check.
                    _run([*ident.as_user(), "chmod", "700", "--", str(current)])
                continue
            if stat.S_ISLNK(st.st_mode):
                raise AccountError(f"{current} is a symlink; refusing to follow it")
            if not stat.S_ISDIR(st.st_mode):
                raise AccountError(f"{current} exists and is not a directory")
            if current == Path(path) or st.st_uid == ident.uid:
                self._own(ident, current, st)

    def _own(self, ident: Identity, path: Path, st: os.stat_result) -> None:
        if st.st_uid == 0:
            os.chmod(path, 0o700)                   # still root's: chmod, then hand over
            os.chown(path, ident.uid, ident.gid)
        elif st.st_uid != ident.uid:
            raise AccountError(f"{path} belongs to uid {st.st_uid}, not {ident.user}")
        elif stat.S_IMODE(st.st_mode) != 0o700:
            _run([*ident.as_user(), "chmod", "700", "--", str(path)])

    def prepare(self, name: str, folders: tuple[str, ...]) -> Identity:
        ident = self.identity(name)
        self.ensure_user(ident)
        self.ensure_dir(ident, ident.home)
        for folder in folders:
            self.ensure_dir(ident, folder)
        return ident


def _run(argv: list[str]) -> None:
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AccountError(f"{argv[0]} failed: {proc.stderr.strip()}")
