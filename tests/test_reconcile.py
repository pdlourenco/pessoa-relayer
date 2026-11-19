"""Reconciler plan and apply (docs/SPEC.md §4)."""

import shlex
import tempfile
import unittest

from relayer.reconcile import Action, apply, plan
from relayer.runtime import Runtime
from relayer.sessions import Running

from helpers import FakeBackend, persona


def H(p):
    """Stand-in session hash for plan tests."""
    return "h-" + p.config_hash()


def P(desired, running_sessions):
    return plan(desired, running_sessions, H)


def running(p, config_hash=None):
    return Running(p.name, config_hash if config_hash is not None else H(p), 0)


class Plan(unittest.TestCase):
    def test_nothing_to_do_when_in_sync(self):
        pa = persona()
        self.assertEqual(P({"pa": pa}, {"pa": running(pa)}), [])

    def test_start_missing(self):
        self.assertEqual(P({"pa": persona()}, {}),
                         [Action("start", "pa", "not running")])

    def test_disabled_not_started(self):
        self.assertEqual(P({"pa": persona(enabled=False)}, {}), [])

    def test_stop_disabled(self):
        pa = persona(enabled=False)
        self.assertEqual(P({"pa": pa}, {"pa": running(pa)}),
                         [Action("stop", "pa", "disabled")])

    def test_stop_removed(self):
        self.assertEqual(P({}, {"old": Running("old", "x", 0)}),
                         [Action("stop", "old", "not in personas.yaml")])

    def test_restart_on_hash_change(self):
        pa = persona()
        self.assertEqual(P({"pa": pa}, {"pa": running(pa, "stale")}),
                         [Action("restart", "pa", "configuration changed")])

    def test_untagged_session_restarts(self):
        # A session that lost its hash tag cannot be trusted to match.
        pa = persona()
        self.assertEqual([a.kind for a in plan({"pa": pa}, {"pa": running(pa, "")}, H)],
                         ["restart"])

    def test_order_stops_then_restarts_then_starts(self):
        a, b = persona("a"), persona("b")
        actions = plan({"a": a, "b": b, "c": persona("c")},
                       {"a": running(a, "stale"), "z": Running("z", "", 0)}, H)
        self.assertEqual([(x.kind, x.name) for x in actions],
                         [("stop", "z"), ("restart", "a"), ("start", "b"), ("start", "c")])


class Apply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Runtime(models={"fast": "f", "standard": "s", "strong": "o",
                                       "frontier": "x"},
                               command=("agent", "--model", "{model}", "{workdir}"),
                               env={"TERM": "xterm"})

    ENV = {"PATH": "/bin", "HOME": "/home/a", "GH_TOKEN": "secret"}

    def test_start_passes_command_workdir_hash(self):
        pa = persona(folder=f"{self.tmp.name}/pa")
        backend = FakeBackend()
        errors = apply([Action("start", "pa", "")], {"pa": pa}, backend, self.runtime, self.ENV)
        self.assertEqual(errors, [])
        _, name, command, workdir, h = backend.calls[0]
        words = shlex.split(command)
        self.assertEqual(words[:2], ["env", "-i"])
        self.assertEqual(words[-4:], ["agent", "--model", "s", f"{self.tmp.name}/pa"])
        self.assertIn("TERM=xterm", words)
        self.assertNotIn("GH_TOKEN=secret", words)
        self.assertEqual((name, workdir, h),
                         ("pa", f"{self.tmp.name}/pa", self.runtime.session_hash(pa, self.ENV)))

    def test_restart_stops_then_starts(self):
        pa = persona(folder=f"{self.tmp.name}/pa")
        backend = FakeBackend()
        apply([Action("restart", "pa", "")], {"pa": pa}, backend, self.runtime, self.ENV)
        self.assertEqual([c[0] for c in backend.calls], ["stop", "start"])

    def test_one_failure_does_not_block_others(self):
        a = persona("a", folder=f"{self.tmp.name}/a")
        b = persona("b", folder=f"{self.tmp.name}/b")
        backend = FakeBackend(fail={"a"})
        errors = apply([Action("start", "a", ""), Action("start", "b", "")],
                       {"a": a, "b": b}, backend, self.runtime, self.ENV)
        self.assertEqual(len(errors), 1)
        self.assertIn("start a", errors[0])
        self.assertEqual([c[1] for c in backend.calls], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
