"""Session backends. The tmux backend runs each heteronym in a tmux session on
a dedicated server socket, so the runtime never touches other tmux sessions.

Hardening (docs/SPEC.md §4.2): tmux runs with ``-f /dev/null`` (no user
config) and a minimal environment; the session command is one shell-quoted
string that carries its own environment through ``env -i``, so a session
inherits nothing from whichever process started the tmux server.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass

from .users import processes_of
from typing import Protocol

HASH_OPTION = "@relayer_hash"
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_HASH_RE = re.compile(r"^[0-9a-f]{1,64}$")


@dataclass(frozen=True)
class Running:
    name: str
    config_hash: str        # "" if the session carries none
    last_activity: int      # epoch seconds of the last pane output; 0 = unknown


class Backend(Protocol):
    def list(self) -> dict[str, Running]: ...
    def start(self, name: str, command: str, workdir: str, config_hash: str) -> None: ...
    def stop(self, name: str) -> None: ...


class SessionError(RuntimeError):
    pass


class TmuxBackend:
    def __init__(self, socket: str, tmux: str = "tmux", prefix: list[str] | None = None,
                 home: str | None = None):
        # ``prefix`` runs tmux as another user (setpriv), so that user owns
        # the tmux server and every session in it (docs/SPEC.md §6).
        self._base = [*(prefix or []), tmux, "-L", socket, "-f", "/dev/null"]
        # A tmux server inherits the environment of the client that starts
        # it; give it nothing worth inheriting.
        self._env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
        if home is not None:
            self._env["HOME"] = home
            # The socket lives under the heteronym's own 700 home, not
            # /tmp/tmux-<uid>, which another heteronym could create first
            # (uids are predictable) and so block this one's server.
            self._env["TMUX_TMPDIR"] = home

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run([*self._base, *args], capture_output=True, text=True,
                              env=self._env)
        if proc.returncode != 0 and "server exited unexpectedly" in proc.stderr:
            # The server was shutting down (its last session had just ended)
            # when we connected. Once it is gone the next call starts a new
            # one, so retry once.
            time.sleep(0.2)
            proc = subprocess.run([*self._base, *args], capture_output=True, text=True,
                                  env=self._env)
        if check and proc.returncode != 0:
            raise SessionError(f"tmux {' '.join(args[:2])}: {proc.stderr.strip()}")
        return proc

    def list(self, only: str | None = None) -> dict[str, Running]:
        """Sessions on this server. With ``only``, every other line is
        ignored, malformed ones included: on a heteronym's own server the
        heteronym may have made sessions of its own (docs/SPEC.md §6.3)."""
        # "|" not tab: with no locale in its environment, tmux prints a tab
        # as "_". Names, hashes and timestamps cannot contain "|".
        # window_activity, not session_activity: in tmux 3.4 the latter only
        # moves with client input, so a detached session never updates it.
        fmt = "#{session_name}|#{" + HASH_OPTION + "}|#{window_activity}"
        proc = self._run("list-sessions", "-F", fmt, check=False)
        if proc.returncode != 0:
            # No server on our socket means no sessions; anything else is real.
            if any(s in proc.stderr for s in ("no server running", "error connecting",
                                              "server exited unexpectedly")):
                return {}
            raise SessionError(f"tmux list-sessions: {proc.stderr.strip()}")
        running = {}
        for line in proc.stdout.splitlines():
            parts = line.split("|")
            if only is not None and (len(parts) != 3 or parts[0] != only):
                continue
            if len(parts) != 3 or not _NAME_RE.match(parts[0]):
                raise SessionError(f"tmux list-sessions: unexpected line {line!r}")
            name, config_hash, activity = parts
            running[name] = Running(name, config_hash,
                                    int(activity) if activity.isdigit() else 0)
        return running

    def start(self, name, command, workdir, config_hash):
        # Defence in depth: the config layer already restricts these.
        if not _NAME_RE.match(name) or not _HASH_RE.match(config_hash):
            raise SessionError(f"refusing unsafe session name or hash: {name!r}")
        if command.rstrip().endswith(";"):
            raise SessionError("refusing a command ending in ';' (tmux separator)")
        # One tmux invocation: create the session, then tag it with its hash.
        # The chained set-option has no -t: it applies to the session just
        # created (tmux 3.4 rejects "-t =name" for set-option). "#" is
        # doubled because tmux expands formats in the start directory.
        self._run("new-session", "-d", "-s", name, "-c", workdir.replace("#", "##"),
                  "--", command, ";", "set-option", HASH_OPTION, config_hash)

    def stop(self, name):
        self._run("kill-session", "-t", f"={name}")


class UserTmuxBackend:
    """One tmux server per heteronym, each owned by that heteronym's user.

    ``accounts`` is a :class:`relayer.users.Accounts`. Listing walks every
    heteronym that has ever had a uid, so the sessions of one removed from
    the definitions are still found, and stopped.
    """

    def __init__(self, socket: str, accounts, tmux: str = "tmux"):
        self.socket = socket
        self.accounts = accounts
        self.tmux = tmux
        self.errors: list[str] = []     # per-heteronym listing failures, last list()

    def _for(self, ident) -> TmuxBackend:
        return TmuxBackend(self.socket, self.tmux, prefix=ident.as_user(), home=ident.home)

    def list(self) -> dict[str, Running]:
        running: dict[str, Running] = {}
        self.errors = []
        for ident in self.accounts.known():
            # One heteronym's server failing must not stop the others'
            # reconcile: record it and carry on.
            try:
                found = self._for(ident).list(only=ident.name)
            except Exception as exc:  # noqa: BLE001 - reported via .errors
                self.errors.append(f"sessions of {ident.name}: {exc}")
                continue
            if ident.name in found:
                running[ident.name] = found[ident.name]
            elif processes_of(ident.uid):
                # Processes but no recognisable session: a session renamed
                # or mis-tagged by the heteronym, or a daemon it left. List
                # it untagged, so plan() stops or restarts it; it must not be
                # able to hide from a stop by hiding its session.
                running[ident.name] = Running(ident.name, "", 0)
        return running

    def prepare(self, heteronym) -> None:
        self.accounts.prepare(heteronym.name, heteronym.folders)

    def identity(self, name: str) -> dict[str, str]:
        return self.accounts.identity(name).env()

    def start(self, name, command, workdir, config_hash):
        self._for(self.accounts.identity(name)).start(name, command, workdir, config_hash)

    def stop(self, name):
        ident = self.accounts.identity(name)
        try:
            self._for(ident).stop(name)
        except SessionError:
            pass                          # already gone, or hidden: the kill below covers it
        # kill-session ends the tmux session, not a daemon the heteronym
        # started with setsid; end everything its uid runs (docs/SPEC.md §6.3).
        subprocess.run([*ident.as_user(), "kill", "-KILL", "-1"], capture_output=True)
        deadline = time.time() + 5
        while processes_of(ident.uid) and time.time() < deadline:
            time.sleep(0.05)
        if processes_of(ident.uid):
            raise SessionError(f"processes of {ident.user} survived the stop")

