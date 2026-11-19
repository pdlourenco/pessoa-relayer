"""Session backends. The tmux backend runs each persona in a tmux session on
a dedicated server socket, so the runtime never touches other tmux sessions.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol, Sequence

HASH_OPTION = "@relayer_hash"


@dataclass(frozen=True)
class Running:
    name: str
    config_hash: str        # "" if the session carries none
    last_activity: int      # epoch seconds


class Backend(Protocol):
    def list(self) -> dict[str, Running]: ...
    def start(self, name: str, argv: Sequence[str], workdir: str,
              env: dict[str, str], config_hash: str) -> None: ...
    def stop(self, name: str) -> None: ...


class SessionError(RuntimeError):
    pass


class TmuxBackend:
    def __init__(self, socket: str, tmux: str = "tmux"):
        self._base = [tmux, "-L", socket]

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run([*self._base, *args], capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise SessionError(f"tmux {' '.join(args[:2])}: {proc.stderr.strip()}")
        return proc

    def list(self) -> dict[str, Running]:
        fmt = "#{session_name}\t#{" + HASH_OPTION + "}\t#{session_activity}"
        proc = self._run("list-sessions", "-F", fmt, check=False)
        if proc.returncode != 0:
            # No server on our socket means no sessions; anything else is real.
            if "no server running" in proc.stderr or "error connecting" in proc.stderr:
                return {}
            raise SessionError(f"tmux list-sessions: {proc.stderr.strip()}")
        running = {}
        for line in proc.stdout.splitlines():
            name, config_hash, activity = line.split("\t")
            running[name] = Running(name, config_hash, int(activity or 0))
        return running

    def start(self, name, argv, workdir, env, config_hash):
        env_args = [arg for k, v in sorted(env.items()) for arg in ("-e", f"{k}={v}")]
        # One tmux invocation: create the session, then tag it with its hash.
        # The chained set-option has no -t: it applies to the session just
        # created. (tmux 3.4 fails "-t =name" inside the chain, though the
        # session exists; tests/test_tmux.py covers this against real tmux.)
        self._run("new-session", "-d", "-s", name, "-c", workdir, *env_args,
                  "--", *argv, ";", "set-option", HASH_OPTION, config_hash)

    def stop(self, name):
        self._run("kill-session", "-t", f"={name}")
