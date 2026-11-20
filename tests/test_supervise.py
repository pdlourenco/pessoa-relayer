"""Supervisor ticks with a fake backend and a fake clock (docs/SPEC.md §5)."""

import dataclasses
import tempfile
import unittest
from pathlib import Path

from relayer.config import ConfigError
from relayer.runtime import Runtime
from relayer.sessions import Running
from relayer.supervise import (MAX_BACKOFF, MIN_UPTIME, ParkStore, Supervisor,
                               check_state_dir, run)

from helpers import heteronym

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
        self.heteronyms = {"pa": heteronym(folder=str(self.root / "pa"))}
        self.backend = Backend()
        self.parks = ParkStore(self.root / "parked")
        self.fail_load = False
        self.sup = Supervisor(self.backend, self.load, self.parks, 30, ENV)

    def load(self):
        if self.fail_load:
            raise ConfigError(["heteronyms.yaml: broken"])
        return RT, dict(self.heteronyms)

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
        self.heteronyms["pa"] = dataclasses.replace(self.heteronyms["pa"], idle_stop=600)
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
        self.heteronyms["pa"] = dataclasses.replace(self.heteronyms["pa"], idle_stop=60)
        self.tick(0)
        self.tick(60)                                   # idle-stopped at 60s
        self.parks.wake("pa")
        self.assertIn("start pa: not running", self.tick(61))

    def test_park_cleared_when_heteronym_removed(self):
        self.parks.park("pa")
        self.heteronyms = {}
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

    def test_stop_soon_after_start_is_not_a_crash(self):
        self.tick(0)
        self.heteronyms["pa"] = dataclasses.replace(self.heteronyms["pa"], enabled=False)
        self.assertIn("stop pa: disabled", self.tick(10))
        self.heteronyms["pa"] = dataclasses.replace(self.heteronyms["pa"], enabled=True)
        log = self.tick(20)
        self.assertIn("start pa: not running", log)
        self.assertFalse(any(l.startswith(("crash", "hold")) for l in log))

    def test_park_cleared_when_heteronym_disabled(self):
        self.parks.park("pa")
        self.heteronyms["pa"] = dataclasses.replace(self.heteronyms["pa"], enabled=False)
        self.tick(0)
        self.assertEqual(self.parks.names(), set())

    def test_disabled_heteronym_is_stopped_not_parked(self):
        self.heteronyms["pa"] = dataclasses.replace(self.heteronyms["pa"], idle_stop=60)
        self.tick(0)
        self.heteronyms["pa"] = dataclasses.replace(self.heteronyms["pa"], enabled=False)
        self.assertIn("stop pa: disabled", self.tick(120))
        self.assertEqual(self.parks.names(), set())

    def test_exit_at_exactly_min_uptime_is_not_a_crash(self):
        self.tick(0)
        self.backend.sessions.clear()
        log = self.tick(MIN_UPTIME)
        self.assertFalse(any(l.startswith("crash") for l in log))

    def test_restart_that_dies_counts_as_crash(self):
        self.tick(0)
        self.heteronyms["pa"] = dataclasses.replace(self.heteronyms["pa"], tier="strong")
        self.assertIn("restart pa: configuration changed", self.tick(400))
        self.backend.sessions.clear()
        self.assertTrue(any(l.startswith("crash pa") for l in self.tick(401)))

    def test_backoff_survives_huge_crash_counts(self):
        self.sup.interval = 30.0          # the CLI passes a float: 30.0 * 2**5000 overflows
        self.tick(0)
        self.sup.crashes["pa"].count = 5000
        self.backend.sessions.clear()
        log = self.tick(1)
        self.assertIn(f"crash pa: exited after 1s; next start in {MAX_BACKOFF}s", log)

    def test_park_failure_leaves_session_running(self):
        self.heteronyms["pa"] = dataclasses.replace(self.heteronyms["pa"], idle_stop=60)
        (self.root / "parked").write_text("a file, not a directory")
        self.tick(0)
        log = self.tick(120)
        self.assertTrue(any(l.startswith("park pa:") for l in log))
        self.assertIn("pa", self.backend.sessions)

    def test_wake_rejects_bad_names(self):
        with self.assertRaises(ValueError):
            self.parks.wake("../etc")



class Loop(unittest.TestCase):
    def test_exception_in_a_tick_does_not_end_the_loop(self):
        import os
        import signal

        class Flaky:
            calls = 0

            def tick(self, now):
                Flaky.calls += 1
                if Flaky.calls == 1:
                    raise RuntimeError("boom")
                os.kill(os.getpid(), signal.SIGTERM)   # second pass: ask to stop
                return []

        old = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGTERM, old[0])
        self.addCleanup(signal.signal, signal.SIGINT, old[1])
        from unittest import mock
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(run(Flaky(), 0.01), 0)
        self.assertEqual(Flaky.calls, 2)

    def test_state_dir_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(check_state_dir(Path(tmp) / "state"))
            bad = Path(tmp) / "file"
            bad.write_text("x")
            self.assertIn("state directory", check_state_dir(bad))

    def test_cli_rejects_non_positive_interval(self):
        from unittest import mock
        from relayer.cli import main
        with mock.patch("sys.stderr"):
            self.assertEqual(main(["supervise", "--interval", "0"]), 1)



class MemorySync(unittest.TestCase):
    """The supervisor's memory pass, with a fake repository (SPEC §7)."""

    class FakeMemory:
        def __init__(self, root, fail=()):
            self.root, self.fail = root, set(fail)
            self.collected, self.commits, self.pushes = [], [], 0
            self.push_state = type("S", (), {"blocked": None})()
            self.push_result = None

        def live_dir(self, name):
            return str(self.root / name)

        def collect(self, ident):
            if ident.name in self.fail:
                raise RuntimeError("dump failed")
            self.collected.append(ident.name)
            return []

        def commit(self, names, when):
            self.commits.append(list(names))
            return list(names), []

        def push(self):
            self.pushes += 1
            self.push_state.blocked = self.push_result
            return self.push_result

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for n in ("pa", "pb"):
            (self.root / n).mkdir()
        idents = [type("I", (), {"name": n})() for n in ("pa", "pb", "other")]
        self.backend = Backend()
        self.backend.accounts = type("A", (), {"known": lambda self_: idents})()
        self.backend.memory = self.FakeMemory(self.root, fail={"pb"})
        hs = {n: heteronym(n, folder=str(self.root / "w" / n)) for n in ("pa", "pb")}
        rt = dataclasses.replace(RT, memory_repo="/r", memory_interval=600)
        self.sup = Supervisor(self.backend, lambda: (rt, hs), ParkStore(self.root / "parked"),
                              30, ENV)

    def test_one_failure_does_not_stop_the_others(self):
        log = self.sup.tick(T0)
        self.assertEqual(self.backend.memory.collected, ["pa"])     # "other" is not this class's
        self.assertIn("memory pb: dump failed", log)
        self.assertEqual(self.backend.memory.commits, [["pa"]])
        self.assertIn("memory: committed pa", log)

    def test_runs_once_per_interval(self):
        self.sup.tick(T0)
        self.sup.tick(T0 + 300)
        self.assertEqual(len(self.backend.memory.commits), 1)
        self.sup.tick(T0 + 600)
        self.assertEqual(len(self.backend.memory.commits), 2)

    def test_push_failure_is_logged_and_kept(self):
        self.backend.memory.push_result = "push failed: offline"
        log = self.sup.tick(T0)
        self.assertIn("memory: not pushed, kept locally: push failed: offline", log)


if __name__ == "__main__":
    unittest.main()
