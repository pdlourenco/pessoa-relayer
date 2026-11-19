"""Runtime config and the API-key guard (docs/SPEC.md §3)."""

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from relayer.cli import main
from relayer.config import ConfigError
from relayer.runtime import check_environment, load_runtime

from helpers import persona

REPO = Path(__file__).resolve().parent.parent

GOOD = """\
version: 1
models: {fast: h, standard: s, strong: o, frontier: f}
session:
  command: ["claude", "--model", "{model}", "--settings", "{settings}"]
"""


class RuntimeConfig(unittest.TestCase):
    def load(self, body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "relayer.yaml"
        path.write_text(textwrap.dedent(body))
        return load_runtime(path)

    def rejected(self, body, expected):
        with self.assertRaises(ConfigError) as exc:
            self.load(body)
        self.assertIn(expected, "\n".join(exc.exception.errors))

    def test_shipped_example_loads(self):
        rt = load_runtime(REPO / "examples" / "relayer.yaml")
        self.assertEqual(rt.models["frontier"], "fable")

    def test_session_argv_fills_placeholders(self):
        rt = self.load(GOOD)
        argv = rt.session_argv(persona(tier="strong", allow=("Read", "Bash(git *)")))
        self.assertEqual(argv[:3], ["claude", "--model", "o"])
        self.assertEqual(json.loads(argv[4]), {"permissions": {
            "allow": ["Read", "Bash(git *)"], "deny": ["Edit(./.claude/**)"]}})

    def test_missing_tier_mapping(self):
        self.rejected(GOOD.replace(" frontier: f", ""), "models.frontier: missing model name")

    def test_unknown_placeholder(self):
        self.rejected(GOOD.replace("{settings}", "{token}"), "unknown placeholder {token}")

    def test_api_key_in_session_env(self):
        self.rejected(GOOD + "  env: {ANTHROPIC_API_KEY: sk-x}\n",
                      "session.env.ANTHROPIC_API_KEY: not allowed")

    def test_bad_socket(self):
        self.rejected(GOOD + "tmux_socket: ../x\n", "tmux_socket: must match")

    def test_unknown_key(self):
        self.rejected(GOOD + "sesion: {}\n", "sesion: unknown key")


class ApiKeyGuard(unittest.TestCase):
    def test_flags_key_in_environment(self):
        self.assertEqual(len(check_environment({"ANTHROPIC_API_KEY": "sk-x"})), 1)
        self.assertEqual(check_environment({"ANTHROPIC_API_KEY": ""}), [])
        self.assertEqual(check_environment({"PATH": "/bin"}), [])

    def test_reconcile_refuses_before_touching_sessions(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-x"}), \
                mock.patch("relayer.cli.TmuxBackend") as backend, \
                mock.patch("sys.stderr"):
            code = main(["reconcile", "--personas", str(REPO / "examples/personas.yaml"),
                         "--templates", str(REPO / "templates"),
                         "--runtime", str(REPO / "examples/relayer.yaml")])
        self.assertEqual(code, 2)
        backend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
