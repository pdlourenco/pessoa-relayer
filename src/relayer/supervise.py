"""The session supervisor (docs/SPEC.md §5).

``relayer supervise`` runs forever: every tick it reloads the config,
reconciles, stops sessions idle past their persona's ``idle_stop`` (parking
them until woken), and backs off sessions that keep dying right after start.

``Supervisor.tick`` holds all the logic and takes the clock, backend and
loader as arguments, so tests drive it tick by tick without tmux or sleep.
"""

from __future__ import annotations

import dataclasses
import re
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import ConfigError, Persona
from .reconcile import Action, apply, plan
from .runtime import Runtime
from .sessions import Backend, SessionError

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

# A session gone within MIN_UPTIME of its start counts as a crash. Restarts
# then wait interval * 2**crashes, capped at MAX_BACKOFF; a session that
# stays up for STABLE_UPTIME clears its count.
MIN_UPTIME = 60
STABLE_UPTIME = 300
MAX_BACKOFF = 900

Loader = Callable[[], tuple[Runtime, dict[str, Persona]]]


class ParkStore:
    """Personas stopped for idleness, one marker file each under ``root``.

    A marker survives supervisor restarts. ``relayer wake <name>`` (or a
    channel, later) removes it, and the next tick starts the session.
    """

    def __init__(self, root: Path):
        self.root = root

    def names(self) -> set[str]:
        if not self.root.is_dir():
            return set()
        return {p.name for p in self.root.iterdir() if NAME_RE.match(p.name)}

    def park(self, name: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / name).touch()

    def wake(self, name: str) -> bool:
        if not NAME_RE.match(name):
            raise ValueError(f"not a persona name: {name!r}")
        try:
            (self.root / name).unlink()
            return True
        except FileNotFoundError:
            return False


@dataclass
class _Crash:
    started: float | None = None   # None: not started by us since last check
    count: int = 0
    not_before: float = 0.0


class Supervisor:
    def __init__(self, backend: Backend, loader: Loader, parks: ParkStore,
                 interval: float, environ: dict[str, str]):
        self.backend = backend
        self.loader = loader
        self.parks = parks
        self.interval = interval
        self.environ = environ
        self.config: tuple[Runtime, dict[str, Persona]] | None = None
        self.crashes: dict[str, _Crash] = {}

    def tick(self, now: float) -> list[str]:
        """One reconcile pass; returns log lines."""
        log: list[str] = []

        try:
            self.config = self.loader()
        except ConfigError as exc:
            # Keep the last good config: a bad edit must not stop everything.
            log += [f"config error: {e}" for e in exc.errors]
            log.append("config: keeping the last valid configuration"
                       if self.config else "config: no valid configuration yet; doing nothing")
        if self.config is None:
            return log
        runtime, personas = self.config

        try:
            running = self.backend.list()
        except SessionError as exc:
            return log + [f"sessions: {exc}"]

        # Crash accounting: a session we started that is already gone.
        for name, crash in self.crashes.items():
            if crash.started is not None and name not in running:
                if now - crash.started < MIN_UPTIME:
                    crash.count += 1
                    wait = min(self.interval * 2 ** crash.count, MAX_BACKOFF)
                    crash.not_before = now + wait
                    log.append(f"crash {name}: exited after {now - crash.started:.0f}s; "
                               f"next start in {wait:.0f}s")
                crash.started = None
            elif crash.started is not None and now - crash.started >= STABLE_UPTIME:
                crash.count = 0

        parked = self.parks.names()
        # Forget parks for personas that no longer exist or are disabled.
        for name in sorted(parked):
            if name not in personas or not personas[name].enabled:
                self.parks.wake(name)
                parked.discard(name)

        # Idle stop: running past idle_stop since the last activity.
        idle: list[Action] = []
        for name, session in running.items():
            p = personas.get(name)
            if (p and p.enabled and p.idle_stop is not None and session.last_activity
                    and now - session.last_activity >= p.idle_stop):
                idle.append(Action("stop", name, f"idle for {now - session.last_activity:.0f}s"))
                self.parks.park(name)
                parked.add(name)

        # Parked personas count as disabled, so plan() neither starts them nor
        # leaves them running.
        effective = {n: dataclasses.replace(p, enabled=False) if n in parked else p
                     for n, p in personas.items()}
        actions = plan(effective, running, runtime.session_hash)
        actions = [a for a in actions if a.name not in {i.name for i in idle}] + idle

        # Back-off: hold starts of a persona that keeps crashing.
        held = []
        for a in actions:
            crash = self.crashes.get(a.name)
            if a.kind == "start" and crash and now < crash.not_before:
                held.append(a)
                log.append(f"hold {a.name}: backing off for {crash.not_before - now:.0f}s")
        actions = [a for a in actions if a not in held]

        for a in actions:
            log.append(str(a))
        log += apply(actions, effective, self.backend, runtime, self.environ)
        for a in actions:
            if a.kind in ("start", "restart"):
                self.crashes.setdefault(a.name, _Crash()).started = now
            elif a.kind == "stop":
                self.crashes.pop(a.name, None)
        return log


def run(supervisor: Supervisor, interval: float) -> int:
    """Tick until SIGTERM or SIGINT. Sessions are left running on exit, so a
    supervisor restart does not restart every persona."""
    stopping = False

    def _stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stopping:
        for line in supervisor.tick(time.time()):
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            print(f"{stamp} {line}", flush=True)
        deadline = time.monotonic() + interval
        while not stopping and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
    print("supervisor: stopping; sessions left running", file=sys.stderr, flush=True)
    return 0
