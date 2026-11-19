"""Supervisor ticks with a fake backend and a fake clock (docs/SPEC.md §5)."""

import dataclasses
import tempfile
import unittest
from pathlib import Path

from relayer.config import ConfigError
from relayer.runtime import Runtime
from relayer.sessions import Running
from relayer.supervise import MAX_BACKOFF, ParkStore, Supervisor

from helpers import persona

RT = Runtime(models={"fast": "f", "standard": "s", "strong": "o", "frontier": "x"},
             command=("agent",))
ENV = {"PATH": "/bin"}
T0 = 1_800_000_000   # test clock origin; 0 would read as "no activity recorded"


class Backend:
    """Fake tmux: start adds a session, stop removes it, ``die`` kills one."""

    def __init__(self):
        self.sessions = {}
        self.now = 0.0
        self.log = []

    def list(self):
        return dict(self.sessions)

    def start(self, name, command, workdir, config_hash):
        self.log.append(("start", name))
        self.sessions[name] = Running(name, config_hash, int(self.now))

    def stop(self, name):
        self.log.append(("stop", name))
        self.sessions.pop(name, None)

    def touch(self, name, t):
        s = self.sessions[name]
        self.sessions[name] = dataclasses.replace(s, last_activity=int(T0 + t))


class Tick(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.personas = {"pa": persona(folder=str(self.root / "pa"))}
        self.backend = Backend()
        self.parks = ParkStore(self.root / "parked")
        self.fail_load = False
        self.sup = Supervisor(self.backend, self.load, self.parks, 30, ENV)

    def load(self):
        if self.fail_load:
            raise ConfigError(["personas.yaml: broken"])
        return RT, dict(self.personas)

    def tick(self, t):
        self.backend.now = T0 + t
        return self.sup.tick(T0 + t)

    def test_starts_then_idles(self):
        self.assertIn("start pa: not running", self.tick(0))
        self.assertEqual(self.tick(30), [])

    def test_bad_config_keeps_sessions(self):
        self.tick(0)
        self.fail_load = True
        log = self.tick(30)
        self.assertIn("config: keeping the last valid configuration", log)
        self.assertIn("pa", self.backend.sessions)
        self.assertNotIn(("stop", "pa"), self.backend.log)

    def test_bad_first_config_does_nothing(self):
        self.fail_load = True
        log = self.tick(0)
        self.assertIn("config: no valid configuration yet; doing nothing", log)
        self.assertEqual(self.backend.log, [])

    def test_idle_stop_parks_until_woken(self):
        self.personas["pa"] = dataclasses.replace(self.personas["pa"], idle_stop=600)
        self.tick(0)
        self.backend.touch("pa", 100)
        self.assertEqual(self.tick(650), [])            # 550s idle: under the limit
        log = self.tick(700)                            # 600s idle
        self.assertTrue(any(l.startswith("stop pa: idle") for l in log))
        self.assertEqual(self.parks.names(), {"pa"})
        self.assertEqual(self.tick(730), [])            # parked: not restarted
        self.assertTrue(self.parks.wake("pa"))
        self.assertIn("start pa: not running", self.tick(760))

    def test_idle_stop_is_not_a_crash(self):
        self.personas["pa"] = dataclasses.replace(self.personas["pa"], idle_stop=60)
        self.tick(0)
        self.tick(60)                                   # idle-stopped at 60s
        self.parks.wake("pa")
        self.assertIn("start pa: not running", self.tick(61))

    def test_park_cleared_when_persona_removed(self):
        self.parks.park("pa")
        self.personas = {}
        self.tick(0)
        self.assertEqual(self.parks.names(), set())

    def test_crash_loop_backs_off_and_caps(self):
        waits = []
        t = 0.0
        self.tick(t)
        for _ in range(8):
            self.backend.sessions.clear()               # dies straight away
            t += 1
            starts = len(self.backend.log)
            log = self.tick(t)
            wait = next(float(l.rsplit(" ", 1)[1][:-1]) for l in log if l.startswith("crash pa"))
            waits.append(wait)
            self.assertEqual(len(self.backend.log), starts)   # held, not restarted
            t += wait
            self.assertIn("start pa: not running", self.tick(t))
        self.assertEqual(waits[:3], [60, 120, 240])
        self.assertEqual(waits[-1], MAX_BACKOFF)

    def test_held_start_is_logged(self):
        self.tick(0)
        self.backend.sessions.clear()
        self.tick(1)
        self.assertTrue(any(l.startswith("hold pa") for l in self.tick(31)))

    def test_stable_session_clears_crash_count(self):
        self.tick(0)
        self.backend.sessions.clear()
        self.tick(1)                                    # crash 1, wait 60
        self.tick(61)                                   # restarted at 61
        self.tick(61 + 300)                             # stable for 300s
        self.tick(61 + 330)                             # it dies later: not a crash
        self.backend.sessions.clear()
        self.tick(61 + 331)                             # restarted (no back-off)
        self.backend.sessions.clear()                   # now it crashes again
        log = self.tick(61 + 332)
        self.assertIn("crash pa: exited after 1s; next start in 60s", log)

    def test_wake_rejects_bad_names(self):
        with self.assertRaises(ValueError):
            self.parks.wake("../etc")


if __name__ == "__main__":
    unittest.main()
