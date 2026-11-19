"""Session backends. The tmux backend runs each persona in a tmux session on
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
from dataclasses import dataclass
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
    def __init__(self, socket: str, tmux: str = "tmux"):
        self._base = [tmux, "-L", socket, "-f", "/dev/null"]
        # A tmux server inherits the environment of the client that starts
        # it; give it nothing worth inheriting.
        self._env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run([*self._base, *args], capture_output=True, text=True,
                              env=self._env)
        if check and proc.returncode != 0:
            raise SessionError(f"tmux {' '.join(args[:2])}: {proc.stderr.strip()}")
        return proc

    def list(self) -> dict[str, Running]:
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
