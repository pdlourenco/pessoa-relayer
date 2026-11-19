"""The session supervisor (docs/SPEC.md §5).

``relayer supervise`` runs forever: every tick it reloads the config,
reconciles, stops sessions idle past their heteronym's ``idle_stop`` (parking
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

from .config import ConfigError, Heteronym
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
MAX_EXPONENT = 10        # 2**10 already exceeds MAX_BACKOFF for any interval >= 1s

Loader = Callable[[], tuple[Runtime, dict[str, Heteronym]]]


def check_state_dir(root: Path) -> str | None:
    """Create ``root/parked`` if needed; return a problem, or None."""
    parked = root / "parked"
    try:
        parked.mkdir(parents=True, exist_ok=True)
        probe = parked / ".write-test"
        probe.touch()
        probe.unlink()
    except OSError as exc:
        return f"state directory {root}: {exc}"
    return None


class ParkStore:
    """Heteronyms stopped for idleness, one marker file each under ``root``.

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
            raise ValueError(f"not a heteronym name: {name!r}")
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
        self.config: tuple[Runtime, dict[str, Heteronym]] | None = None
        self.last_memory_sync = float("-inf")
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
        runtime, heteronyms = self.config

        try:
            running = self.backend.list()
        except SessionError as exc:
            return log + [f"sessions: {exc}"]
        log += getattr(self.backend, "errors", [])

        # Crash accounting: a session we started that is already gone.
        for name, crash in self.crashes.items():
            if crash.started is not None and name not in running:
                if now - crash.started < MIN_UPTIME:
                    crash.count += 1
                    wait = min(self.interval * 2 ** min(crash.count, MAX_EXPONENT),
                               MAX_BACKOFF)
                    crash.not_before = now + wait
                    log.append(f"crash {name}: exited after {now - crash.started:.0f}s; "
                               f"next start in {wait:.0f}s")
                crash.started = None
            elif crash.started is not None and now - crash.started >= STABLE_UPTIME:
                crash.count = 0

        parked = self.parks.names()
        # Forget parks for heteronyms that no longer exist or are disabled.
        for name in sorted(parked):
            if name not in heteronyms or not heteronyms[name].enabled:
                self.parks.wake(name)
                parked.discard(name)

        # Idle stop: running past idle_stop since the last activity.
        idle: list[Action] = []
        for name, session in running.items():
            p = heteronyms.get(name)
            if (p and p.enabled and p.idle_stop is not None and session.last_activity
                    and now - session.last_activity >= p.idle_stop):
                try:
                    self.parks.park(name)   # park first: a stop without a park restarts
                except OSError as exc:
                    log.append(f"park {name}: {exc}; not stopping it")
                    continue
                idle.append(Action("stop", name, f"idle for {now - session.last_activity:.0f}s"))
                parked.add(name)

        # Parked heteronyms count as disabled, so plan() neither starts them nor
        # leaves them running.
        effective = {n: dataclasses.replace(p, enabled=False) if n in parked else p
                     for n, p in heteronyms.items()}
        actions = plan(effective, running, runtime.session_hash)
        actions = [a for a in actions if a.name not in {i.name for i in idle}] + idle

        # Back-off: hold starts of a heteronym that keeps crashing.
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
        log += self._sync_memory(runtime, heteronyms, now)
        for a in actions:
            if a.kind in ("start", "restart"):
                self.crashes.setdefault(a.name, _Crash()).started = now
            elif a.kind == "stop":
                self.crashes.pop(a.name, None)
        return log


    def _sync_memory(self, runtime: Runtime, heteronyms, now: float) -> list[str]:
        """Copy every heteronym's memory into the clone, commit, push (SPEC §7)."""
        memory = getattr(self.backend, "memory", None)
        if memory is None or now - self.last_memory_sync < runtime.memory_interval:
            return []
        self.last_memory_sync = now
        log: list[str] = []
        names = []
        for ident in self.backend.accounts.known():
            if ident.name not in heteronyms:
                continue                       # another class's, or removed
            if not Path(memory.live_dir(ident.name)).exists():
                continue                       # never started here yet
            try:
                log += memory.collect(ident)
                names.append(ident.name)
            except Exception as exc:  # noqa: BLE001 - one heteronym must not stop the rest
                log.append(f"memory {ident.name}: {exc}")
        try:
            committed, problems = memory.commit(
                names, time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))) \
                if names else ([], [])
            log += problems
            if committed:
                log.append(f"memory: committed {', '.join(committed)}")
            if memory.push_state.blocked or names:
                problem = memory.push()
                if problem:
                    log.append(f"memory: not pushed, kept locally: {problem}")
        except Exception as exc:  # noqa: BLE001
            log.append(f"memory: {exc}")
        return log


def run(supervisor: Supervisor, interval: float) -> int:
    """Tick until SIGTERM or SIGINT; an exception in one pass is logged and
    the loop goes on. The supervisor does not stop sessions on exit (SPEC §5
    says when they survive it)."""
    stopping = False

    def _stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stopping:
        try:
            lines = supervisor.tick(time.time())
        except Exception as exc:  # noqa: BLE001 - the loop must outlive one bad pass
            lines = [f"tick failed: {type(exc).__name__}: {exc}"]
        for line in lines:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            print(f"{stamp} {line}", flush=True)
        deadline = time.monotonic() + interval
        while not stopping and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
    print("supervisor: stopping", file=sys.stderr, flush=True)
    return 0
